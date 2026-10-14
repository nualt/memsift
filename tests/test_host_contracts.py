"""Sanitized host boundary fixtures for the shared host implementation."""

import json
from pathlib import Path
import unittest

from tests.support import IsolatedTest


FIXTURES = Path(__file__).parent / "fixtures"
EVENTS = ("start", "prompt", "tool-edit", "tool-write", "tool-bash-ok",
          "tool-bash-failed", "stop", "end")


def load_fixture(path, repo, transcript):
    """Expand fixture path tokens without interpreting source-code braces."""
    text = Path(path).read_text(encoding="utf-8")
    return text.replace("{repo}", json.dumps(str(repo))[1:-1]).replace(
        "{transcript}", json.dumps(str(transcript))[1:-1])


class HostContractTests(IsolatedTest):
    def contract(self):
        path = FIXTURES / "hooks" / "contract.json"
        self.assertTrue(path.is_file(), str(path))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_required_events_have_sanitized_fixtures(self):
        for host in ("claude-code", "codex"):
            events = EVENTS + (("tool-apply-patch",) if host == "codex" else
                               ("tool-multiedit", "tool-notebookedit", "tool-failure"))
            for event in events:
                with self.subTest(host=host, event=event):
                    path = FIXTURES / "hooks" / host / (event + ".json")
                    self.assertTrue(path.is_file(), str(path))
                    raw = path.read_text(encoding="utf-8")
                    self.assertIsInstance(json.loads(raw), dict)
                    self.assertNotIn("/Users/", raw)
                    self.assertNotIn("Bearer sk-", raw)

    def test_hook_fields_and_path_tokens_match_the_contract(self):
        for host, spec in self.contract()["hosts"].items():
            transcript = self.home / (host + ".jsonl")
            for name, event in spec["fixtures"].items():
                with self.subTest(host=host, fixture=name):
                    payload = json.loads(load_fixture(
                        FIXTURES / "hooks" / host / (name + ".json"),
                        self.repo, transcript))
                    self.assertEqual(payload["cwd"], str(self.repo))
                    self.assertEqual(payload["transcript_path"], str(transcript))
                    self.assertEqual(payload["hook_event_name"], event["event"])
                    for field, kind in spec["event_fields"][event["event"]].items():
                        self.assertIn(field, payload)
                        types = {"string": str, "boolean": bool, "integer": int,
                                 "object": dict, "array": list}
                        self.assertIsInstance(payload[field], types[kind])
                    self.assertNotIn("{repo}", json.dumps(payload))
                    self.assertNotIn("{transcript}", json.dumps(payload))

    def test_live_gates_are_distinct_from_captured_and_schema_evidence(self):
        hosts = self.contract()["hosts"]
        self.assertEqual(hosts["claude-code"]["version"], "2.1.299")
        codex = hosts["codex"]
        self.assertEqual(codex["version"], "0.162.1")
        self.assertEqual(codex["verified_events"], [
            "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"])
        self.assertEqual(codex["source_values"], [
            "startup", "resume", "clear", "compact", "fork"])
        self.assertEqual(codex["captured_sources"], ["startup"])
        self.assertEqual(codex["support"]["failed_commands"], "captured")
        self.assertEqual(codex["support"]["apply_patch"], "captured")
        self.assertEqual(codex["support"]["root_source"], "live_unverified")
        self.assertEqual(codex["commands"]["status"], "live_unverified")
        self.assertIsNone(codex["commands"]["registration"])
        self.assertEqual(codex["commands"]["names"], [
            "memsift-resume", "memsift-doctor"])
        for spec in hosts.values():
            self.assertEqual(spec["prompt_output"]["envelope"], {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit", "additionalContext": "{context}"}})
            self.assertEqual(spec["prompt_output"]["empty"], "")
            self.assertEqual(spec["prompt_output"]["evidence"], "schema")
            self.assertEqual(set(spec["sources"]), set(spec["source_values"]))
            for source, evidence in spec["sources"].items():
                self.assertEqual(evidence, "captured" if source == "startup" else "schema")

    def test_failed_bash_uses_the_real_host_exit_status_locations(self):
        root = FIXTURES / "hooks"
        claude = json.loads((root / "claude-code" / "tool-bash-failed.json").read_text())
        self.assertEqual(claude["hook_event_name"], "PostToolUseFailure")
        self.assertTrue(claude["error"].startswith("Exit code 1"))
        self.assertNotIn("tool_response", claude)
        codex = json.loads((root / "codex" / "tool-bash-failed.json").read_text())
        success = json.loads((root / "codex" / "tool-bash-ok.json").read_text())
        self.assertEqual(codex["hook_event_name"], "PostToolUse")
        self.assertIsInstance(codex["tool_response"], str)
        self.assertNotIn("exit_code", codex)
        self.assertEqual(codex["tool_input"]["command"], success["tool_input"]["command"])
        for variant in ("plain", "compacted"):
            rows = [json.loads(line) for line in (
                FIXTURES / "transcripts" / ("codex-" + variant + ".jsonl")
            ).read_text().splitlines()]
            results = {row["payload"]["item"]["id"]: row["payload"]["item"]
                       for row in rows if row["type"] == "event_msg"
                       and row["payload"].get("type") == "item_completed"
                       and row["payload"]["item"]["type"] == "CommandExecution"}
            self.assertEqual(results[codex["tool_use_id"]]["exit_code"], 1)
            self.assertEqual(results[codex["tool_use_id"]]["status"], "failed")
            self.assertEqual(results[success["tool_use_id"]]["exit_code"], 0)
            self.assertEqual(results[success["tool_use_id"]]["status"], "completed")

    def test_codex_edit_and_write_cases_use_native_patch_input(self):
        for name, operation in (("tool-edit", "Update"), ("tool-write", "Add")):
            payload = json.loads((FIXTURES / "hooks" / "codex" / (name + ".json")).read_text())
            self.assertEqual(payload["tool_name"], "apply_patch")
            command = payload["tool_input"]["command"]
            self.assertIn("*** " + operation + " File:", command)
            self.assertIn("*** End Patch", command)
            self.assertIsInstance(payload["tool_response"], str)

    def test_transcripts_keep_text_tools_and_compaction_cases_separate(self):
        contract = self.contract()
        for name, case in contract["transcripts"].items():
            with self.subTest(fixture=name):
                path = FIXTURES / "transcripts" / name
                self.assertTrue(path.is_file(), str(path))
                text = load_fixture(path, self.repo, self.home / "transcript.jsonl")
                rows = [json.loads(line) for line in text.splitlines()]
                self.assertIn("DO_NOT_SEND_TOOL_OUTPUT", text)
                self.assertIn(case["expected"]["recent_user"], text)
                self.assertIn("I retract the header correction; do not treat the next commit as confirmation.", text)
                self.assertIn("git commit", text)
                if name.startswith("claude-code"):
                    boundaries = [row for row in rows if row.get("subtype") == "compact_boundary"]
                    self.assertEqual(len(boundaries), case["compactions"])
                    blocks = [row["message"]["content"] for row in rows if row["type"] == "assistant"]
                    self.assertTrue(any(block == [] for block in blocks))
                    self.assertTrue(any(isinstance(block, list) and
                                        {item["type"] for item in block} >= {"text", "tool_use"}
                                        for block in blocks))
                    if boundaries:
                        last = rows.index(boundaries[-1])
                        self.assertTrue(rows[last + 1]["isCompactSummary"])
                        self.assertEqual(rows[last + 1]["message"]["content"],
                                         case["expected"]["latest_summary"])
                else:
                    boundaries = [row for row in rows if row["type"] == "compacted"]
                    self.assertEqual(len(boundaries), case["compactions"])
                    self.assertTrue(any(row["type"] == "response_item" and
                                        row["payload"].get("role") == "assistant" and
                                        row["payload"].get("content") == [] for row in rows))
                    if boundaries:
                        history = boundaries[-1]["payload"]["replacement_history"]
                        self.assertTrue(any(item["type"] == "compaction" and
                                            item["encrypted_content"] == "ENCRYPTED_SUMMARY"
                                            for item in history))
                        self.assertTrue(any(item.get("role") == "user" and
                                            item["content"][0]["text"] == case["expected"]["latest_summary"]
                                            for item in history))
                safe = json.dumps(case["expected"])
                for excluded in ("OLD_SUMMARY", "DO_NOT_SEND_TOOL_OUTPUT", "ENCRYPTED_SUMMARY"):
                    self.assertNotIn(excluded, safe)

    def test_action_groups_cover_resume_unmatched_and_repeated_excerpts(self):
        for name, case in self.contract()["transcripts"].items():
            with self.subTest(fixture=name):
                groups = case["actions"]
                self.assertEqual([group["turn"] for group in groups[:2]], [1, 1])
                self.assertTrue(all(0 < len(group["prompt"]) <= 200 for group in groups))
                text = (FIXTURES / "transcripts" / name).read_text()
                self.assertIn(groups[0]["prompt"], text)
                self.assertIn(groups[1]["prompt"], text)
                self.assertNotIn(case["unmatched_prompt"], text)
                self.assertGreaterEqual(text.count(case["repeated_prompt"]), 2)
                self.assertFalse(any(case["expected"]["recent_user"].startswith(group["prompt"])
                                     for group in groups))

    def test_jev_wire_request_uses_instructions_and_typed_criteria(self):
        jev = self.contract()["jev"]
        self.assertEqual(jev["endpoint"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(jev["evidence"], "official_documentation")
        request = jev["request"]
        self.assertEqual(set(request), {"model", "state", "questions"})
        self.assertEqual(request["model"], "typesafe/jev-1.13")
        questions = request["questions"]
        self.assertEqual({q["type"] for q in questions.values()}, {"noul", "choice", "score"})
        for question in questions.values():
            self.assertEqual(set(question), {"type", "instructions", "criteria"})
            self.assertIsInstance(question["instructions"], str)
            if question["type"] == "score":
                self.assertIsInstance(question["criteria"], list)
                self.assertGreaterEqual(len(question["criteria"]), 2)
            else:
                self.assertIsInstance(question["criteria"], dict)
                if question["type"] == "noul":
                    self.assertEqual(set(question["criteria"]), {"true", "false"})
        self.fake.push({"answers": {}, "usage": {"input_tokens": 20}})
        import urllib.request

        with urllib.request.urlopen(urllib.request.Request(
                jev["endpoint"], data=json.dumps(request).encode("utf-8"),
                headers={"Content-Type": "application/json"}), timeout=1) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.fake.requests[0]["body"], request)

    def test_all_fixture_bytes_are_synthetic_utf8_with_newlines(self):
        self.contract()
        paths = sorted(FIXTURES.rglob("*"))
        self.assertTrue(paths)
        for path in paths:
            if path.is_file():
                with self.subTest(path=str(path.relative_to(FIXTURES))):
                    data = path.read_bytes()
                    text = data.decode("utf-8")
                    self.assertTrue(data.endswith(b"\n"))
                    self.assertNotIn(b"\r", data)
                    for forbidden in ("/Users/", "/private/", "Bearer sk-", "\u2014", "TODO"):
                        self.assertNotIn(forbidden, text)
                    self.assertIn("acme-shop", text)


if __name__ == "__main__":
    unittest.main()
