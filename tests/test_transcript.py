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
                self.assertEqual(value["summary"], case["expected"]["latest_summary"])
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
        self.assertLessEqual(len(json.dumps(value["exchanges"], ensure_ascii=False)), 15000)
        actions = [{"turn": 1, "prompt": "acme-shop latest prompt", "items": ["Edit x: " + "x" * 190] * 20}]
        value = host.read_conversation(path, "claude-code", actions, 3.0)
        self.assertEqual(value["exchanges"][0]["actions"], actions[0]["items"])
        self.assertLessEqual(len(json.dumps(value["exchanges"], ensure_ascii=False)), 15000)

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

    def test_multi_megabyte_reverse_reads_find_summary_after_recent_budget_is_full(self):
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
        self.assertEqual(value["summary"], "acme-shop latest summary")
        self.assertEqual(value["exchanges"][-1]["text"], "acme-shop newest café")
        self.assertLess(len(repr(value)), 18000)
        self.assertGreater(len(reads), 40)

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


class PromptBudgetTests(IsolatedTest):
    def state(self):
        return {"prompt": "acme-shop question", "project_card": "acme-shop card",
                "session_start": "new session", "compaction_summary": "",
                "recent_exchanges": [], "notes": {}, "datasets": {}}

    def test_fit_copies_and_redacts_without_changing_protected_fields(self):
        from memsift import host
        state = self.state()
        state["notes"] = {"d1": "PASSWORD=invented-password"}
        before = copy.deepcopy(state)
        value = host.fit_prompt(state, {"missing": {"instructions": "Question?"}}, 3.0)
        self.assertEqual(state, before)
        self.assertNotIn("invented-password", repr(value))
        for key in ("prompt", "project_card", "session_start"):
            self.assertEqual(value[key], state[key])

    def test_cut_order_preserves_digest_and_newest_draft(self):
        from memsift import host
        state = self.state()
        state.update(compaction_summary="s" * 18000,
                     recent_exchanges=[{"role": "user", "text": "old" + "x" * 12000, "actions": []},
                                       {"role": "user", "text": "new" + "x" * 12000, "actions": []}],
                     notes={"g1": "digest" + "x" * 23000, "d1": "old draft" + "x" * 23000,
                            "d2": "new draft" + "x" * 23000})
        # At ratio=1 all three cuts are needed to reach the actual 32,000 limit.
        value = host.fit_prompt(state, {"missing": {"instructions": "Question?"}}, 1.0)
        self.assertEqual(value["recent_exchanges"], [])
        self.assertEqual(value["compaction_summary"], "")
        self.assertEqual(value["notes"], {"g1": state["notes"]["g1"]})
        self.assertEqual(state["notes"]["d2"], "new draft" + "x" * 23000)

    def test_summary_end_cut_keeps_start_after_oldest_exchange_removed(self):
        from memsift import host
        state = self.state()
        state.update(compaction_summary="START" + "s" * 18000 + "END",
                     recent_exchanges=[{"role": "user", "text": "old" + "x" * 5000, "actions": []}],
                     notes={"g1": "digest" + "x" * 20000})
        value = host.fit_prompt(state, {"missing": "?"}, 1.0)
        self.assertEqual(value["recent_exchanges"], [])
        self.assertTrue(value["compaction_summary"].startswith("START"))
        self.assertFalse(value["compaction_summary"].endswith("END"))
        self.assertEqual(value["notes"], state["notes"])

    def test_string_recent_exchanges_drop_whole_oldest_turn(self):
        from memsift import host
        state = self.state()
        state["notes"] = {"g1": "g" * 31000}
        state["recent_exchanges"] = "User: old\nAgent: " + "a" * 2000 + "\nUser: new\nAgent: ready\n"
        value = host.fit_prompt(state, {"missing": "?"}, 1.0)
        self.assertEqual(value["recent_exchanges"], "User: new\nAgent: ready\n")

    def test_json_escaping_counts_toward_budget(self):
        from memsift import host
        state = self.state()
        state["prompt"] = '"' * 50000
        with self.assertRaises(ValueError):
            host.fit_prompt(state, {"missing": "?"}, 3.0)

    def test_protected_oversize_longest_question_and_total_request_are_rejected(self):
        from memsift import host
        for field in ("prompt", "project_card", "questions", "total_questions"):
            with self.subTest(field=field):
                state = self.state()
                questions = {"missing": "?"}
                if field == "questions":
                    questions = {"missing": "x" * 96000}
                elif field == "total_questions":
                    questions = {str(i): "x" * 60000 for i in range(4)}
                else:
                    state[field] = "x" * 96000
                with self.assertRaises(ValueError):
                    host.fit_prompt(state, questions, 3.0)
        self.assertIn("Traceback", (self.home / ".memsift/debug.log").read_text())
