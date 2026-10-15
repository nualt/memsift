"""Offline compaction validation, durability and concurrent append regressions."""

import copy
from datetime import datetime, timezone
import json
import threading
import time
from unittest.mock import patch

from tests.support import IsolatedTest


def answer(text="The header reuses the footer grid.", nature="decisions", changes=()):
    new = [{"section": nature, "text": text, "date": "2026-10-14"}] if text else []
    return {"changes": list(changes), "new": new,
            "packet": {"topics": "header grid", "text": text or "Build needs sharp."}}


def sections(**lines):
    value = {key: [] for key in ("in_progress", "decisions", "project_facts", "working_rules")}
    value.update(lines)
    return value


def envelope(value):
    return {"choices": [{"message": {"content": json.dumps(value), "reasoning": "Not memory."}}]}


class CompactionTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        from memsift import breaker, cognee, compactor, store
        self.breaker, self.cognee, self.compactor, self.store = breaker, cognee, compactor, store

    def add(self, prompt="Keep the footer grid.", nature="project", correction=False):
        turn = {"ts": "2026-10-14T07:31:02Z", "session": "a1",
                "prompt": prompt, "answer": "Applied.", "actions": []}
        return self.store.append_draft(self.repo, turn, nature, correction, 3.0)

    def log(self):
        return self.store.read_text(self.store.home() / "debug.log")

    def service(self, name="compactor"):
        return self.breaker.state()["services"][name]

    def packets(self):
        return list((self.store.repo_dir(self.repo) / "outbox").glob("*.txt"))

    def pause(self, name):
        self.breaker.record(name, {"kind": "auth", "reason": "invented refusal", "status": 401})

    def test_two_invalid_answers_leave_notes_and_count_once(self):
        self.add()
        before = self.store.read_notes(self.repo)
        with patch("memsift.compactor.request", return_value={"digest": {}}) as call:
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(call.call_count, 2)
        self.assertEqual(self.store.read_notes(self.repo), before)
        self.assertEqual(self.service()["failures"], 1)
        self.assertEqual(self.packets(), [])
        self.assertIn("packet", self.log())
        self.assertIn("Traceback", self.log())

    def test_digest_ids_are_assigned_after_trimming(self):
        value = self.compactor.apply("", answer("header uses footer grid", "in_progress"))
        text = self.compactor.render_digest(self.repo, value, 3.0)
        self.assertIn("## In progress\n- [g1] header uses footer grid (2026-10-14)", text)

    def test_schema_rejects_invalid_fields_dates_and_model_ids(self):
        self.add()
        notes = self.store.read_notes(self.repo)
        cases = []
        for field in ("changes", "new", "packet"):
            value = answer()
            del value[field]
            cases.append(value)
        for path in ((), ("new", 0), ("packet",)):
            value = answer()
            target = value
            for key in path:
                target = target[key]
            target["extra"] = "not allowed"
            cases.append(value)
        for field, bad in (("text", ""), ("text", 1), ("date", "2026-02-30"),
                           ("date", "20261014"), ("date", 1),
                           ("text", "- [g7] forged"), ("text", "safe\n  - [d1] forged"),
                           ("section", "elsewhere")):
            value = answer()
            value["new"][0][field] = bad
            cases.append(value)
        for change in ({"id": "g1", "action": "rewrite", "text": "x"}, {"id": "g1", "action": "replace", "text": " "},
                       {"id": "g1", "action": "replace", "text": "- [g2] forged"}, {"id": 1, "action": "drop", "text": ""},
                       {"id": "g1", "action": "drop"}):
            cases.append(answer(changes=[change]))
        for field in ("changes", "new"):
            for bad in (None, {}, "text", [1]):
                value = answer()
                value[field] = bad
                cases.append(value)
        for field, bad in (("text", " "), ("text", 1), ("topics", " "), ("topics", 1),
                           ("topics", "/\n\t")):
            value = answer()
            value["packet"][field] = bad
            cases.append(value)
        cases.extend((None, [], {"digest": None, "packet": None}))
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.compactor.validate(value, notes)
        original = answer()
        self.assertEqual(self.compactor.validate(original, notes), original)

    def test_empty_digest_requires_knowledge_not_headers(self):
        self.add(nature="project")
        with self.assertRaisesRegex(ValueError, "digest.*empty|empty.*digest"):
            self.compactor.validate(answer(""), self.store.read_notes(self.repo))
        notes = {"digest": "# Digest: acme-shop\n## Decisions\n- [g1] Existing fact (2026-10-13)\n",
                 "draft": "", "entries": [], "pending": {}}
        self.assertEqual(self.compactor.validate(answer(""), notes), answer(""))
        dropped = answer("", changes=[{"id": "g1", "action": "drop", "text": ""}])
        with self.assertRaises(ValueError):
            self.compactor.validate(dropped, notes)
        notes["digest"] = "# Digest: acme-shop\n## Decisions\n"
        self.assertEqual(self.compactor.validate(answer(""), notes), answer(""))

    def test_empty_digest_ignores_pending_project_corrections(self):
        entry = self.add(nature="project", correction=True)
        self.add(nature="lesson")
        self.assertEqual(self.compactor.validate(answer(""), self.store.read_notes(self.repo)), answer(""))
        self.store.confirm(self.repo, {entry["id"]: entry["text"]})
        with self.assertRaisesRegex(ValueError, "digest.*empty|empty.*digest"):
            self.compactor.validate(answer(""), self.store.read_notes(self.repo))

    def test_lessons_compact_to_empty_digest_with_pending_project_carried_over(self):
        entry = self.add(nature="project", correction=True)
        self.add(nature="lesson")
        self.fake.push(envelope(answer("")))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        notes = self.store.read_notes(self.repo)
        self.assertEqual(notes["pending"], {entry["id"]: entry["text"]})
        self.assertEqual([e["id"] for e in notes["entries"]], [entry["id"]])

    def test_recent_first_and_stable_ties(self):
        value = sections(in_progress=[{"text": "current work", "date": "2026-10-14"}],
                         decisions=[{"text": "old decision", "date": "2026-10-01"},
                                    {"text": "first tie", "date": "2026-10-14"},
                                    {"text": "second tie", "date": "2026-10-14"}])
        before = copy.deepcopy(value)
        text = self.compactor.render_digest(self.repo, value, 3.0)
        self.assertIn("- [g1] current work", text)
        self.assertIn("- [g2] first tie", text)
        self.assertIn("- [g3] second tie", text)
        self.assertIn("- [g4] old decision", text)
        self.assertEqual(value, before)

    def test_digest_over_cap_keeps_only_working_rules(self):
        value = sections(in_progress=[{"text": "x" * 9000, "date": "2026-10-14"}],
                         decisions=[{"text": "a decision", "date": "2026-10-14"}],
                         working_rules=[{"text": "never commit the key", "date": "2026-10-01"},
                                        {"text": "answer in French", "date": "2026-10-14"}])
        text = self.compactor.render_digest(self.repo, value, 3.0)
        self.assertLessEqual(self.store.estimate(text, 3.0), 3000)
        self.assertNotIn("a decision", text)
        self.assertIn("- [g1] answer in French", text)
        self.assertIn("- [g2] never commit the key", text)

    def test_rules_alone_over_cap_lose_oldest_first(self):
        value = sections(working_rules=[{"text": "old rule " + "x" * 9000, "date": "2026-10-01"},
                                        {"text": "new rule", "date": "2026-10-14"}])
        text = self.compactor.render_digest(self.repo, value, 3.0)
        self.assertLessEqual(self.store.estimate(text, 3.0), 3000)
        self.assertNotIn("old rule", text)
        self.assertIn("- [g1] new rule", text)

    def test_unmentioned_lines_kept_word_for_word(self):
        digest = ("# Digest: acme-shop\n## In progress\n- [g1] Checkout page (2026-10-12)\n"
                  "## Decisions\n- [g2] GitHub Pages: the client already has an account, zero server upkeep (2026-10-10)\n"
                  "- [g3] Mondial Relay delivery (2026-10-11)\n## Working rules\n- [g4] Answer in French (2026-10-09)\n")
        value = answer("Stripe payments", changes=[{"id": "g1", "action": "drop", "text": ""},
                                                   {"id": "g3", "action": "replace", "text": "Colissimo delivery"},
                                                   {"id": "g99", "action": "drop", "text": ""}])
        result = self.compactor.apply(digest, value)
        self.assertEqual(result["in_progress"], [])
        self.assertEqual(result["decisions"][0], {"text": "GitHub Pages: the client already has an account, zero server upkeep",
                                                  "date": "2026-10-10"})
        self.assertEqual([entry["text"] for entry in result["decisions"][1:]], ["Colissimo delivery", "Stripe payments"])
        self.assertEqual(result["working_rules"], [{"text": "Answer in French", "date": "2026-10-09"}])

    def test_packet_names_use_time_replace_quotes_and_drop_controls(self):
        now = datetime(2026, 10, 14, 15, 32, tzinfo=timezone.utc)
        self.assertEqual(self.compactor.packet_name(self.repo, '"header" gr\x01\x80id', now),
                         "acme-shop 2026-10-14 15h32 'header' grid.txt")
        for topics, expected in (("bad/path grid", "bad path grid"), ("bad\\path grid", "bad path grid"),
                                 ("header\n grid", "header grid"), ("header\0 grid", "header grid"),
                                 ("stripe payment decision and jev update", "stripe payment decision and")):
            with self.subTest(topics=topics):
                self.assertEqual(self.compactor.packet_name(self.repo, topics, now),
                                 "acme-shop 2026-10-14 15h32 " + expected + ".txt")

    def test_skip_empty_header_pending_status_and_below_threshold(self):
        directory = self.store.repo_dir(self.repo)
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.store.write_text(directory / "draft.md", "# Draft: acme-shop\n\n")
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        entry = self.add(correction=True)
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.store.append_text(directory / "draft.md", "- [d99] confirmed\n")
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.store.confirm(self.repo, {entry["id"]: entry["text"]})
        self.assertFalse(self.compactor.compact(self.repo, self.config))
        self.assertEqual(self.fake.requests, [])

    def test_exact_threshold_compacts_without_force(self):
        self.add()
        notes = self.store.read_notes(self.repo)
        self.store.append_text(self.store.repo_dir(self.repo) / "draft.md",
                               "x" * (23999 - len(notes["digest"] + notes["draft"])))
        self.assertEqual(self.store.estimate("".join(self.store.read_notes(self.repo)[key]
                                                   for key in ("digest", "draft")), 3.0), 8000)
        self.fake.push(envelope(answer()))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config))

    def test_paused_jev_and_compactor_make_no_calls(self):
        self.add()
        before = self.store.read_notes(self.repo)
        for name in ("jev", "compactor"):
            self.pause(name)
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
            self.breaker.resume(name)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.store.read_notes(self.repo), before)

    def test_request_payload_default_custom_config_schema_and_redaction(self):
        self.add("PASSWORD=invented-input-secret")
        notes = self.store.read_notes(self.repo)
        for base in ("https://openrouter.ai/api/v1", self.fake.url + "/custom/"):
            config = dict(self.config, MEMSIFT_COMPACTOR_URL=base,
                          MEMSIFT_COMPACTOR_MODEL="invented-custom-model",
                          MEMSIFT_COMPACTOR_API_KEY="invented-custom-key")
            self.fake.push(envelope(answer()))
            self.assertEqual(self.compactor.request(config, notes), answer())
            request = self.fake.requests[-1]
            self.assertEqual(request["url"], base.rstrip("/") + "/chat/completions")
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["headers"]["Authorization"], "Bearer invented-custom-key")
            body = request["body"]
            self.assertEqual(body["model"], "invented-custom-model")
            self.assertEqual(body["temperature"], 0.2)
            self.assertEqual(body["messages"][1]["content"], notes["digest"] + "\n\n" + notes["draft"])
            instructions = body["messages"][0]["content"]
            self.assertIn("Keep the user's words", instructions)
            self.assertIn("Lessons never go to the digest.", instructions)
            self.assertIn("only the new draft entries", instructions)
            self.assertIn("Do not rewrite the digest.", instructions)
            self.assertIn("never the old digest again", instructions)
            self.assertNotIn("invented-input-secret", repr(body))
            response = body["response_format"]
            self.assertEqual(response["type"], "json_schema")
            self.assertIs(response["json_schema"]["strict"], True)
            def check_objects(schema):
                if schema.get("type") == "object":
                    self.assertIs(schema["additionalProperties"], False)
                    self.assertEqual(set(schema["required"]), set(schema["properties"]))
                    for child in schema["properties"].values():
                        check_objects(child)
                if "items" in schema:
                    check_objects(schema["items"])
            check_objects(response["json_schema"]["schema"])
        self.assertEqual(self.service()["failures"], 0)
        self.assertFalse((self.store.home() / "state.json").exists())

    def test_invalid_json_retries_same_request_then_commits_once(self):
        self.add()
        self.fake.push({"choices": [{"message": {"content": "not JSON"}}]})
        self.fake.push(envelope(answer()))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.fake.requests[0]["body"], self.fake.requests[1]["body"])
        self.assertEqual(len(self.fake.requests), 3)
        self.assertEqual(self.service()["failures"], 0)
        self.assertIn("Traceback", self.log())
        self.assertNotIn("Not memory", self.store.read_notes(self.repo)["digest"])

    def test_empty_digest_twice_refused_and_logged(self):
        self.add()
        before = self.store.read_notes(self.repo)
        for _ in range(2):
            self.fake.push(envelope(answer("")))
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.store.read_notes(self.repo), before)
        self.assertEqual(self.service()["failures"], 1)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("empty", self.log())
        self.assertIn("Traceback", self.log())

    def test_schema_refusal_logs_http_status_and_redacted_response_excerpt(self):
        self.add()
        invalid = {"marker": "invalid-schema-marker PASSWORD=invented-schema-secret", "digest": {}}
        for _ in range(2):
            self.fake.push(envelope(invalid))
        self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.service()["failures"], 1)
        self.assertIn("compactor · HTTP 200", self.log())
        self.assertIn("invalid-schema-marker", self.log())
        self.assertNotIn("invented-schema-secret", self.log())
        self.assertIn("Traceback", self.log())

    def test_slow_body_failure_keeps_redacted_partial_diagnostics(self):
        self.fake.push_chunks([b'{"partial":"PASSWORD=invented-prefix-secret",'] + [b" "] * 10,
                              interval=0.02)
        with patch("memsift.compactor.HTTP_TIMEOUT", 0.05), self.assertRaises(self.breaker.ServiceError) as failure:
            self.compactor.request(self.config, {"digest": "", "draft": "", "entries": []})
        for thread in threading.enumerate():
            if thread.name == "memsift-compactor-http":
                thread.join(1)
        self.assertEqual(failure.exception.status, 200)
        self.assertIn("partial", failure.exception.response)
        self.assertNotIn("invented-prefix-secret", failure.exception.response)
        self.assertIn("partial", self.log())
        self.assertNotIn("invented-prefix-secret", self.log())
        self.assertIn("Traceback", self.log())

    def test_lessons_only_allow_empty_digest_and_packet(self):
        self.add("Build needs sharp.", nature="lesson")
        self.fake.push(envelope(answer("")))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        notes = self.store.read_notes(self.repo)
        self.assertEqual(notes["entries"], [])
        self.assertIn(b"Build needs sharp.", self.fake.requests[1]["body"])
        self.assertNotIn("sharp", notes["digest"])

    def test_old_digest_not_reuploaded_in_known_packet_fixture(self):
        self.add("Header uses footer grid.")
        self.store.write_text(self.store.repo_dir(self.repo) / "digest.md",
                              "# Digest: acme-shop\n## Decisions\n- [g1] Stripe payments (2026-10-13)\n")
        self.fake.push(envelope(answer("Header uses footer grid.")))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertIn("Stripe payments", self.store.read_notes(self.repo)["digest"])
        self.assertNotIn(b"Stripe payments", self.fake.requests[1]["body"])

    def test_transport_statuses_not_retried_and_counted_separately(self):
        self.add()
        before = self.store.read_notes(self.repo)
        for status, failures, paused in ((401, 1, True), (403, 1, True), (429, 0, False),
                                          (529, 0, False), (400, 1, False), (500, 1, False)):
            with self.subTest(status=status):
                self.breaker.resume("compactor")
                count = len(self.fake.requests)
                self.fake.push({"error": "PASSWORD=invented-response-secret"}, status=status)
                self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
                self.assertEqual(len(self.fake.requests), count + 1)
                self.assertEqual(self.service()["failures"], failures)
                self.assertEqual(self.service()["paused"], paused)
                self.assertEqual(self.store.read_notes(self.repo), before)
                self.assertIn("compactor · HTTP " + str(status), self.log())
                self.assertNotIn("invented-response-secret", self.log())
                self.assertIn("Traceback", self.log())

    def test_opener_fault_is_logged_without_validation_retry(self):
        self.add()
        self.fake.push_error(OSError("invented opener failure"))
        with patch("memsift.transport.open_request", side_effect=self.fake.open):
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.service()["failures"], 1)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("invented opener failure", self.log())
        self.assertIn("Traceback", self.log())

    def test_invalid_endpoint_is_transport_failure_without_content_retry(self):
        self.add()
        config = dict(self.config, MEMSIFT_COMPACTOR_URL="invalid endpoint")
        request = self.compactor.request
        with patch("memsift.compactor.request", wraps=request) as call:
            self.assertFalse(self.compactor.compact(self.repo, config, force=True))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.service()["failures"], 1)
        self.assertIn("Traceback", self.log())

    def test_multiline_digest_text_remains_one_dated_selectable_entry(self):
        value = answer("Header grid\nshares twelve columns.")
        rendered = self.compactor.render_digest(self.repo, self.compactor.apply("", value), 3.0)
        self.store.write_text(self.store.repo_dir(self.repo) / "digest.md", rendered)
        notes = self.store.read_notes(self.repo)
        self.assertEqual(len(notes["entries"]), 1)
        self.assertEqual(notes["entries"][0]["date"], "2026-10-14")
        self.assertIn("Header grid shares twelve columns. (2026-10-14)", rendered)

    def test_malformed_response_envelopes_and_json_are_retried_once(self):
        self.add()
        before = self.store.read_notes(self.repo)
        for value in ("not JSON", [], {}, {"choices": []}, {"choices": [None]},
                      {"choices": [{"message": None}]}, {"choices": [{"message": {"content": {}}}]}):
            with self.subTest(value=value):
                self.breaker.resume("compactor")
                count = len(self.fake.requests)
                for _ in range(2):
                    self.fake.push(value)
                self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
                self.assertEqual(len(self.fake.requests), count + 2)
                self.assertEqual(self.service()["failures"], 1)
                self.assertEqual(self.store.read_notes(self.repo), before)
                self.assertIn("Traceback", self.log())

    def test_pause_race_in_request_logs_and_never_counts(self):
        self.add()
        with patch("memsift.compactor.request", side_effect=self.breaker.PausedError("compactor")):
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.service()["failures"], 0)
        self.pause("compactor")
        before = self.breaker.state()
        with self.assertRaises(self.breaker.PausedError):
            self.compactor.request(self.config, self.store.read_notes(self.repo))
        self.assertEqual(self.breaker.state(), before)
        self.assertEqual(self.fake.requests, [])
        self.assertIn("Traceback", self.log())

    def test_outbox_exception_after_commit_preserves_compactor_success(self):
        self.add()
        self.fake.push(envelope(answer()))
        with patch("memsift.compactor.cognee.send_outbox", side_effect=OSError("invented drain fault")):
            self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.service()["failures"], 0)
        self.assertEqual(len(self.packets()), 1)
        self.assertNotIn("- [d", self.store.read_notes(self.repo)["draft"])
        self.assertIn("invented drain fault", self.log())
        self.assertIn("Traceback", self.log())

    def test_accounting_write_failure_logs_and_returns_commit_result(self):
        self.pause("cognee")
        self.add()
        self.fake.push(envelope(answer()))
        with patch("memsift.compactor.breaker.record", side_effect=OSError("invented accounting fault")):
            self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertNotIn("- [d", self.store.read_notes(self.repo)["draft"])
        self.assertEqual(len(self.packets()), 1)
        self.add()
        for _ in range(2):
            self.fake.push(envelope({"digest": {}}))
        with patch("memsift.compactor.breaker.record", side_effect=OSError("invented accounting fault")):
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertIn("invented accounting fault", self.log())
        self.assertIn("Traceback", self.log())

    def test_probe_transport_error_preserves_state_and_does_not_retry(self):
        self.pause("compactor")
        before = self.breaker.state()
        self.fake.push({"error": "down"}, status=503)
        with self.assertRaises(self.breaker.ServiceError) as failure:
            self.compactor.probe(self.config)
        self.assertEqual(failure.exception.status, 503)
        self.assertIsNotNone(failure.exception.__cause__)
        self.assertEqual(self.breaker.state(), before)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("Traceback", self.log())

    def test_snapshot_lock_timeout_logs_without_model_or_notes_changes(self):
        from memsift.vendor.file_lock import file_lock
        self.add()
        before = self.store.read_notes(self.repo)
        with file_lock(self.store.repo_dir(self.repo) / "draft.md.lock") as acquired:
            self.assertTrue(acquired)
            with patch("memsift.store.LOCK_WAIT", 0.02):
                self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.store.read_notes(self.repo), before)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.service()["failures"], 0)
        self.assertIn("Storage file lock unavailable", self.log())
        self.assertIn("Traceback", self.log())

    def test_generated_secrets_redacted_in_notes_packet_name_and_upload(self):
        self.add("PASSWORD=invented-input-secret")
        value = answer("API_KEY=invented-digest-secret")
        value["packet"] = {"topics": "PASSWORD=invented-name-secret grid",
                           "text": "PASSWORD=invented-packet-secret"}
        self.fake.push(envelope(value))
        self.fake.push({"ok": True})
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        secrets = ("invented-input-secret", "invented-digest-secret", "invented-name-secret", "invented-packet-secret")
        for path in self.store.home().rglob("*"):
            if path.is_file():
                for secret in secrets:
                    self.assertNotIn(secret, path.name + path.read_text())
        for request in self.fake.requests:
            for secret in secrets:
                self.assertNotIn(secret, repr(request["body"]))

    def test_request_deadline_bounds_slow_headers_and_body(self):
        self.add()
        for body in (False, True):
            with self.subTest(body=body):
                if body:
                    self.fake.push_chunks([b"{"] + [b" "] * 15 + [b"}"], interval=0.03)
                else:
                    self.fake.push(envelope(answer()), delay=0.3)
                began = time.monotonic()
                with patch("memsift.compactor.HTTP_TIMEOUT", 0.08), self.assertRaises(self.breaker.ServiceError):
                    self.compactor.request(self.config, self.store.read_notes(self.repo))
                self.assertLess(time.monotonic() - began, 0.25)
                for thread in threading.enumerate():
                    if thread.name == "memsift-compactor-http":
                        thread.join(1)
        self.assertEqual(self.service()["failures"], 0)
        self.assertIn("Traceback", self.log())

    def test_cognee_paused_or_down_keeps_packet_and_compactor_success(self):
        self.pause("cognee")
        for down in (False, True):
            with self.subTest(down=down):
                self.add()
                if down:
                    self.breaker.resume("cognee")
                self.fake.push(envelope(answer()))
                if down:
                    self.fake.push({"error": "down"}, status=500)
                self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
                self.assertEqual(self.service()["failures"], 0)
                self.assertEqual(len(self.packets()), 1 if not down else 2)
        self.assertEqual(self.service("cognee")["failures"], 1)

    def test_sync_interleaving_preserves_tail_correction_and_confirmation(self):
        self.add()
        correction = self.add("No, twelve columns.", correction=True)
        started, release = threading.Event(), threading.Event()
        opener = self.fake.open
        def blocked(request, timeout):
            response = opener(request, timeout)
            started.set()
            if not release.wait(2):
                response.close()
                raise TimeoutError("test response synchronization expired")
            return response
        self.pause("cognee")
        self.fake.push(envelope(answer()))
        results = []
        with patch("memsift.transport.open_request", side_effect=blocked):
            worker = threading.Thread(target=lambda: results.append(self.compactor.compact(self.repo, self.config, force=True)))
            worker.start()
            try:
                self.assertTrue(started.wait(1))
                self.add("Also keep the checkout flow.")
                self.assertEqual(self.store.confirm(self.repo, {correction["id"]: correction["text"]}), ["d2"])
                self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
                self.assertEqual(len(self.fake.requests), 1)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results, [True])
        notes = self.store.read_notes(self.repo)
        self.assertIn(correction["text"], notes["draft"])
        self.assertIn("- [d2] confirmed", notes["draft"])
        self.assertIn("Also keep the checkout flow.", notes["draft"])
        self.assertNotIn("Keep the footer grid.", notes["draft"])
        self.assertEqual(notes["pending"], {})
        self.assertEqual(self.store.confirm(self.repo, {"d1": "stale entry text"}), [])
        self.fake.push(envelope(answer("Twelve columns and checkout flow.")))
        self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.store.read_notes(self.repo)["entries"][0]["id"], "g1")
        self.assertNotIn("- [d", self.store.read_notes(self.repo)["draft"])

    def test_staging_failure_keeps_notes_unchanged(self):
        self.add()
        before = self.store.read_notes(self.repo)
        self.fake.push(envelope(answer()))
        with patch("memsift.cognee.os.fsync", side_effect=OSError("invented fsync failure")):
            self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual(self.store.read_notes(self.repo), before)
        self.assertEqual(self.packets(), [])
        self.assertIn("invented fsync failure", self.log())
        self.assertIn("Traceback", self.log())

    def test_crashes_after_staging_digest_and_draft_keep_durable_packet(self):
        for stage in ("staging", "digest", "draft"):
            with self.subTest(stage=stage):
                self.breaker.resume("compactor")
                self.add()
                before = self.store.read_notes(self.repo)
                replace = self.store.replace_notes
                def fail(root, snapshot, digest):
                    self.assertTrue(self.packets())
                    if stage == "digest":
                        self.store.write_text(self.store.repo_dir(root) / "digest.md", digest)
                    elif stage == "draft":
                        replace(root, snapshot, digest)
                    raise OSError("invented crash after " + stage)
                self.fake.push(envelope(answer()))
                with patch("memsift.compactor.store.replace_notes", side_effect=fail):
                    committed = self.compactor.compact(self.repo, self.config, force=True)
                self.assertFalse(committed)
                self.assertEqual(len(self.packets()), 1)
                if stage == "staging":
                    self.assertEqual(self.store.read_notes(self.repo), before)
                elif stage == "digest":
                    self.assertEqual(self.store.read_notes(self.repo)["draft"], before["draft"])
                else:
                    self.assertNotIn("- [d", self.store.read_notes(self.repo)["draft"])
                self.assertIn("invented crash after " + stage, self.log())
                self.assertIn("Traceback", self.log())
                self.fake.push({"ok": True})
                self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 1)
                self.assertEqual(self.packets(), [])
                if stage != "draft":
                    self.fake.push(envelope(answer()))
                    self.fake.push({"ok": True})
                    self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
                    self.assertNotIn("- [d", self.store.read_notes(self.repo)["draft"])
                else:
                    count = len(self.fake.requests)
                    self.assertFalse(self.compactor.compact(self.repo, self.config, force=True))
                    self.assertEqual(len(self.fake.requests), count)

    def test_staging_order_fsync_collision_and_no_overwrite(self):
        self.pause("cognee")
        now = datetime(2026, 10, 14, 15, 32, tzinfo=timezone.utc)
        name = "acme-shop 2026-10-14 15h32 'header' grid.txt"
        original = self.cognee.queue_packet(self.repo, name, "original packet")
        self.add()
        value = answer()
        value["packet"]["topics"] = '"header" grid'
        self.fake.push(envelope(value))
        replace = self.store.replace_notes
        def checked(root, snapshot, digest):
            self.assertEqual(len(self.packets()), 2)
            self.assertEqual(original.read_text(), "original packet")
            replace(root, snapshot, digest)
        with patch("memsift.compactor.datetime") as clock, patch("memsift.compactor.store.replace_notes", side_effect=checked):
            clock.now.return_value = now
            self.assertTrue(self.compactor.compact(self.repo, self.config, force=True))
        self.assertEqual({path.name for path in self.packets()}, {name, name[:-4] + " (2).txt"})

    def test_probe_bypasses_pauses_without_persistence_or_counter_changes(self):
        for name in ("jev", "compactor", "cognee"):
            self.pause(name)
        before = self.breaker.state()
        self.fake.push(envelope(answer("")))
        self.assertIsNone(self.compactor.probe(self.config))
        self.assertEqual(self.breaker.state(), before)
        self.assertFalse((self.store.home() / "repos").exists())
        self.assertEqual(len(self.fake.requests), 1)
        self.assertLess(len(self.fake.requests[0]["body"]["messages"][1]["content"]), 200)
        self.assertIn("acme-shop", self.fake.requests[0]["body"]["messages"][1]["content"])
        self.fake.push(envelope({"digest": {}}))
        with self.assertRaises(ValueError):
            self.compactor.probe(self.config)
        self.assertEqual(self.breaker.state(), before)
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn("Traceback", self.log())
