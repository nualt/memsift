"""Explicit recovery and single-owner pending replay regressions."""

import json
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from tests.support import IsolatedTest, child_env
from tests.test_core import event, turn, stop_body
from tests.test_decisions import noul
from tests.test_compaction import answer as compact_answer


class ResumeTests(IsolatedTest):
    def work(self, root=None):
        return {"repo_root": str(root or self.repo),
                "event": {"host": "claude-code", "transcript_path": ""},
                "session": {}, "turn": None}

    def test_replay_has_one_owner(self):
        from memsift import core, store
        store.queue_turn(self.repo, turn(), 3)
        entered, release = threading.Event(), threading.Event()
        results = []
        def classify(work, queued, config):
            entered.set()
            self.assertTrue(release.wait(5))
            store.append_draft(self.repo, queued, "project", False, 3)
            return True
        with patch("memsift.core._classify_turn", side_effect=classify) as classify_call:
            first = threading.Thread(target=lambda: results.append(core.replay_pending(self.work(), self.config)))
            first.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(core.replay_pending(self.work(), self.config), {"sorted": 0, "kept": 0})
                store.queue_turn(self.repo, turn("New arrival"), 3)
            finally:
                release.set()
                first.join(5)
            self.assertFalse(first.is_alive())
            self.assertEqual(classify_call.call_count, 1)
        self.assertEqual(results, [{"sorted": 1, "kept": 1}])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertEqual(store.pending_turns(self.repo), [turn("New arrival")])

    def test_failed_replay_acknowledges_only_successful_prefix(self):
        from memsift import core, store
        for prompt in ("First", "Second", "Third"):
            store.queue_turn(self.repo, turn(prompt), 3)
        self.fake.push(stop_body())
        self.fake.push({"error": "unavailable"}, status=500)
        self.assertEqual(core.replay_pending(self.work(), self.config), {"sorted": 1, "kept": 1})
        self.assertEqual(store.pending_turns(self.repo), [turn("Second"), turn("Third")])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.fake.push(stop_body("nothing"))
        self.fake.push(stop_body())
        self.assertEqual(core.replay_pending(self.work(), self.config), {"sorted": 2, "kept": 1})
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 2)

    def test_resume_clears_notices_in_every_live_session_only_on_success(self):
        from memsift import breaker, core, store
        for sid in ("a1", "a2"):
            core.prepare(event(self.repo, "start", session_id=sid))
            store.update_session_if_exists(sid, lambda s: dict(s, outage_notices=["jev", "cognee", "compactor"]))
        for service in ("jev", "cognee", "compactor"):
            breaker.record(service, {"kind": "auth", "reason": "key refused", "status": 401})
        before = breaker.state()["services"]["cognee"]
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch(
            "memsift.cognee.probe", side_effect=breaker.ServiceError("cognee", "timeout")):
            result = core.resume(self.repo, self.config)
        self.assertIn("Jev ✓", result)
        self.assertIn("0 turns sorted, 0 kept", result)
        self.assertIn("Cognee ✗ still unreachable (timeout)", result)
        self.assertEqual(breaker.state()["services"]["cognee"], before)
        for sid in ("a1", "a2"):
            self.assertEqual(store.read_json(store.session_path(sid), {})["outage_notices"], ["cognee"])
        breaker.record("jev", {"kind": "auth", "reason": "new outage", "status": 401})
        for sid in ("a1", "a2"):
            result = core.prompt(core.prepare(event(self.repo, "prompt", session_id=sid, prompt="Continue")), self.config)
            self.assertIn("Memory is off", result)
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_replay_processes_share_the_real_lock_and_preserve_arrivals(self):
        from memsift import store
        store.queue_turn(self.repo, turn(), 3)
        marker, release = self.home / "entered", self.home / "release"
        source = """
import json, sys, time
from pathlib import Path
from unittest.mock import patch
from memsift import core, store
work, config, marker, release = json.load(sys.stdin)
def classify(work, turn, config):
    Path(marker).touch()
    deadline = time.monotonic() + 8
    while not Path(release).exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    store.append_draft(Path(work['repo_root']), turn, 'project', False, 3)
    return True
with patch('memsift.core._classify_turn', side_effect=classify):
    print(json.dumps(core.replay_pending(work, config)))
"""
        environment = child_env(self.fake.url, self.home)
        first = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=environment, cwd=self.repo)
        first.stdin.write(json.dumps([self.work(), self.config, str(marker), str(release)]))
        first.stdin.close()
        first.stdin = None
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            second_source = "from memsift import core; import json, sys; w,c=json.load(sys.stdin); print(json.dumps(core.replay_pending(w,c)))"
            second = subprocess.run([sys.executable, "-c", second_source],
                                    input=json.dumps([self.work(), self.config]),
                                    text=True, capture_output=True, env=environment, cwd=self.repo, timeout=5)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(json.loads(second.stdout), {"sorted": 0, "kept": 0})
            store.queue_turn(self.repo, turn("New arrival"), 3)
        finally:
            release.touch()
            output, errors = first.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, errors)
        self.assertEqual(json.loads(output), {"sorted": 1, "kept": 1})
        self.assertEqual(store.pending_turns(self.repo), [turn("New arrival")])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)

    def test_resume_probes_each_healthy_or_paused_service_once_then_drains_all_repos(self):
        from memsift import breaker, cognee, core, store
        other = self.home / "ended" / "acme-shop"
        other.mkdir(parents=True)
        packets = [cognee.queue_packet(root, "acme-shop 2026-10-14 08h00 grid rules.txt", "Twelve columns.")
                   for root in (self.repo, other)]
        store.queue_turn(self.repo, turn(), 3)
        store.queue_turn(other, turn("Other repo", session="a2"), 3)
        for paused in (False, True):
            with self.subTest(paused=paused):
                if paused:
                    for service in ("jev", "compactor", "cognee"):
                        breaker.record(service, {"kind": "auth", "reason": "key refused", "status": 401})
                before = len(self.fake.requests)
                self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}}, path="/api/alpha/decisions")
                self.fake.push({"choices": [{"message": {"content": json.dumps(compact_answer())}}]}, path="/chat/completions")
                self.fake.push([], path="/api/v1/datasets")
                if not paused:
                    self.fake.push({"ok": True}, path="/api/v1/remember")
                    self.fake.push({"ok": True}, path="/api/v1/remember")
                    self.fake.push(stop_body(), path="/api/alpha/decisions")
                result = core.resume(self.repo, self.config)
                self.assertIn("Jev ✓", result)
                self.assertIn("Cognee ✓", result)
                self.assertIn("compactor ✓", result)
                self.assertEqual(len(self.fake.requests) - before, 3 if paused else 6)
                for service in ("jev", "compactor", "cognee"):
                    self.assertEqual(breaker.state()["services"][service], {"failures": 0, "paused": False, "reason": ""})
        self.assertTrue(all(not packet.exists() for packet in packets))
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertEqual(store.pending_turns(other), [turn("Other repo", session="a2")])
        self.assertIn("User: Keep twelve columns.", store.read_notes(self.repo)["draft"])
        self.assertFalse((store.home() / "sessions").exists())

    def test_failed_probes_preserve_all_state_and_pending(self):
        from memsift import breaker, core, store
        for service in ("jev", "cognee", "compactor"):
            breaker.record(service, {"kind": "auth", "reason": "key refused", "status": 401})
        core.prepare(event(self.repo, "start"))
        store.update_session_if_exists("a1", lambda s: dict(s, outage_notices=["jev", "cognee", "compactor"]))
        store.queue_turn(self.repo, turn(), 3)
        before, session = breaker.state(), store.read_json(store.session_path("a1"), {})
        for path in ("/api/alpha/decisions", "/chat/completions", "/api/v1/datasets"):
            self.fake.push({"error": "PASSWORD=invented-private-value"}, status=401, path=path)
        result = core.resume(self.repo, self.config)
        self.assertEqual(breaker.state(), before)
        self.assertEqual(store.read_json(store.session_path("a1"), {}), session)
        self.assertEqual(store.pending_turns(self.repo), [turn()])
        self.assertEqual(len(self.fake.requests), 3)
        self.assertEqual(result.count("✗"), 3)
        self.assertNotIn("invented-private-value", result)
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_other_repo_replays_at_its_next_stop_with_its_own_card(self):
        from memsift import core, store
        other = self.home / "other" / "acme-shop"
        other.mkdir(parents=True)
        (self.repo / "jev.md").write_text("First repo card", encoding="utf-8")
        (other / "jev.md").write_text("Second repo card", encoding="utf-8")
        store.queue_turn(other, turn("Second repo decision", session="a2"), 3)
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch("memsift.cognee.probe"):
            core.resume(self.repo, self.config)
        self.assertEqual(store.pending_turns(other), [turn("Second repo decision", session="a2")])
        core.prepare(event(other, "start", session_id="a2"))
        core.prepare(event(other, "prompt", session_id="a2", prompt="Fix typo"))
        self.fake.push(stop_body())
        self.fake.push(stop_body("nothing"))
        core.stop(core.prepare(event(other, "stop", session_id="a2", answer="Done")), self.config)
        self.assertEqual(store.pending_turns(other), [])
        self.assertEqual(store.read_notes(self.repo)["entries"], [])
        self.assertIn("Second repo decision", store.read_notes(other)["draft"])
        self.assertTrue(all(r["body"]["state"]["project_card"] == "Second repo card" for r in self.fake.requests))

    def test_resume_without_work_does_not_borrow_another_sessions_history(self):
        from memsift import core, store
        other = self.home / "other" / "acme-shop"
        other.mkdir(parents=True)
        core.prepare(event(other, "start", session_id="a2"))
        store.update_session_if_exists("a2", lambda s: dict(s, actions=[{"turn": 1, "prompt": "Other history",
                                                                        "items": ["Edit other.txt"]}]))
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        store.queue_turn(self.repo, turn("Continue"), 3)
        self.fake.push(stop_body("nothing", confirmed_d1=noul(0)))
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch("memsift.cognee.probe"):
            core.resume(self.repo, self.config)
        sent = self.fake.requests[0]["body"]["state"]
        self.assertEqual(sent["recent_actions"], [])
        self.assertNotIn("recent_exchanges", sent)
        self.assertEqual(sent["pending_corrections"], {})
        self.assertNotIn("Other history", repr(sent))

    def test_resume_outbox_error_logs_and_continues_to_other_repos(self):
        from memsift import cognee, core, store
        other = self.home / "other" / "acme-shop"
        other.mkdir(parents=True)
        for root in (self.repo, other):
            cognee.queue_packet(root, "acme-shop 2026-10-14 08h00 grid rules.txt", "Twelve columns.")
        send = cognee.send_outbox
        directories = sorted((store.home() / "repos").iterdir())
        def drain(directory, config):
            if directory == directories[0]:
                raise OSError("PASSWORD=invented-private-value")
            return send(directory, config)
        self.fake.push({"ok": True})
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch("memsift.cognee.probe"), patch(
            "memsift.cognee.send_outbox", side_effect=drain):
            core.resume(self.repo, self.config)
        self.assertEqual(len(list((directories[0] / "outbox").glob("*.txt"))), 1)
        self.assertEqual(list((directories[1] / "outbox").glob("*.txt")), [])
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)

    def test_resume_uses_command_work_for_text_only_retraction(self):
        from memsift import core, store
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        store.queue_turn(self.repo, turn("Commit the grid"), 3)
        transcript = self.home / "acme-shop-transcript.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "Keep ten after all"}]}}) + "\n", encoding="utf-8")
        captured = self.work()
        captured["event"]["transcript_path"] = str(transcript)
        captured["session"]["actions"] = [{"turn": 1, "prompt": "Use twelve columns", "items": ["git commit"]}]
        self.fake.push(stop_body("nothing", confirmed_d1=noul(0)))
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch("memsift.cognee.probe"):
            result = core.resume(self.repo, self.config, work=captured)
        self.assertIn("1 turns sorted, 0 kept", result)
        state = self.fake.requests[0]["body"]["state"]
        self.assertEqual(state["recent_exchanges"], [{"role": "user", "text": "Keep ten after all", "actions": []}])
        self.assertEqual(state["recent_actions"], ["git commit"])
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertIn("d1", store.read_notes(self.repo)["pending"])

    def test_resume_before_notice_lock_does_not_restore_an_obsolete_notice(self):
        from memsift import breaker, core, store
        core.prepare(event(self.repo, "start"))
        breaker.record("jev", {"kind": "auth", "reason": "key refused", "status": 401})
        captured = core.prepare(event(self.repo, "prompt", prompt="Continue"))
        update = store.update_session_if_exists
        resumed = False
        def concurrent_resume(sid, change):
            nonlocal resumed
            if not resumed:
                resumed = True
                core.resume(self.repo, self.config)
            return update(sid, change)
        with patch("memsift.jev.probe"), patch("memsift.compactor.probe"), patch("memsift.cognee.probe"), patch(
            "memsift.store.update_session_if_exists", side_effect=concurrent_resume):
            result = core.prompt(captured, self.config)
        self.assertNotIn("Memory is off", result)
        self.assertEqual(store.read_json(store.session_path("a1"), {})["outage_notices"], [])
        breaker.record("jev", {"kind": "auth", "reason": "new outage", "status": 401})
        result = core.prompt(core.prepare(event(self.repo, "prompt", prompt="Continue")), self.config)
        self.assertIn("Memory is off", result)

    def test_confirmation_write_failure_leaves_replay_without_a_new_entry(self):
        from memsift import core, store
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        store.queue_turn(self.repo, turn("Keep the footer spacing"), 3)
        captured = self.work()
        transcript = self.home / "acme-shop-transcript.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "Keep twelve columns"}}) + "\n")
        captured["event"]["transcript_path"] = str(transcript)
        self.fake.push(stop_body(confirmed_d1=noul(1)))
        with patch("memsift.store.confirm", side_effect=TimeoutError("injected confirmation lock timeout")):
            with self.assertRaises(TimeoutError):
                core.replay_pending(captured, self.config)
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertEqual(store.pending_turns(self.repo), [turn("Keep the footer spacing")])
        self.fake.push(stop_body(confirmed_d1=noul(1)))
        self.assertEqual(core.replay_pending(captured, self.config), {"sorted": 1, "kept": 1})
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 2)
        self.assertEqual(store.read_notes(self.repo)["pending"], {})

    def test_session_flag_failure_after_append_logs_and_acknowledges_replay(self):
        from memsift import breaker, core, store
        core.prepare(event(self.repo, "start"))
        store.queue_turn(self.repo, turn(), 3)
        captured = self.work()
        captured["event"]["session_id"] = "a1"
        self.fake.push(stop_body(reason_missing=noul(1)))
        with patch("memsift.store.update_session_if_exists", side_effect=TimeoutError("PASSWORD=invented-private-value")):
            self.assertEqual(core.replay_pending(captured, self.config), {"sorted": 1, "kept": 1})
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0)
        self.assertEqual(core.replay_pending(captured, self.config), {"sorted": 0, "kept": 0})
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)
