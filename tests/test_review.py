"""Regression coverage for the final review findings, using invented data."""

import copy
import http.server
import json
from pathlib import Path
import threading
import time
import urllib.request
from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_core import event, turn, stop_body
from tests.test_decisions import noul, prompt_kind
from tests.test_compaction import answer


class ReviewTests(IsolatedTest):
    def work(self, prompt="Current decision"):
        from memsift import core
        core.prepare(event(self.repo, "start"))
        core.prepare(event(self.repo, "prompt", prompt=prompt))
        return core.prepare(event(self.repo, "stop", answer="Applied"))

    def test_access_keys_are_redacted_through_storage_and_all_services(self):
        from memsift import breaker, cognee, compactor, core, store
        from memsift.vendor.redact import redact
        secret = "invented-access-secret"
        text = "AWS_SECRET_ACCESS_KEY=" + secret
        self.assertNotIn(secret, repr(redact({"AWS_ACCESS_KEY_ID": secret, "AWS_SECRET_ACCESS_KEY": secret})))
        captured = self.work(text)
        self.fake.push(stop_body())
        core.stop(captured, self.config)
        store.queue_turn(self.repo, turn(text), 3)
        self.fake.push({"choices": [{"message": {"content": json.dumps(answer())}}]})
        compactor.request(self.config, store.read_notes(self.repo))
        cognee.queue_packet(self.repo, "acme-shop 2026-10-14 08h00 access keys.txt", text)
        self.fake.push({"ok": True})
        cognee.send_outbox(store.repo_dir(self.repo), self.config)
        for path in store.home().rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode(), path.read_bytes(), str(path))
        self.assertNotIn(secret, repr([r["body"] for r in self.fake.requests]))
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0)

    def test_authenticated_transports_reject_cross_origin_redirects(self):
        from memsift import cognee, compactor, jev
        from memsift.vendor import recall_http, remember_http
        received = []
        class Destination(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'[]')
            def log_message(self, *args):
                pass
        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        destination = "http://127.0.0.1:%s/leak" % target.server_port
        class Redirect(Destination):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", destination)
                self.end_headers()
            do_POST = do_GET
        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=source.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%s" % source.server_port
        config = dict(self.config, COGNEE_BASE_URL=url, MEMSIFT_COMPACTOR_URL=url)
        from tests.fake_services import _REAL_URLOPEN, _REAL_BUILD_OPENER
        try:
            with patch("urllib.request.urlopen", _REAL_URLOPEN), patch("urllib.request.build_opener", _REAL_BUILD_OPENER), patch("memsift.jev.ENDPOINT", url):
                operations = [lambda: cognee.probe(config),
                              lambda: jev.probe(config),
                              lambda: compactor.probe(config),
                              lambda: recall_http.do_recall(url, "invented-key", "grid", "", ["graph"], 5),
                              lambda: remember_http.do_remember(url, "invented-key", "grid", "memsift", "acme-shop")]
                for operation in operations:
                    with self.subTest(operation=operation):
                        with self.assertRaises(Exception):
                            operation()
                self.assertEqual(received, [])
        finally:
            source.shutdown()
            source.server_close()
            target.shutdown()
            target.server_close()
            thread.join()

    def test_escaped_json_response_secrets_never_reach_diagnostics(self):
        from memsift import compactor, store
        from memsift.vendor.redact import redact
        secret = "invented-escaped-secret"
        nested = json.dumps({"response": json.dumps({"PASSWORD": secret})})
        self.fake.push(nested, status=500)
        with self.assertRaises(Exception):
            compactor.request(self.config, {"digest": "", "draft": "lesson"})
        self.assertNotIn(secret, store.read_text(store.home() / "debug.log"))
        self.assertNotIn(secret, redact("proxy error: " + nested))

    def test_plain_diagnostics_mask_whole_escaped_quoted_credentials(self):
        from memsift.vendor.redact import redact
        text = json.dumps({"response": json.dumps({"PASSWORD": 'invented-head "invented-tail" secret-end'})})
        result = redact("proxy error: " + text)
        self.assertNotIn("invented-head", result)
        self.assertNotIn("invented-tail", result)
        self.assertNotIn("secret-end", result)

    def test_sensitive_dataset_keys_use_distinct_wire_names_and_local_routing(self):
        from memsift import jev
        from memsift.vendor.redact import redact
        names = ["sk-proj-" + "a" * 24, "sk-proj-" + "b" * 24]
        redacted = redact({name: index for index, name in enumerate(names)})
        self.assertEqual(len(redacted), 2)
        self.assertNotIn(names[0], repr(redacted))
        state = {"prompt": "grid", "datasets": {name: {"description": "past work"} for name in names}}
        questions = jev.prompt_questions(state)
        def respond(request, timeout, **kwargs):
            wire = json.loads(request.data)
            self.assertNotIn(names[0], repr(wire))
            self.assertNotIn(names[1], repr(wire))
            if "datasets" in wire["state"]:
                # Only the Cognee request carries the datasets (prompt request ③).
                self.assertEqual(len(list(wire["state"]["datasets"])), 2)
            # The agent lacks the information (has_info 0), so the Cognee datasets are chosen.
            self.fake.push({"answers": {key: prompt_kind(0) if key == "has_info" else noul(1) for key in wire["questions"]},
                            "usage": {"input_tokens": 500}})
            return self.fake.open(request, timeout)
        with patch("memsift.transport.open_request", side_effect=respond):
            result = jev.request(self.config, state, questions, timeout=2)
        self.assertEqual(jev.choose_prompt(result, [], names)["datasets"], names)

    def test_dictionary_key_prefixes_cannot_hide_recognizable_vendor_keys(self):
        from memsift.vendor.redact import redact
        for secret in ("sk-proj-" + "a" * 24, "sk_test_" + "a" * 24, "AKIAINVENTEDKEY00001"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, repr(redact({"dataset_" + secret: "past work"})))

    def test_invalid_configured_auth_headers_never_log_the_value(self):
        from memsift import cognee, compactor, jev, store
        from memsift.vendor import recall_http, remember_http
        secret = "invented-opaque-key"
        config = dict(self.config, COGNEE_API_KEY=secret + "\n", OPENROUTER_API_KEY=secret + "\r",
                      MEMSIFT_COMPACTOR_API_KEY=secret + "\n")
        operations = [lambda: cognee.probe(config), lambda: jev.probe(config), lambda: compactor.probe(config),
                      lambda: recall_http.do_recall(self.fake.url, secret + "\n", "grid", "", ["graph"], 5),
                      lambda: remember_http.do_remember(self.fake.url, secret + "\n", "grid", "memsift", "acme-shop")]
        for operation in operations:
            with self.assertRaises(Exception):
                operation()
        self.assertEqual(self.fake.requests, [])
        self.assertNotIn(secret, store.read_text(store.home() / "debug.log"))

    def test_pending_turns_precede_current_and_preserve_timestamp_order(self):
        from memsift import core, store
        old = dict(turn("Earlier decision"), ts="2026-10-14T07:00:00Z")
        middle = dict(turn("Later pending decision"), ts="2026-10-14T08:00:00Z")
        store.queue_turn(self.repo, middle, 3)
        store.queue_turn(self.repo, old, 3)
        captured = self.work()
        for _ in range(3):
            self.fake.push(stop_body())
        core.stop(captured, self.config)
        prompts = [request["body"]["state"]["turn"]["prompt"] for request in self.fake.requests]
        self.assertEqual(prompts, [old["prompt"], middle["prompt"], captured["turn"]["prompt"]])
        draft = store.read_notes(self.repo)["draft"]
        self.assertLess(draft.index(old["prompt"]), draft.index(middle["prompt"]))
        self.assertIn(old["ts"], draft)
        self.assertIn(middle["ts"], draft)
        self.assertEqual(store.pending_turns(self.repo), [])

    def test_malformed_json_redaction_logs_and_masks_the_fallback(self):
        from memsift import store
        from memsift.vendor.redact import redact
        result = redact('{invalid: "PASSWORD": "invented-fallback-secret"}')
        self.assertNotIn("invented-fallback-secret", result)
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_decision_logging_failure_does_not_requeue_a_durable_turn(self):
        from memsift import core, store
        captured = self.work()
        self.fake.push(stop_body())
        with patch("memsift.core.decision", side_effect=OSError("injected decision log failure")):
            core.stop(captured, self.config)
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_timestamp_sorted_replay_acknowledges_success_before_a_failure(self):
        from memsift import core, store
        old = dict(turn("Earlier decision"), ts="2026-10-14T07:00:00Z")
        later = dict(turn("Later decision"), ts="2026-10-14T08:00:00Z")
        store.queue_turn(self.repo, later, 3)
        store.queue_turn(self.repo, old, 3)
        captured = self.work()
        self.fake.push(stop_body())
        self.fake.push({}, status=500)
        self.assertEqual(core.replay_pending(captured, self.config), {"sorted": 1, "kept": 1})
        self.assertEqual(store.pending_turns(self.repo), [later])
        self.fake.push(stop_body())
        core.replay_pending(captured, self.config)
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 2)

    def test_contextless_resume_defers_correction_confirmations(self):
        from memsift import core, store
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        store.queue_turn(self.repo, turn("Committed twelve columns"), 3)
        self.fake.push(stop_body("nothing", confirmed_d1=noul(1)))
        with patch("memsift.jev.probe"), patch("memsift.cognee.probe"), patch("memsift.compactor.probe"):
            core.resume(self.repo, self.config)
        self.assertIn("d1", store.read_notes(self.repo)["pending"])
        self.assertNotIn("confirmed_d1", self.fake.requests[0]["body"]["questions"])

    def test_text_only_turns_expire_old_action_groups(self):
        from memsift import core, store
        captured = self.work()
        store.update_session_if_exists("a1", lambda s: dict(s, actions=[{
            "turn": 1, "prompt": "Current decision", "items": ["Edit grid.ts"]}]))
        for _ in range(9):
            core.prepare(event(self.repo, "prompt", prompt="Discuss"))
        self.assertEqual(store.read_json(store.session_path("a1"), {})["actions"], [])
        captured = core.prepare(event(self.repo, "stop", answer="Applied"))
        self.fake.push(stop_body())
        core.stop(captured, self.config)
        self.assertEqual(self.fake.requests[0]["body"]["state"]["recent_actions"], [])

    def test_stop_clips_oversized_recent_actions_with_the_turn(self):
        from memsift import jev
        action = "Bash: start " + "x" * 110000 + " end (ok)"
        state = {"turn": {"prompt": "grid", "answer": "Applied", "actions": [action]},
                 "recent_actions": [action], "notes": {}, "pending_corrections": {}}
        original = copy.deepcopy(state)
        questions = jev.stop_questions(state)
        fitted = jev.fit_stop(state, questions, 3)
        self.assertTrue(jev._fits(fitted, questions, 3))
        self.assertTrue(fitted["recent_actions"][0].startswith("Bash: start"))
        self.assertTrue(fitted["recent_actions"][0].endswith("end (ok)"))
        self.assertEqual(state, original)

    def test_stop_drops_oldest_notes_across_digest_and_draft(self):
        from memsift import jev
        state = {"turn": {"prompt": "grid", "answer": "ok", "actions": []}, "pending_corrections": {},
                 "notes": {"g1": "- [g1] Current progress " + "x" * 12000 + " (2026-10-14)\n",
                           "d1": "- [d1] 2026-10-01 project\n  User: " + "x" * 12000,
                           "d2": "- [d2] 2026-10-13 project\n  User: " + "x" * 12000}}
        fitted = jev.fit_stop(state, jev.stop_questions(state), 3)
        self.assertEqual(list(fitted["notes"]), ["g1", "d2"])

    def test_unrelated_recall_404_counts_as_failure(self):
        from memsift import breaker, cognee
        self.fake.push({"detail": "Unknown proxy route /api/v1/recall"}, status=404)
        with self.assertRaises(breaker.ServiceError):
            cognee.recall(self.config, "grid", [{"name": "memsift", "id": "memory"}])
        self.assertEqual(breaker.state()["services"]["cognee"]["failures"], 1)

    def test_readme_discloses_pending_corrections_in_compactor_draft(self):
        text = (Path(__file__).parents[1] / "README.md").read_text()
        row = next(line for line in text.splitlines() if line.startswith("| the compactor |"))
        self.assertIn("whole draft, including pending corrections", row)

    def test_old_end_worker_cannot_remove_resumed_session(self):
        from memsift import core, store
        self.work()
        ended = core.prepare(event(self.repo, "end"))
        core.prepare(event(self.repo, "start", source="resume"))
        core.prepare(event(self.repo, "prompt", prompt="Resumed prompt"))
        with patch("memsift.compactor.compact"):
            core.end(ended, self.config)
        self.assertTrue(store.session_path("a1").exists())
        self.assertEqual(core.prepare(event(self.repo, "stop", answer="ok"))["turn"]["prompt"], "Resumed prompt")

    def test_stop_queues_turn_on_draft_or_confirmation_write_failure(self):
        from memsift import core, store
        from memsift.vendor.file_lock import file_lock
        captured = self.work()
        self.fake.push(stop_body())
        with file_lock(store.repo_dir(self.repo) / "draft.md.lock") as acquired:
            self.assertTrue(acquired)
            with patch("memsift.store.LOCK_WAIT", 0.03):
                core.stop(captured, self.config)
        self.assertEqual(store.pending_turns(self.repo), [captured["turn"]])
        store.ack_pending(self.repo, [captured["turn"]])
        store.append_draft(self.repo, turn("Use twelve columns"), "project", True, 3)
        self.fake.push(stop_body(confirmed_d1=noul(1)))
        with patch("memsift.store.confirm", side_effect=OSError("injected confirmation failure")):
            core.stop(captured, self.config)
        self.assertEqual(store.pending_turns(self.repo), [captured["turn"]])

    def test_upload_and_get_bound_slow_success_and_error_bodies(self):
        from memsift import breaker, cognee, store
        for status in (200, 500):
            for operation in ("get", "upload"):
                with self.subTest(status=status, operation=operation):
                    breaker.resume("cognee")
                    body = b'[]' if operation == "get" else b'{"ok":true}'
                    chunks = [body[:1]] + [b' '] * 20 + [body[1:]]
                    self.fake.push_chunks(chunks, interval=0.02, status=status)
                    if operation == "upload":
                        packet = cognee.queue_packet(self.repo, "acme-shop 2026-10-14 08h00 grid.txt", "grid")
                    start = time.monotonic()
                    with patch("memsift.cognee.UPLOAD_TIMEOUT", 0.05):
                        if operation == "get":
                            with self.assertRaises(breaker.ServiceError):
                                cognee.list_datasets(self.config)
                        else:
                            self.assertEqual(cognee.send_outbox(store.repo_dir(self.repo), self.config), 0)
                            self.assertTrue(packet.exists())
                            packet.unlink()
                    self.assertLess(time.monotonic() - start, 0.2)
                    self.assertEqual(breaker.state()["services"]["cognee"]["failures"], 1)

    def test_note_reader_cannot_observe_mixed_compaction_files(self):
        from memsift import store
        store.append_draft(self.repo, turn(), "project", False, 3)
        snapshot = store.notes_snapshot(self.repo)
        read = store.read_text
        entered, finished = threading.Event(), threading.Event()
        failures = []
        workers = []
        def replace():
            entered.set()
            try:
                store.replace_notes(self.repo, snapshot, "- [g1] Twelve columns (2026-10-14)\n")
            except Exception as exc:
                failures.append(exc)
            finally:
                finished.set()
        def interleave(path):
            result = read(path)
            if path.name == "digest.md" and not workers:
                worker = threading.Thread(target=replace)
                workers.append(worker)
                worker.start()
                self.assertTrue(entered.wait(1))
                finished.wait(0.1)
            return result
        with patch("memsift.store.read_text", side_effect=interleave):
            notes = store.read_notes(self.repo)
        for worker in workers:
            worker.join(2)
        self.assertEqual(failures, [])
        self.assertEqual(len(notes["entries"]), 1)
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
