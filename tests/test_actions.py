"""Action notes using an isolated real git repository."""

import copy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_host_contracts import FIXTURES, load_fixture


class ActionTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.name", "Acme Test")
        self.git("config", "user.email", "acme@example.invalid")
        self.file = self.repo / "header.txt"
        self.file.write_text("six columns\n")
        self.git("add", "header.txt")
        self.git("commit", "-qm", "Initial acme-shop header")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, timeout=3)

    def session(self):
        from memsift import host
        return {"turn": 1, "prompt": "Use twelve columns.",
                "snapshot": host.snapshot(self.repo), "actions": []}

    def bash(self, session, command, status=0):
        from memsift import host
        return host.note_tool(self.repo, session, {"name": "Bash", "changes": [],
                              "command": command, "exit_code": status})

    def test_edit_refreshes_snapshot_and_read_only_command_does_not_double_count(self):
        from memsift import host
        session = self.session()
        before = copy.deepcopy(session)
        self.file.write_text("twelve columns\n")
        changed = host.note_tool(self.repo, session, {"name": "Edit", "changes": [
            {"path": str(self.file), "kind": "edit", "diff": "six → twelve"}],
            "command": "", "exit_code": None})
        self.assertEqual(session, before)
        self.assertEqual(changed["actions"], [{"turn": 1, "prompt": "Use twelve columns.",
                         "items": ["Edit header.txt: six → twelve"]}])
        self.assertEqual(changed["actions"], self.bash(changed, "git status")["actions"])

    def test_dirty_file_further_change_commit_and_untracked_names_change_snapshot(self):
        session = self.session()
        self.file.write_text("twelve columns\n")
        session = self.bash(session, "update header")
        self.file.write_text("twelve columns, gap two\n")
        session = self.bash(session, "update gap")
        self.git("add", "header.txt")
        self.git("commit", "-qm", "Update acme-shop header")
        session = self.bash(session, "git commit")
        (self.repo / "new file's name.txt").write_text("acme-shop\n")
        session = self.bash(session, "create file")
        self.assertEqual(session["actions"][0]["items"], ["Bash: update header (ok)",
                         "Bash: update gap (ok)", "Bash: git commit (ok)", "Bash: create file (ok)"])

    def test_failed_retry_in_same_turn_and_hidden_unretried_failure(self):
        from memsift import host
        session = self.bash(self.session(), "python3 -m unittest", 1)
        path = self.home / "turn.jsonl"
        path.write_text(json.dumps({"type": "user", "message": {
            "content": session["prompt"]}}) + "\n")
        self.assertEqual(host.read_conversation(path, "claude-code", session["actions"], 3.0)
                         ["exchanges"][0]["actions"], [])
        session = self.bash(session, "python3 -m unittest")
        self.assertEqual(session["actions"][0]["items"], ["Bash: python3 -m unittest failed → ok"])
        self.assertEqual(self.bash(session, "python3 -m unittest")["actions"], session["actions"])

    def test_retry_never_matches_across_turns_and_last_five_groups_remain(self):
        session = self.bash(self.session(), "check", 1)
        session.update(turn=2, prompt="Try again.")
        session = self.bash(session, "check")
        self.assertNotIn("failed → ok", repr(session))
        from memsift import host
        for turn in range(2, 8):
            session.update(turn=turn, prompt="acme-shop " + "p" * 300)
            session = host.note_tool(self.repo, session, {"name": "Write", "changes": [
                {"path": str(self.file), "kind": "write", "diff": ""}], "command": "", "exit_code": None})
        self.assertEqual([g["turn"] for g in session["actions"]], [3, 4, 5, 6, 7])
        self.assertTrue(all(len(g["prompt"]) == 200 for g in session["actions"]))

    def test_outside_and_symlink_edits_ignored_and_relative_paths_with_quotes_kept(self):
        from memsift import host
        outside = self.home / "outside.txt"
        outside.write_text("acme-shop")
        (self.repo / "link.txt").symlink_to(outside)
        session = self.session()
        for name in (str(outside), "link.txt", "../outside.txt"):
            session = host.note_tool(self.repo, session, {"name": "Edit", "changes": [
                {"path": name, "kind": "edit", "diff": "change"}], "command": "", "exit_code": None})
        self.assertEqual(session["actions"], [])
        session = host.note_tool(self.repo, session, {"name": "Write", "changes": [
            {"path": "new file's name.txt", "kind": "write", "diff": "SECRET_CONTENT"}],
            "command": "", "exit_code": None})
        self.assertEqual(session["actions"][0]["items"], ["Write new file's name.txt"])

    def test_fixture_edits_multiedit_notebook_patch_and_failed_edit(self):
        from memsift import host
        for name, fixture, expected in (
                ("claude-code", "tool-edit", ["Edit src/header.tsx"]),
                ("claude-code", "tool-write", ["Write src/catalog.tsx"]),
                ("claude-code", "tool-notebookedit", ["Edit analysis/catalog.ipynb"]),
                ("claude-code", "tool-failure", []),
                ("claude-code", "tool-multiedit", []),
                ("codex", "tool-apply-patch", ["Edit src/header.tsx", "Write src/catalog.tsx", "Edit src/legacy.tsx"])):
            with self.subTest(host=name, fixture=fixture):
                payload = json.loads(load_fixture(FIXTURES / "hooks" / name / (fixture + ".json"),
                                                   self.repo, self.home / "transcript.jsonl"))
                event = host.normalize(payload["hook_event_name"], payload, name)
                session = host.note_tool(self.repo, self.session(), event["tool"])
                items = session["actions"][0]["items"] if session["actions"] else []
                self.assertEqual([i.split(": ")[0] for i in items], expected)
                if fixture == "tool-multiedit":
                    payload.pop("error")
                    payload["hook_event_name"] = "PostToolUse"
                    event = host.normalize("PostToolUse", payload, name)
                    session = host.note_tool(self.repo, self.session(), event["tool"])
                    self.assertIn("gap-1 → gap-2", repr(session["actions"]))

    def test_same_file_edits_collapse_preserving_bounded_redacted_diffs(self):
        from memsift import host
        session = self.session()
        for diff in ("six → twelve", "gap-1 → gap-2", "PASSWORD=invented-password " + "x" * 250):
            session = host.note_tool(self.repo, session, {"name": "Edit", "changes": [
                {"path": str(self.file), "kind": "edit", "diff": diff}], "command": "", "exit_code": None})
        items = session["actions"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("six → twelve", items[0])
        self.assertIn("gap-1 → gap-2", items[0])
        self.assertNotIn("invented-password", repr(session))
        snippets = items[0].split(": ", 1)[1].split(" · ")
        self.assertEqual(len(snippets), 3)
        self.assertTrue(all(len(snippet) <= 200 for snippet in snippets))
        self.assertEqual(len(snippets[-1]), 200)

    def test_new_prompt_with_reused_turn_number_does_not_borrow_actions(self):
        from memsift import host
        session = self.session()
        session["actions"] = [{"turn": 1, "prompt": "old acme-shop prompt", "items": ["Edit old.txt"]}]
        result = host.note_tool(self.repo, session, {"name": "Write", "changes": [
            {"path": "new.txt", "kind": "write", "diff": ""}], "command": "", "exit_code": None})
        self.assertEqual(result["actions"][-1]["prompt"], "Use twelve columns.")
        self.assertEqual(result["actions"][-1]["items"], ["Write new.txt"])

    def test_normalized_codex_failed_then_successful_command_is_a_retry(self):
        from memsift import host
        transcript = self.home / "codex.jsonl"
        transcript.write_text(load_fixture(FIXTURES / "transcripts/codex-plain.jsonl", self.repo, transcript))
        session = self.session()
        for name in ("tool-bash-failed", "tool-bash-ok"):
            payload = json.loads(load_fixture(FIXTURES / "hooks/codex" / (name + ".json"), self.repo, transcript))
            session = host.note_tool(self.repo, session, host.normalize("PostToolUse", payload, "codex")["tool"])
        self.assertEqual(session["actions"][0]["items"],
                         ["Bash: python3 -m unittest tests.test_catalog failed → ok"])

    def test_no_git_and_unborn_repo(self):
        from memsift import host
        root = self.home / "acme-shop-empty"
        root.mkdir()
        self.assertEqual(host.snapshot(root), "")
        session = {"turn": 1, "prompt": "acme-shop", "snapshot": "", "actions": []}
        tool = {"name": "Bash", "changes": [], "command": "test", "exit_code": 1}
        self.assertEqual(host.note_tool(root, session, tool)["actions"], [])
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
        first = host.snapshot(root)
        self.assertTrue(first)
        (root / "new.txt").write_text("acme-shop")
        self.assertNotEqual(host.snapshot(root), first)

    def test_git_errors_and_path_resolution_errors_have_tracebacks(self):
        from memsift import host
        for fault in (OSError("injected git error"), subprocess.TimeoutExpired("git", 1)):
            with patch("memsift.host.subprocess.run", side_effect=fault):
                self.assertEqual(host.snapshot(self.repo), "")
        with patch.object(Path, "resolve", side_effect=OSError("injected path error")):
            self.assertEqual(host.note_tool(self.repo, {"turn": 1, "prompt": "acme-shop", "actions": []},
                             {"name": "Edit", "changes": [{"path": "header.txt", "kind": "edit", "diff": "x"}],
                              "command": "", "exit_code": None})["actions"], [])
        log = (self.home / ".memsift/debug.log").read_text()
        self.assertIn("Traceback", log)
        self.assertIn("injected git error", log)
        self.assertIn("injected path error", log)

    def test_detach_options_and_read_search_web_are_ignored(self):
        from memsift import host
        options = host.detach_args()
        if os.name == "nt":
            self.assertTrue(options["creationflags"])
        else:
            self.assertTrue(options["start_new_session"])
        session = self.session()
        for name in ("Read", "Grep", "WebSearch"):
            self.assertEqual(host.note_tool(self.repo, session, {"name": name, "changes": [],
                             "command": "", "exit_code": None}), session)
