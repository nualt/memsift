"""Host fixtures, bounded transcript reads and prompt budgets."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_host_contracts import FIXTURES, load_fixture


class TranscriptTests(IsolatedTest):
    def test_fixture_text_boundaries_and_action_matching(self):
        from memsift import host
        contract = json.loads((FIXTURES / "hooks/contract.json").read_text())
        for name, case in contract["transcripts"].items():
            with self.subTest(name=name):
                value = host.read_conversation(FIXTURES / "transcripts" / name,
                                               name.split("-plain")[0].split("-compacted")[0],
                                               case["actions"], 3.0)
                self.assertEqual(value["summary"], "")
                users = [e for e in value["exchanges"] if e["role"] == "user"]
                self.assertEqual(users[-1]["text"], case["expected"]["recent_user"])
                self.assertEqual(users[-1]["actions"], [])
                for excluded in case["exclusions"]:
                    self.assertNotIn(excluded, repr(value))
                for group in case["actions"][:2] + case["actions"][3:4]:
                    matched = [e for e in users if e["text"].startswith(group["prompt"])]
                    self.assertEqual(matched[-1]["actions"], group["items"])
                repeats = [e for e in users if e["text"] == case["repeated_prompt"]]
                self.assertEqual([e["actions"] for e in repeats], [[], case["actions"][2]["items"]])
                self.assertIn(case["expected"]["assistant_text"], repr(value))
                self.assertIn(case["expected"]["retraction_before_commit"], repr(value))

    def test_every_hook_fixture_and_output_envelope(self):
        from memsift import host
        contract = json.loads((FIXTURES / "hooks/contract.json").read_text())
        mapping = {"SessionStart": "start", "UserPromptSubmit": "prompt",
                   "PostToolUse": "tool", "PostToolUseFailure": "tool",
                   "Stop": "stop", "SessionEnd": "end"}
        for name, spec in contract["hosts"].items():
            transcript = self.home / (name + ".jsonl")
            transcript.write_text(load_fixture(FIXTURES / "transcripts" / (name + "-plain.jsonl"),
                                               self.repo, transcript))
            for fixture, fields in spec["fixtures"].items():
                with self.subTest(host=name, fixture=fixture):
                    payload = json.loads(load_fixture(FIXTURES / "hooks" / name / (fixture + ".json"),
                                                       self.repo, transcript))
                    event = host.normalize(fields["event"], payload, name)
                    self.assertEqual(event["event"], mapping[fields["event"]])
                    self.assertEqual(event["host"], name)
                    self.assertEqual(event["cwd"], str(self.repo))
                    self.assertEqual(event["transcript_path"], str(transcript))
                    self.assertEqual(event["source"], "resume" if payload.get("source") == "fork"
                                     else payload.get("source", ""))
                    self.assertEqual(event["answer"], payload.get("last_assistant_message", ""))
                    self.assertNotIn("DO_NOT_SEND_TOOL_OUTPUT", repr(event))
                    self.assertEqual(host.render(event, ""), "")
                    if event["event"] == "prompt":
                        self.assertEqual(json.loads(host.render(event, "acme-shop context")),
                                         {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                          "additionalContext": "acme-shop context"}})
                    else:
                        self.assertEqual(host.render(event, "acme-shop context"), "")
                    if fixture.startswith("tool-bash"):
                        expected = 1 if fixture.endswith("failed") else (0 if name == "codex" else None)
                        self.assertEqual(event["tool"]["exit_code"], expected)

    def test_unknown_host_and_event_are_rejected(self):
        from memsift import host
        for event, name in (("Stop", "other"), ("PreCompact", "codex")):
            with self.subTest(event=event, host=name), self.assertRaises(ValueError):
                host.normalize(event, {}, name)

    def write_rows(self, rows, ending=b"\n"):
        path = self.home / "transcript.jsonl"
        path.write_bytes(b"\n".join(json.dumps(r, ensure_ascii=False).encode() for r in rows) + ending)
        return path

    def user(self, text):
        return {"type": "user", "message": {"role": "user", "content": text}}

    def test_partial_final_record_and_malformed_complete_record_log_and_keep_valid_text(self):
        from memsift import host
        path = self.write_rows([self.user("acme-shop café")])
        with path.open("ab") as stream:
            stream.write(b'bad record\n{"type": "user", "message":')
        value = host.read_conversation(path, "claude-code", [], 3.0)
        self.assertEqual(value["exchanges"][0]["text"], "acme-shop café")
        log = (self.home / ".memsift/debug.log").read_text()
        self.assertGreaterEqual(log.count("Traceback"), 2)
        self.assertIn("JSONDecodeError", log)

    def test_valid_final_record_without_newline(self):
        from memsift import host
        path = self.write_rows([self.user("acme-shop café")], b"")
        self.assertEqual(host.read_conversation(path, "claude-code", [], 3.0)["exchanges"][0]["text"],
                         "acme-shop café")

    def test_long_latest_answer_keeps_paired_user_and_bounded_actions(self):
        from memsift import host
        path = self.write_rows([self.user("acme-shop latest prompt"),
                                {"type": "assistant", "message": {
                                    "content": "answer start " + "a" * 30000 + " answer end"}}])
        value = host.read_conversation(path, "claude-code", [], 3.0)
        self.assertEqual([e["role"] for e in value["exchanges"]], ["user", "assistant"])
        self.assertEqual(value["exchanges"][0]["text"], "acme-shop latest prompt")
        self.assertIn("answer end", value["exchanges"][1]["text"])
        self.assertLessEqual(len(json.dumps(value["exchanges"], ensure_ascii=False)), 75000)
        actions = [{"turn": 1, "prompt": "acme-shop latest prompt", "items": ["Edit x: " + "x" * 190] * 20}]
        value = host.read_conversation(path, "claude-code", actions, 3.0)
        self.assertEqual(value["exchanges"][0]["actions"], actions[0]["items"])
        self.assertLessEqual(len(json.dumps(value["exchanges"], ensure_ascii=False)), 75000)

    def test_malformed_nested_shapes_never_become_text(self):
        from memsift import host
        path = self.write_rows([self.user("acme-shop valid"),
                                {"type": "user", "message": "DO_NOT_SEND_TOOL_OUTPUT"},
                                {"type": "response_item", "payload": "DO_NOT_SEND_TOOL_OUTPUT"}])
        self.assertEqual(host.read_conversation(path, "claude-code", [], 3.0)["exchanges"],
                         [{"role": "user", "text": "acme-shop valid", "actions": []}])

    def test_codex_missing_exit_status_is_not_inferred(self):
        from memsift import host
        payload = {"tool_name": "Bash", "tool_input": {"command": "check"},
                   "tool_use_id": "absent", "transcript_path": str(self.home / "absent.jsonl")}
        self.assertIsNone(host.normalize("PostToolUse", payload, "codex")["tool"]["exit_code"])

    def test_missing_empty_and_read_errors(self):
        from memsift import host
        path = self.home / "transcript.jsonl"
        empty = {"summary": "", "exchanges": []}
        self.assertEqual(host.read_conversation(path, "codex", [], 3.0), empty)
        self.assertFalse(path.exists())
        path.touch()
        self.assertEqual(host.read_conversation(path, "codex", [], 3.0), empty)
        for fault in (PermissionError("injected read denial"), FileNotFoundError("injected race")):
            with patch.object(Path, "open", side_effect=fault):
                self.assertEqual(host.read_conversation(path, "codex", [], 3.0), empty)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())

    def test_invalid_utf8_and_json_shape_are_logged(self):
        from memsift import host
        path = self.write_rows([self.user("acme-shop valid")])
        with path.open("ab") as stream:
            stream.write(b'\xff\n[]\n')
        value = host.read_conversation(path, "claude-code", [], 3.0)
        self.assertEqual(value["exchanges"][0]["text"], "acme-shop valid")
        self.assertIn("UnicodeDecodeError", (self.home / ".memsift/debug.log").read_text())

    def test_multi_megabyte_reverse_reads_stop_when_recent_budget_is_full(self):
        from memsift import host
        summary = self.user("acme-shop latest summary")
        summary["isCompactSummary"] = True
        rows = [{"type": "system", "subtype": "compact_boundary"}, summary]
        rows += [self.user("acme-shop old " + "x" * 1500)] * 2000
        rows += [self.user("acme-shop newest café")]
        path = self.write_rows(rows)
        actual_open = Path.open
        reads = []

        class CheckedReader:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def seek(self, *args):
                return self.stream.seek(*args)
            def read(self, size=-1):
                self_test.assertTrue(0 < size <= 65536)
                reads.append(size)
                return self.stream.read(size)

        self_test = self
        def checked_open(p, *args, **kwargs):
            stream = actual_open(p, *args, **kwargs)
            return CheckedReader(stream) if p == path else stream

        with patch.object(Path, "open", checked_open):
            value = host.read_conversation(path, "claude-code", [], 3.0)
        self.assertEqual(value["summary"], "")
        self.assertEqual(value["exchanges"][-1]["text"], "acme-shop newest café")
        self.assertLess(len(repr(value)), 78000)
        self.assertLess(len(reads), 3)

    def test_utf8_character_split_at_reverse_chunk_boundary(self):
        from memsift import host
        raw = json.dumps(self.user("acme-shop é"), ensure_ascii=False).encode()
        split = raw.index("é".encode()) + 1
        path = self.home / "transcript.jsonl"
        # The final padding record places the EOF chunk boundary inside é.
        pad = 65536 - (len(raw) - split) - 1
        path.write_bytes(raw + b"\n" + b" " * pad)
        value = host.read_conversation(path, "claude-code", [], 3.0)
        self.assertEqual(value["exchanges"][0]["text"], "acme-shop é")

    def test_actions_redact_excerpts_repeat_newest_first_and_hide_unretried_failure(self):
        from memsift import host
        text = "acme-shop PASSWORD=invented-password"
        path = self.write_rows([self.user(text), self.user(text), self.user("acme-shop next")])
        groups = [{"turn": 1, "prompt": text, "items": ["Edit first"]},
                  {"turn": 1, "prompt": text, "items": ["Edit second", "Bash: test (failed pending)"]}]
        value = host.read_conversation(path, "claude-code", groups, 3.0)
        self.assertEqual([e["actions"] for e in value["exchanges"]], [["Edit first"], ["Edit second"], []])
        self.assertNotIn("invented-password", repr(value))


    def test_last_compaction_marker_excludes_all_earlier_text(self):
        from memsift import host
        for name in ("claude-code", "codex"):
            def user(text):
                if name == "claude-code":
                    return self.user(text)
                return {"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}
            boundary = ({"type": "system", "content": "Conversation compacted",
                         "subtype": "compact_boundary"} if name == "claude-code" else
                        {"type": "compacted", "payload": {"message": "excluded summary",
                         "replacement_history": [dict(user("excluded clear user").get("payload", {}))]}})
            path = self.write_rows([user("excluded oldest"), boundary, user("excluded middle"),
                                    boundary, user("newest")])
            self.assertEqual(host.read_conversation(path, name, [], 3),
                             {"summary": "", "exchanges": [{"role": "user", "text": "newest", "actions": []}]})

    def test_huge_single_record_aborts_in_bounded_time(self):
        import time
        from memsift import host
        path = self.home / "transcript.jsonl"
        with path.open("wb") as stream:
            stream.write(b'{"type":"user","message":{"content":[{"type":"tool_result","content":"')
            for _ in range(96):
                stream.write(b"x" * 1024 * 1024)
            stream.write(b'"}]}}\n')
            stream.write(json.dumps(self.user("newest")).encode() + b"\n")
        started = time.monotonic()
        with self.assertRaises(ValueError):
            host.read_conversation(path, "claude-code", [], 3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())

    def test_conversation_uses_25000_tokens_newest_first_and_stops_scanning(self):
        from memsift import host, store
        rows = [self.user("old" + "x" * 1000) for _ in range(2000)]
        rows += [self.user("recent" + "y" * 1000) for _ in range(30)]
        rows += [self.user("newest")]
        path = self.write_rows(rows)
        with patch("memsift.host._message", wraps=host._message) as messages:
            value = host.read_conversation(path, "claude-code", [], 2)
        self.assertLess(messages.call_count, 60)
        self.assertEqual(value["exchanges"][-1]["text"], "newest")
        estimate = store.estimate(json.dumps(value["exchanges"], ensure_ascii=False), 2)
        self.assertGreater(estimate, 24000)
        self.assertLessEqual(estimate, 25000)


    def test_oversized_latest_answer_keeps_newest_text_with_json_escaping(self):
        from memsift import host, store
        path = self.write_rows([self.user("old user"), {"type": "assistant", "message": {
            "content": '"' * 100000 + "latest answer tail"}}])
        value = host.read_conversation(path, "claude-code", [], 3)
        self.assertTrue(value["exchanges"])
        self.assertTrue(value["exchanges"][-1]["text"].endswith("latest answer tail"))
        self.assertLessEqual(store.estimate(json.dumps(value["exchanges"], ensure_ascii=False), 3), 25000)

    def test_transcript_read_deadline_is_logged(self):
        import time
        from memsift import host
        path = self.write_rows([self.user("acme-shop")])
        clock = time.monotonic
        started = clock()
        with patch("memsift.host.time") as clock:
            clock.monotonic.side_effect = [started, started + 17]
            with self.assertRaises(TimeoutError):
                host.read_conversation(path, "claude-code", [], 3)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())


class PromptBudgetTests(IsolatedTest):
    def test_fit_copies_redacts_and_preserves_prompt(self):
        from memsift import host
        state = {"prompt": "Back to the grid", "conversation": [
            {"role": "user", "text": "PASSWORD=invented-password", "actions": []}]}
        before = copy.deepcopy(state)
        value = host.fit_prompt(state, {"has_info": {"instructions": "Question?"}}, 3)
        self.assertEqual(state, before)
        self.assertNotIn("invented-password", repr(value))
        self.assertEqual(value["prompt"], state["prompt"])

    def test_oldest_exchanges_are_cut_first_at_25000_tokens(self):
        from memsift import host
        state = {"prompt": "p" * 10000, "conversation": [
            {"role": "user", "text": "old" + "x" * 12000, "actions": []},
            {"role": "user", "text": "new" + "x" * 12000, "actions": []}]}
        value = host.fit_prompt(state, {"has_info": {"instructions": "Question?"}}, 1)
        self.assertEqual(value["conversation"], state["conversation"][1:])
        self.assertEqual(value["prompt"], state["prompt"])
        self.assertEqual(len(state["conversation"]), 2)

    def test_prompt_is_never_cut_when_it_exceeds_the_request_budget(self):
        from memsift import host
        for state, questions in [
            ({"prompt": '"' * 50000}, {"has_info": "?"}),
            ({"prompt": "x" * 96000}, {"has_info": "?"}),
            ({"prompt": "grid"}, {"entry_d1": "x" * 96000}),
            ({"prompt": "grid"}, {str(i): "x" * 60000 for i in range(4)}),
        ]:
            with self.subTest(state_length=len(state["prompt"])):
                with self.assertRaises(ValueError):
                    host.fit_prompt(state, questions, 3)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())
