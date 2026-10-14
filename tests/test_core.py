"""Offline lifecycle, prompt cascade and captured-worker regressions."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_decisions import noul, stop_answers
from tests.test_compaction import answer as compact_answer

FIXTURES = Path(__file__).parent / "fixtures"


def event(repo, kind, **changes):
    value = {"event": kind, "host": "claude-code", "session_id": "a1",
             "cwd": str(repo), "source": "startup", "transcript_path": "",
             "prompt": "", "answer": "", "tool": {}}
    value.update(changes)
    return value


def turn(prompt="Keep twelve columns.", answer="Applied.", session="a1"):
    return {"ts": "2026-10-14T07:31:02Z", "session": session,
            "prompt": prompt, "answer": answer, "actions": []}


def stop_body(nature="project", **changes):
    answers = stop_answers(nature=nature)
    answers.pop("confirmed_d4")
    answers["card_outdated"] = noul(0)
    answers["reason_missing"] = noul(0)
    answers.update(changes)
    return {"answers": answers, "usage": {"input_tokens": 500}}


class CoreTests(IsolatedTest):
    def prepare(self, kind, **changes):
        from memsift import core
        return core.prepare(event(self.repo, kind, **changes))

    def session(self, session_id="a1"):
        from memsift import store
        return store.read_json(store.session_path(session_id), {})

    def push_prompt(self, missing=0, cognee=0, **answers):
        self.fake.push({"answers": {"missing": noul(missing), "cognee": noul(cognee),
                                    **{key: noul(value) for key, value in answers.items()}},
                        "usage": {"input_tokens": 500}})

    def test_missing_card_flag_is_given_once_without_memory(self):
        from memsift import core
        self.prepare("start")
        self.push_prompt()
        first = core.prompt(self.prepare("prompt", prompt="Fix a typo."), self.config)
        self.push_prompt()
        second = core.prompt(self.prepare("prompt", prompt="Continue."), self.config)
        self.assertEqual(first, "memsift:\n- This repo has no jev.md. Propose a first version to the user.")
        self.assertEqual(second, "")
        self.assertFalse((self.repo / "jev.md").exists())
        self.assertFalse((self.home / ".memsift/datasets.md").exists())

    def test_start_fields_and_saved_root_override_later_cwd(self):
        from memsift import core, store
        self.prepare("start", source="fork")
        saved = self.session()
        self.assertEqual(set(saved), {"repo_key", "repo_root", "host", "source", "started_at",
                                      "turn", "prompt", "snapshot", "actions", "flags", "outage_notices"})
        self.assertEqual(saved["source"], "resume")
        self.assertEqual(saved["repo_root"], str(self.repo.resolve()))
        self.assertEqual(saved["turn"], 0)
        self.assertEqual(saved["outage_notices"], [])
        for kind in ("prompt", "tool", "stop", "end"):
            self.assertEqual(self.prepare(kind, cwd="/outside/acme-shop")["repo_root"], str(self.repo.resolve()))
        with self.assertRaises(ValueError):
            core.prepare(event(self.repo, "start", cwd=""))
        self.assertEqual(store.repo_key(self.repo), saved["repo_key"])

    def test_paused_prompt_saves_turn_and_stop_queues_its_snapshot(self):
        from memsift import breaker, core, store
        self.prepare("start")
        breaker.record("jev", {"kind": "auth", "reason": "key refused", "status": 401})
        self.assertIn("Memory is off", core.prompt(self.prepare("prompt", prompt="Prompt A"), self.config))
        groups = [{"turn": i, "prompt": "Prompt A" if i == 7 else str(i),
                   "items": ["Edit src/header.tsx", "Bash: tests (failed pending)"]} for i in range(1, 8)]
        store.update_session_if_exists("a1", lambda s: dict(s, actions=groups, turn=7))
        stamp = datetime(2026, 10, 14, 7, 31, 2, tzinfo=timezone.utc)
        with patch("memsift.core.datetime") as clock:
            clock.now.return_value = stamp
            captured = self.prepare("stop", answer="Answer A")
        original = copy.deepcopy(captured)
        core.prompt(self.prepare("prompt", prompt="Prompt B"), self.config)
        self.assertEqual(core.stop(captured, self.config), "")
        self.assertEqual(captured, original)
        self.assertEqual(len(captured["session"]["actions"]), 5)
        self.assertEqual(store.pending_turns(self.repo), [{"ts": "2026-10-14T07:31:02Z", "session": "a1",
                                                        "prompt": "Prompt A", "answer": "Answer A",
                                                        "actions": ["Edit src/header.tsx"]}])
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.session()["prompt"], "Prompt B")

    def test_notes_win_in_file_order_and_prompt_sends_no_instructions(self):
        from memsift import core, store
        (self.repo / "jev.md").write_text("acme-shop card", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("private instructions", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("other instructions", encoding="utf-8")
        store.write_text(store.repo_dir(self.repo) / "digest.md", "## Decisions\n- [g1] Twelve columns (2026-10-12)\n")
        entry = store.append_draft(self.repo, turn(), "project", False, 3)
        self.prepare("start", source="compact")
        self.push_prompt(1, 1, entry_g1=1, entry_d1=1)
        result = core.prompt(self.prepare("prompt", prompt="Back to the header"), self.config)
        self.assertEqual(result, "memsift, from the working notes:\n- [g1] Twelve columns (2026-10-12)\n" + entry["text"])
        state = self.fake.requests[0]["body"]["state"]
        self.assertEqual(state["session_start"], "only the compaction summary and what followed")
        self.assertNotIn("instruction_files", state)
        self.assertNotIn("private instructions", repr(state))
        self.assertEqual(len(self.fake.requests), 1)

    def test_stop_keeps_raw_labels_confirms_and_emits_flags_once(self):
        from memsift import core, store
        (self.repo / "jev.md").write_text("acme-shop card", encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("First instructions", encoding="utf-8")
        (self.repo / "AGENTS.md").write_text("Second instructions", encoding="utf-8")
        correction = store.append_draft(self.repo, turn(), "project", True, 3)
        self.prepare("start")
        self.prepare("prompt", prompt="Use the footer grid because it keeps alignment.")
        self.fake.push(stop_body(confirmed_d1=noul(1), card_outdated=noul(1), reason_missing=noul(1)))
        self.assertEqual(core.stop(self.prepare("stop", answer="Done."), self.config), "")
        notes = store.read_notes(self.repo)
        self.assertEqual(len(notes["entries"]), 2)
        self.assertIn("- [d1] confirmed\n", notes["draft"])
        self.assertIn("User: Use the footer grid because it keeps alignment.", notes["draft"])
        self.assertEqual(notes["pending"], {})
        sent = self.fake.requests[0]["body"]["state"]
        self.assertEqual(sent["pending_corrections"], {"d1": correction["text"]})
        self.assertIn("First instructions", sent["instruction_files"])
        self.assertIn("Second instructions", sent["instruction_files"])
        self.assertEqual(self.session()["flags"], ["jev_md_outdated", "decision_reason_missing:d2"])
        self.push_prompt(entry_d1=0, entry_d2=0)
        result = core.prompt(self.prepare("prompt", prompt="Continue."), self.config)
        self.assertIn("jev.md looks outdated. Propose an update to the user.", result)
        self.assertIn("Entry `d2` records a structuring decision without its reason. Ask the user why.", result)
        self.assertEqual(self.session()["flags"], [])

    def test_every_failed_classification_queues_once(self):
        from memsift import breaker, core, store
        self.prepare("start")
        for status in (500, 401, 403, 429, 529, 413, 200):
            with self.subTest(status=status):
                breaker.resume("jev")
                self.prepare("prompt", prompt="Keep the grid " + str(status))
                self.fake.push({"error": "request too large" if status == 413 else "unavailable"}, status=status)
                if status == 413:
                    self.fake.push({"error": "request too large"}, status=413)
                self.assertEqual(core.stop(self.prepare("stop", answer="Done."), self.config), "")
                pending = store.pending_turns(self.repo)
                self.assertEqual(pending[-1]["prompt"], "Keep the grid " + str(status))
                self.assertEqual(len(pending), (500, 401, 403, 429, 529, 413, 200).index(status) + 1)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0 if status in (429, 529) else 1)
        self.assertEqual(store.read_notes(self.repo)["entries"], [])
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_successful_dropped_stop_drains_backlog(self):
        from memsift import core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Fix typo.")
        store.queue_turn(self.repo, turn(), 3)
        self.fake.push(stop_body("nothing"))
        self.fake.push(stop_body())
        core.stop(self.prepare("stop", answer="Fixed."), self.config)
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertIn("User: Keep twelve columns.", store.read_notes(self.repo)["draft"])

    def test_stop_persistence_failure_is_not_requeued(self):
        from memsift import core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Keep twelve columns.")
        self.fake.push(stop_body())
        with patch("memsift.store.append_draft", side_effect=OSError("injected write fault")):
            with self.assertRaises(OSError):
                core.stop(self.prepare("stop", answer="Applied."), self.config)
        self.assertEqual(store.pending_turns(self.repo), [])

    def test_tool_is_local_and_keeps_other_session_fields(self):
        from memsift import core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Update the acme-shop grid.")
        store.update_session_if_exists("a1", lambda s: dict(s, flags=["jev_md_outdated"]))
        self.assertEqual(core.tool(self.prepare("tool", tool={"name": "Edit", "changes": [
            {"path": "src/header.tsx", "kind": "edit", "diff": "ten → twelve"}],
            "command": "", "exit_code": None}), self.config), "")
        self.assertEqual(self.session()["actions"], [{"turn": 1, "prompt": "Update the acme-shop grid.",
                                                     "items": ["Edit src/header.tsx: ten → twelve"]}])
        self.assertEqual(self.session()["flags"], ["jev_md_outdated"])
        self.assertEqual(self.fake.requests, [])

    def test_stop_after_end_keeps_captured_turn_without_resurrecting_session(self):
        from memsift import core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Prompt A")
        captured = self.prepare("stop", answer="Answer A")
        entered, release = threading.Event(), threading.Event()
        failures = []
        def response(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return stop_body(card_outdated=noul(1), reason_missing=noul(1))["answers"]
        def stop_worker():
            try:
                core.stop(captured, self.config)
            except Exception as exc:
                failures.append(exc)
        with patch("memsift.jev.request", side_effect=response):
            worker = threading.Thread(target=stop_worker)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.prepare("prompt", prompt="Prompt B")
                core.end(self.prepare("end"), self.config)
                self.assertFalse(store.session_path("a1").exists())
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(store.session_path("a1").exists())
        draft = store.read_notes(self.repo)["draft"]
        self.assertIn("User: Prompt A", draft)
        self.assertIn("Agent: Answer A", draft)
        self.assertNotIn("Prompt B", draft)

    def test_cognee_cascade_searches_selected_datasets_and_filters_before_output(self):
        from memsift import core, store
        (self.repo / "jev.md").write_text("acme-shop card", encoding="utf-8")
        store.write_text(store.home() / "datasets.md", "- lessons: technical fixes\n- projects: past projects\n- articles: IGNORED\n")
        store.update_json(store.repo_dir(self.repo) / "cognee-index.json", {}, lambda _: {"datasets": [
            {"name": "lessons", "id": "lessons-id", "items": ["sharp build scripts"]},
            {"name": "projects", "id": "projects-id", "items": []},
            {"name": "articles", "id": "ignored-id", "items": ["excluded"]}]})
        self.prepare("start")
        for useful, known, expected in ((0.7, 0, "memsift, from Cognee (dataset lessons):\nAllow sharp."),
                                        (0.7, 0.5, ""), (0.69, 0, "")):
            with self.subTest(useful=useful, known=known):
                before = len(self.fake.requests)
                self.push_prompt(1, 1, dataset_lessons=1, dataset_projects=0)
                self.fake.push([{"source": "graph", "text": "Allow sharp."}])
                self.fake.push({"answers": {"useful_1": noul(useful), "known_1": noul(known)},
                                "usage": {"input_tokens": 500}})
                result = core.prompt(self.prepare("prompt", prompt="What fixed sharp?"), self.config)
                self.assertEqual(result, expected)
                first, recall, passage = self.fake.requests[before:]
                self.assertEqual(first["body"]["state"]["datasets"], {
                    "lessons": {"description": "technical fixes", "items": ["sharp build scripts"]},
                    "projects": {"description": "past projects", "items": []}})
                self.assertEqual(recall["body"]["dataset_ids"], ["lessons-id"])
                self.assertEqual(set(passage["body"]["state"]), {"prompt", "compaction_summary", "recent_exchanges", "passages"})
                self.assertEqual(passage["body"]["state"]["passages"], {"p1": "Allow sharp."})
                self.assertLessEqual(first["timeout"], 2)
                self.assertLessEqual(recall["timeout"], 12)
                self.assertLessEqual(passage["timeout"], 2)

    def test_empty_recall_skips_passage_model_and_pause_uses_only_local_notes(self):
        from memsift import breaker, core, store
        (self.repo / "jev.md").write_text("acme-shop card", encoding="utf-8")
        store.write_text(store.home() / "datasets.md", "- lessons: technical fixes\n")
        self.prepare("start")
        self.push_prompt(1, 1, dataset_lessons=1)
        self.fake.push([])
        self.assertEqual(core.prompt(self.prepare("prompt", prompt="Past fixes?"), self.config), "")
        self.assertEqual(len(self.fake.requests), 2)
        breaker.record("cognee", {"kind": "auth", "reason": "key refused", "status": 401})
        self.push_prompt(1, 1, dataset_lessons=1)
        result = core.prompt(self.prepare("prompt", prompt="Past fixes?"), self.config)
        self.assertIn("packets wait to be sent", result)
        self.assertEqual(len(self.fake.requests), 3)
        entry = store.append_draft(self.repo, turn(), "project", False, breaker.ratio())
        self.push_prompt(1, 1, dataset_lessons=1, entry_d1=1)
        result = core.prompt(self.prepare("prompt", prompt="Grid?"), self.config)
        self.assertEqual(result, "memsift, from the working notes:\n" + entry["text"])
        self.assertTrue(breaker.paused("cognee"))

    def test_prompt_timeout_delivers_flags_without_memory_or_pending_turn(self):
        from memsift import core, store
        self.prepare("start")
        self.fake.push({"answers": {"missing": noul(1), "cognee": noul(1)},
                        "usage": {"input_tokens": 500}}, delay=3)
        started = time.monotonic()
        result = core.prompt(self.prepare("prompt", prompt="Earlier grid?"), self.config)
        self.assertLess(time.monotonic() - started, 2.4)
        self.assertIn("This repo has no jev.md", result)
        self.assertNotIn("from the working notes", result)
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_passage_failure_never_exposes_unsorted_recall(self):
        from memsift import core, store
        self.prepare("start")
        store.write_text(store.home() / "datasets.md", "- lessons: technical fixes\n")
        self.push_prompt(1, 1, dataset_lessons=1)
        self.fake.push(["Unsorted secret-free passage."])
        self.fake.push({"error": "unavailable"}, status=500)
        result = core.prompt(self.prepare("prompt", prompt="Earlier fix?"), self.config)
        self.assertIn("This repo has no jev.md", result)
        self.assertNotIn("Unsorted", result)
        self.assertEqual(len(self.fake.requests), 3)

    def test_prompt_protected_oversize_and_file_fault_keep_flags(self):
        from memsift import core, store
        self.prepare("start")
        result = core.prompt(self.prepare("prompt", prompt="x" * 100000), self.config)
        self.assertIn("This repo has no jev.md", result)
        self.assertEqual(self.fake.requests, [])
        store.update_session_if_exists("a1", lambda s: dict(s, flags=["jev_md_outdated"]))
        with patch("memsift.store.read_notes", side_effect=OSError("PASSWORD=invented-private-value")):
            result = core.prompt(self.prepare("prompt", prompt="Grid"), self.config)
        self.assertIn("jev.md looks outdated", result)
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)

    def test_source_descriptions_and_hosts_use_normalized_fixtures(self):
        from memsift import core, host
        (self.repo / "jev.md").write_text("acme-shop card", encoding="utf-8")
        cases = (("startup", "new session: the agent knows nothing of this repo"),
                 ("clear", "new session: the agent knows nothing of this repo"),
                 ("compact", "only the compaction summary and what followed"),
                 ("resume", "the whole previous conversation"),
                 ("fork", "the whole previous conversation"))
        for host_name in ("claude-code", "codex"):
            for source, description in cases:
                with self.subTest(host=host_name, source=source):
                    raw = json.loads((FIXTURES / "hooks" / host_name / "start.json").read_text())
                    raw.update(cwd=str(self.repo), source=source, transcript_path="")
                    core.prepare(host.normalize("SessionStart", raw, host_name))
                    raw = json.loads((FIXTURES / "hooks" / host_name / "prompt.json").read_text())
                    raw.update(cwd="/outside/acme-shop", transcript_path="")
                    self.push_prompt()
                    captured = core.prepare(host.normalize("UserPromptSubmit", raw, host_name))
                    self.assertEqual(core.dispatch(captured, self.config), "")
                    self.assertEqual(self.fake.requests[-1]["body"]["state"]["session_start"], description)
                    self.assertEqual(captured["repo_root"], str(self.repo.resolve()))

    def test_retraction_tail_is_sent_for_both_hosts_and_replay(self):
        from memsift import core, host, jev, store
        request = jev.request
        for host_name in ("claude-code", "codex"):
            for replay in (False, True):
                with self.subTest(host=host_name, replay=replay):
                    directory = store.repo_dir(self.repo)
                    store.write_text(directory / "draft.md", "")
                    correction = store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
                    transcript = self.home / (host_name + ".jsonl")
                    transcript.write_text((FIXTURES / "transcripts" / (host_name + "-plain.jsonl")).read_text(), encoding="utf-8")
                    raw = json.loads((FIXTURES / "hooks" / host_name / "start.json").read_text())
                    raw.update(cwd=str(self.repo), transcript_path=str(transcript))
                    core.prepare(host.normalize("SessionStart", raw, host_name))
                    sid = raw["session_id"]
                    core.prepare(event(self.repo, "prompt", host=host_name, session_id=sid,
                                       prompt="Commit the acme-shop catalog spacing."))
                    groups = [{"turn": 1, "prompt": "Align the header with the footer grid.",
                               "items": ["Edit src/header.tsx: ten → twelve", "git commit"]},
                              {"turn": 99, "prompt": "Commit the acme-shop catalog spacing.",
                               "items": ["Bash: git commit (ok)"]}]
                    store.update_session_if_exists(sid, lambda s: dict(s, turn=99, actions=groups))
                    raw = json.loads((FIXTURES / "hooks" / host_name / "stop.json").read_text())
                    raw.update(cwd="/outside/acme-shop", transcript_path=str(transcript))
                    captured = core.prepare(host.normalize("Stop", raw, host_name))
                    def classify(config, state, questions, **kwargs):
                        recent = json.dumps(state["recent_exchanges"], ensure_ascii=False)
                        self.assertIn("retract", recent)
                        self.assertLessEqual(store.estimate(recent, 3), 3000)
                        self.fake.push(stop_body("nothing", confirmed_d1=noul(0)))
                        return request(config, state, questions, **kwargs)
                    with patch("memsift.jev.request", side_effect=classify):
                        if replay:
                            store.queue_turn(self.repo, captured["turn"], 3)
                            self.assertEqual(core.replay_pending(captured, self.config), {"sorted": 1, "kept": 0})
                        else:
                            core.stop(captured, self.config)
                    sent = self.fake.requests[-1]["body"]
                    self.assertIn("recent_exchanges", sent["questions"]["confirmed_d1"]["instructions"])
                    self.assertEqual(sent["state"]["turn"]["prompt"], "Commit the acme-shop catalog spacing.")
                    self.assertEqual(sent["state"]["turn"]["actions"], ["Bash: git commit (ok)"])
                    self.assertEqual(store.read_notes(self.repo)["pending"], {"d1": correction["text"]})
                    self.assertNotIn("confirmed\n", store.read_notes(self.repo)["draft"])

    def test_stop_fitting_keeps_ends_and_persistence_uses_its_own_caps(self):
        from memsift import breaker, core, store
        self.prepare("start")
        prompt = "BEGIN" + "p" * 200000 + "END"
        answer = "FIRST" + "a" * 100000 + "LAST"
        self.prepare("prompt", prompt=prompt)
        captured = self.prepare("stop", answer=answer)
        body = stop_body()
        body["usage"]["input_tokens"] = 32000
        self.fake.push(body)
        core.stop(captured, self.config)
        sent = self.fake.requests[0]["body"]["state"]["turn"]
        self.assertTrue(sent["prompt"].startswith("BEGIN"))
        self.assertTrue(sent["prompt"].endswith("END"))
        self.assertIn("[…]", sent["prompt"])
        self.assertGreater(len(sent["answer"]), 3000)
        draft = store.read_notes(self.repo)["draft"]
        self.assertIn("User: BEGIN", draft)
        self.assertIn("END\n", draft)
        self.assertIn("Agent: FIRST", draft)
        self.assertIn("LAST\n", draft)
        fields = draft.split("  User: ")[1].split("\n  Agent: ")
        self.assertLessEqual(store.estimate(fields[0], breaker.ratio()), 1500)
        self.assertLessEqual(store.estimate(fields[1].rstrip("\n"), breaker.ratio()), 1000)
        self.assertEqual(captured["turn"]["prompt"], prompt)

    def test_stop_timeout_and_retry_timeout_queue_original_once(self):
        from memsift import breaker, core, store
        self.prepare("start")
        for retry in (False, True):
            with self.subTest(retry=retry):
                breaker.resume("jev")
                self.prepare("prompt", prompt="Keep twelve columns.")
                if retry:
                    self.fake.push({"error": "request too large"}, status=413)
                self.fake.push(stop_body(), delay=0.3)
                with patch("memsift.jev.STOP_TIMEOUT", 0.08):
                    core.stop(self.prepare("stop", answer="Applied."), self.config)
                self.assertEqual(len(store.pending_turns(self.repo)), 2 if retry else 1)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_start_drains_own_storage_and_emits_dataset_flag(self):
        from memsift import cognee, core, store
        work = self.prepare("start")
        packet = cognee.queue_packet(self.repo, "acme-shop 2026-10-14 08h00 grid rules.txt", "Twelve columns.")
        self.fake.push([{"name": "lessons", "id": "lessons-id"}])
        self.fake.push([])
        self.fake.push({"ok": True})
        self.assertEqual(core.start(work, self.config), "")
        self.assertFalse(packet.exists())
        self.assertTrue((store.repo_dir(self.repo) / "cognee-index.json").exists())
        self.push_prompt(dataset_lessons=0)
        result = core.prompt(self.prepare("prompt", prompt="Continue"), self.config)
        self.assertIn("Cognee dataset `lessons` has no line in datasets.md. Propose a description to the user, or IGNORED.", result)
        self.assertEqual(self.session()["flags"], [])
        self.assertFalse((store.home() / "datasets.md").exists())

    def test_start_worker_after_end_drops_flags_and_does_not_recreate_session(self):
        from memsift import core, store
        captured = self.prepare("start")
        entered, release = threading.Event(), threading.Event()
        errors = []
        def index(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return ["lessons"]
        def start_worker():
            try:
                core.start(captured, self.config)
            except Exception as exc:
                errors.append(exc)
        with patch("memsift.cognee.rebuild_index", side_effect=index):
            worker = threading.Thread(target=start_worker)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                core.end(self.prepare("end"), self.config)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(store.session_path("a1").exists())

    def test_new_flags_survive_prompt_and_action_work(self):
        from memsift import core, jev, store
        self.prepare("start")
        request = jev.request
        def classify(*args, **kwargs):
            store.update_session_if_exists("a1", lambda s: dict(s, flags=s["flags"] + ["jev_md_outdated"]))
            return request(*args, **kwargs)
        self.push_prompt()
        with patch("memsift.jev.request", side_effect=classify):
            result = core.prompt(self.prepare("prompt", prompt="Continue"), self.config)
        self.assertIn("This repo has no jev.md", result)
        self.assertIn("jev.md looks outdated", result)
        store.update_session_if_exists("a1", lambda s: dict(s, flags=["unknown_future_flag"]))
        self.push_prompt()
        self.assertEqual(core.prompt(self.prepare("prompt", prompt="Continue"), self.config), "")
        self.assertEqual(self.session()["flags"], ["unknown_future_flag"])

    def test_compaction_boundary_and_end_force_with_real_outbox(self):
        from memsift import breaker, core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Fix typo")
        for characters, expected in ((23997, False), (23998, True)):
            with self.subTest(characters=characters):
                store.write_text(store.repo_dir(self.repo) / "digest.md", "x" * characters)
                with patch("memsift.jev.request", return_value=stop_body("nothing")["answers"]), patch(
                    "memsift.compactor.compact", return_value=True) as compact:
                    core.stop(self.prepare("stop", answer="Done"), self.config)
                self.assertEqual(compact.call_count, int(expected))
        store.write_text(store.repo_dir(self.repo) / "digest.md", "")
        store.append_draft(self.repo, turn(), "project", False, 3)
        self.fake.push({"choices": [{"message": {"content": json.dumps(compact_answer())}}]})
        self.fake.push({"ok": True})
        core.end(self.prepare("end"), self.config)
        self.assertFalse(store.session_path("a1").exists())
        self.assertEqual(store.read_notes(self.repo)["draft"].count("- [d"), 0)
        self.assertIn("- [g1]", store.read_notes(self.repo)["digest"])
        self.assertEqual(list((store.repo_dir(self.repo) / "outbox").glob("*.txt")), [])
        self.assertFalse(breaker.paused("compactor"))

    def test_pauses_block_automatic_start_and_end_compaction(self):
        from memsift import breaker, core, store
        for service in ("jev", "cognee", "compactor"):
            with self.subTest(service=service):
                for name in ("jev", "cognee", "compactor"):
                    breaker.resume(name)
                work = self.prepare("start")
                store.write_text(store.repo_dir(self.repo) / "draft.md", "")
                store.append_draft(self.repo, turn(), "project", False, 3)
                before = store.read_notes(self.repo)
                breaker.record(service, {"kind": "auth", "reason": "key refused", "status": 401})
                if service == "compactor":
                    self.fake.push([])
                    self.fake.push({"ok": True})
                core.start(work, self.config)
                if service == "cognee":
                    self.fake.push({"choices": [{"message": {"content": json.dumps(compact_answer())}}]})
                core.end(self.prepare("end"), self.config)
                self.assertFalse(store.session_path("a1").exists())
                if service in ("jev", "compactor"):
                    self.assertEqual(store.read_notes(self.repo), before)
                else:
                    self.assertEqual(len(list((store.repo_dir(self.repo) / "outbox").glob("*.txt"))), 1)
                self.assertTrue(breaker.paused(service))

    def test_end_deletes_session_even_when_compaction_raises(self):
        from memsift import core, store
        self.prepare("start")
        store.append_draft(self.repo, turn(), "project", False, 3)
        with patch("memsift.compactor.compact", side_effect=OSError("injected compaction fault")):
            with self.assertRaises(OSError):
                core.end(self.prepare("end"), self.config)
        self.assertFalse(store.session_path("a1").exists())
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)

    def test_start_prunes_only_sessions_strictly_older_than_seven_days(self):
        from memsift import store
        self.prepare("start", session_id="old")
        self.prepare("start", session_id="boundary")
        now = time.time()
        os.utime(store.session_path("old"), (now - 604801, now - 604801))
        os.utime(store.session_path("boundary"), (now - 604800, now - 604800))
        with patch("memsift.core.time.time", return_value=now):
            self.prepare("start")
        self.assertFalse(store.session_path("old").exists())
        self.assertTrue(store.session_path("boundary").exists())

    def test_start_error_is_logged_and_still_drains_outbox(self):
        from memsift import cognee, core, store
        captured = self.prepare("start")
        packet = cognee.queue_packet(self.repo, "acme-shop 2026-10-14 08h00 grid rules.txt", "Twelve columns.")
        with patch("memsift.cognee.rebuild_index", side_effect=OSError("PASSWORD=invented-private-value")):
            self.fake.push({"ok": True})
            core.start(captured, self.config)
        self.assertFalse(packet.exists())
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("core-start-index", log)
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)

    def test_concurrent_tool_updates_recompute_without_losing_actions_or_flags(self):
        from memsift import core, host, store
        self.prepare("start")
        self.prepare("prompt", prompt="Update the grid.")
        tools = [{"name": "Write", "changes": [{"path": name, "kind": "write", "diff": ""}],
                  "command": "", "exit_code": None} for name in ("src/header.tsx", "src/footer.tsx")]
        captured = [self.prepare("tool", tool=value) for value in tools]
        entered, release = threading.Event(), threading.Event()
        note = host.note_tool
        errors = []
        calls = []
        def compute(root, session, tool):
            calls.append(tool["changes"][0]["path"])
            result = note(root, session, tool)
            if tool is captured[0]["event"]["tool"] and calls.count("src/header.tsx") == 1:
                entered.set()
                self.assertTrue(release.wait(5))
            return result
        def run():
            try:
                core.tool(captured[0], self.config)
            except Exception as exc:
                errors.append(exc)
        with patch("memsift.host.note_tool", side_effect=compute):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                core.tool(captured[1], self.config)
                store.update_session_if_exists("a1", lambda s: dict(s, flags=["jev_md_outdated"]))
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls.count("src/header.tsx"), 2)
        self.assertEqual(self.session()["actions"][0]["items"], ["Write src/footer.tsx", "Write src/header.tsx"])
        self.assertEqual(self.session()["flags"], ["jev_md_outdated"])
        self.assertEqual(self.fake.requests, [])

    def test_tool_update_drops_if_prompt_advanced_or_session_ended(self):
        from memsift import core, host, store
        for ended in (False, True):
            with self.subTest(ended=ended):
                self.prepare("start")
                self.prepare("prompt", prompt="Prompt A")
                captured = self.prepare("tool", tool={"name": "Write", "changes": [
                    {"path": "src/header.tsx", "kind": "write", "diff": ""}], "command": "", "exit_code": None})
                note = host.note_tool
                def compute(*args):
                    if ended:
                        core.end(self.prepare("end"), self.config)
                    else:
                        self.prepare("prompt", prompt="Prompt B")
                    return note(*args)
                with patch("memsift.host.note_tool", side_effect=compute):
                    core.tool(captured, self.config)
                if ended:
                    self.assertFalse(store.session_path("a1").exists())
                else:
                    self.assertEqual(self.session()["prompt"], "Prompt B")
                    self.assertEqual(self.session()["actions"], [])

    def test_end_before_paused_stop_retains_late_pending_without_session(self):
        from memsift import breaker, core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Prompt A")
        captured = self.prepare("stop", answer="Answer A")
        breaker.record("jev", {"kind": "auth", "reason": "key refused", "status": 401})
        core.end(self.prepare("end"), self.config)
        core.stop(captured, self.config)
        self.assertFalse(store.session_path("a1").exists())
        self.assertEqual(store.pending_turns(self.repo)[0]["prompt"], "Prompt A")
        self.assertEqual(store.pending_turns(self.repo)[0]["answer"], "Answer A")

    def test_stop_local_budget_failure_queues_without_sending(self):
        from memsift import breaker, core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Keep twelve columns.")
        store.append_draft(self.repo, turn("Keep rule " + "r" * 4500), "project", True, 3)
        # Pending corrections are protected, unlike the selectable notes copy.
        for _ in range(24):
            store.append_draft(self.repo, turn("Keep rule " + "r" * 4500), "project", True, 3)
        core.stop(self.prepare("stop", answer="Applied."), self.config)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(len(store.pending_turns(self.repo)), 1)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_oversize_card_is_protected_in_prompt_and_stop(self):
        from memsift import core, store
        (self.repo / "jev.md").write_text("x" * 100000, encoding="utf-8")
        self.prepare("start")
        self.push_prompt()
        self.fake.push(stop_body())
        captured = self.prepare("prompt", prompt="Keep twelve columns.")
        self.assertEqual(core.prompt(captured, self.config), "")
        core.stop(self.prepare("stop", answer="Applied."), self.config)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(len(store.pending_turns(self.repo)), 1)
        self.assertEqual(store.read_notes(self.repo)["entries"], [])

    def test_correction_confirmation_checks_the_captured_entry_text(self):
        from memsift import core, jev, store
        self.prepare("start")
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        self.prepare("prompt", prompt="Commit the grid.")
        request = jev.request
        def classify(*args, **kwargs):
            result = request(*args, **kwargs)
            store.write_text(store.repo_dir(self.repo) / "draft.md", "")
            store.append_draft(self.repo, turn("Keep ten columns after all"), "project", True, 3)
            return result
        self.fake.push(stop_body("nothing", confirmed_d1=noul(1)))
        with patch("memsift.jev.request", side_effect=classify):
            core.stop(self.prepare("stop", answer="Committed."), self.config)
        notes = store.read_notes(self.repo)
        self.assertIn("Keep ten columns after all", notes["pending"]["d1"])
        self.assertNotIn("- [d1] confirmed", notes["draft"])

    def test_replay_failure_does_not_requeue_the_applied_current_turn(self):
        from memsift import core, store
        self.prepare("start")
        self.prepare("prompt", prompt="Current decision")
        store.queue_turn(self.repo, turn("Old decision"), 3)
        self.fake.push(stop_body())
        self.fake.push({"error": "unavailable"}, status=500)
        core.stop(self.prepare("stop", answer="Applied"), self.config)
        self.assertEqual(store.pending_turns(self.repo), [turn("Old decision")])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertIn("User: Current decision", store.read_notes(self.repo)["draft"])

    def test_failed_fit_and_pause_during_stop_are_logged_and_queue_once(self):
        from memsift import breaker, core, store
        self.prepare("start")
        for exception in (ValueError("PASSWORD=invented-private-value"), breaker.PausedError("jev")):
            with self.subTest(exception=type(exception).__name__):
                self.prepare("prompt", prompt="Keep twelve columns")
                with patch("memsift.jev.fit_stop", side_effect=exception):
                    core.stop(self.prepare("stop", answer="Applied"), self.config)
        self.assertEqual(len(store.pending_turns(self.repo)), 2)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)

    def test_missing_session_uses_cwd_without_recreating_session(self):
        from memsift import core, store
        captured = self.prepare("prompt", prompt="Fix a typo")
        self.assertEqual(captured["repo_root"], str(self.repo.resolve()))
        self.push_prompt()
        self.assertEqual(core.prompt(captured, self.config), "")
        self.assertFalse(store.session_path("a1").exists())
        with self.assertRaises(ValueError):
            self.prepare("stop", cwd="")
        with self.assertRaises(ValueError):
            self.prepare("unknown")
        with self.assertRaises(ValueError):
            self.prepare("start", source="unknown")

