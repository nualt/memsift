"""Shared storage contracts, including append/rewrite interleavings."""

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from tests.support import IsolatedTest, child_env


def turn(prompt="Use twelve columns.", answer="Applied.", actions=None):
    return {"ts": "2026-10-14T07:31:02Z", "session": "a1",
            "prompt": prompt, "answer": answer,
            "actions": [] if actions is None else actions}


def append_worker(root, label, ready, start):
    from memsift import store
    ready.set()
    assert start.wait(10)
    for index in range(200):
        store.append_draft(root, turn("%s-prompt-%s" % (label, index),
                                     "%s-answer-%s" % (label, index)), "project", False, 3)


def append_after_snapshot(root, snapshot_ready, appended):
    from memsift import store
    assert snapshot_ready.wait(10)
    store.append_draft(root, turn("late café 🙂", "late answer"), "lesson", False, 3)
    appended.set()


def rewrite_worker(path, ready, start, done):
    from memsift import store
    ready.set()
    assert start.wait(10)
    for index in range(40):
        value = "old" if index % 2 else "new"
        store.update_json(path, {}, lambda state: {"value": value, "data": value * 100000})
    done.set()


def increment_worker(path, ready, start):
    from memsift import store
    ready.set()
    assert start.wait(10)
    for index in range(100):
        store.update_json(path, {"count": 0, "ratio": 3.0},
                          lambda state: dict(state, count=state["count"] + 1))


def replay_worker(root, acquired, release, result):
    from memsift import store
    with store.replay_lock(root) as owns:
        if owns:
            turns = store.pending_turns(root)
            acquired.set()
            assert release.wait(10)
            for value in turns:
                store.append_draft(root, value, "project", False, 3)
            store.ack_pending(root, turns)
            result.write_text(str(len(turns)))
        else:
            result.write_text("0")


class StoreTests(IsolatedTest):
    def start_process(self, target, *args):
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=target, args=args)
        with patch.dict(os.environ, child_env(self.fake.url, self.home), clear=True):
            process.start()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)

        self.addCleanup(cleanup)
        return process

    def join_process(self, process):
        process.join(15)
        self.assertFalse(process.is_alive(), "storage child did not finish")
        self.assertEqual(process.exitcode, 0)

    def log(self):
        path = self.home / ".memsift" / "debug.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_identity_is_absolute_and_distinguishes_same_basename(self):
        from memsift import store
        other = self.home / "clients" / "acme-shop"
        other.mkdir(parents=True)
        self.assertEqual(store.repo_root(str(self.repo)), self.repo.resolve())
        expected = "acme-shop-" + hashlib.sha256(str(self.repo.resolve()).encode()).hexdigest()[:6]
        self.assertEqual(store.repo_key(self.repo), expected)
        self.assertNotEqual(store.repo_key(self.repo), store.repo_key(other))
        self.assertEqual(store.repo_dir(self.repo), self.home / ".memsift" / "repos" / expected)
        self.assertFalse(store.home().exists())
        with patch.dict(os.environ, {"HOME": str(other)}):
            self.assertEqual(store.home(), other / ".memsift")

    def test_repo_root_uses_git_toplevel_and_resolves_symlinks(self):
        from memsift import store
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        nested = self.repo / "src"
        nested.mkdir()
        link = self.home / "repo-link"
        link.symlink_to(nested, target_is_directory=True)
        self.assertEqual(store.repo_root(str(link)), self.repo.resolve())
        self.assertEqual(store.repo_key(link), store.repo_key(nested))

    def test_git_errors_log_tracebacks_and_fall_back(self):
        from memsift import store
        for exc in (OSError("invented git failure"), subprocess.TimeoutExpired("git", 1)):
            with self.subTest(exc=type(exc).__name__), patch("subprocess.run", side_effect=exc):
                self.assertEqual(store.repo_root(str(self.repo)), self.repo.resolve())
            self.assertIn(type(exc).__name__, self.log())
        self.assertIn("Traceback", self.log())

    def test_session_ids_retain_valid_names_and_reject_traversal(self):
        from memsift import store
        for value in ("a1", "acme-shop_2026-10-14", "aa-bb.cc"):
            self.assertEqual(store.session_path(value), store.home() / "sessions" / (value + ".json"))
        for value in ("", ".", "..", "../escape", "a/escape", "a\\escape", "a\0b", "a..b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.session_path(value)
        sessions = store.home() / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "escape.json").symlink_to(self.home / "outside.json")
        with self.assertRaises(ValueError):
            store.session_path("escape")

    def test_missing_reads_create_nothing_and_defaults_are_independent(self):
        from memsift import store
        path = store.home() / "missing.json"
        self.assertEqual(store.read_text(path), "")
        default = {"items": []}
        result = store.read_json(path, default)
        result["items"].append("changed")
        self.assertEqual(default, {"items": []})
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertEqual(store.read_notes(self.repo), {"digest": "", "draft": "", "entries": [], "pending": {}})
        self.assertFalse(store.home().exists())

    def test_cut_keeps_both_ends_within_budget(self):
        from memsift import store
        text = "START " + "é" * 9000 + " END"
        for limit in (1500, 1000):
            with self.subTest(limit=limit):
                cut = store.clip(text, limit, 3.0)
                self.assertTrue(cut.startswith("START"))
                self.assertTrue(cut.endswith("END"))
                self.assertIn("[…]", cut)
                self.assertLessEqual(store.estimate(cut, 3.0), limit)
        self.assertEqual(store.estimate("é🙂ab", 3), 2)
        self.assertEqual(store.clip("abcdefghijk", 3, 2), "ab[…]k")
        self.assertEqual(store.clip("abcdefghijk", 0, 3), "")
        self.assertEqual(store.clip("abcdefghijk", 1, 1), "[")
        self.assertEqual(store.clip("short", 10, 3), "short")

    def test_writes_redact_normalize_and_replace_atomically(self):
        from memsift import store
        path = store.home() / "notes.txt"
        store.write_text(path, "café\r\nPASSWORD=invented-secret\r")
        self.assertEqual(path.read_bytes(), "café\n[redacted:credential]\n".encode())
        store.append_text(path, "API_KEY=invented-second\r\nend")
        self.assertEqual(path.read_text(), "café\n[redacted:credential]\n[redacted:credential]\nend\n")
        self.assertFalse(path.with_name(path.name + ".tmp").exists())
        state = store.home() / "state.json"
        result = store.update_json(state, {"count": 0}, lambda value: {"count": value["count"] + 1, "api_key": "invented-secret"})
        self.assertEqual(result, {"count": 1, "api_key": "[redacted:credential]"})
        self.assertEqual(store.read_json(state, {}), result)
        store.update_json(state, {}, lambda value: dict(value, count=value["count"] + 1))
        self.assertEqual(store.read_json(state, {})["count"], 2)

    def test_temporary_file_is_already_redacted_on_replace_failure(self):
        from memsift import store
        path = store.home() / "notes.txt"
        store.write_text(path, "old\n")
        with patch("os.replace", side_effect=PermissionError("PASSWORD=invented-secret")):
            with self.assertRaises(PermissionError):
                store.write_text(path, "API_KEY=invented-new")
        self.assertEqual(store.read_text(path), "old\n")
        self.assertNotIn("invented-new", path.with_name(path.name + ".tmp").read_text())
        self.assertIn("PermissionError", self.log())
        self.assertIn("Traceback", self.log())
        self.assertNotIn("invented-secret", self.log())

    def test_malformed_json_and_wrong_shape_log_and_use_default(self):
        from memsift import store
        path = self.home / "bad.json"
        for text in ("{broken", "[]", "null"):
            path.write_text(text)
            self.assertEqual(store.read_json(path, {"count": 0}), {"count": 0})
        self.assertIn("Traceback", self.log())
        self.assertIn("JSONDecodeError", self.log())
        self.assertIn("ValueError", self.log())

    def test_read_errors_and_disappearing_file_are_logged(self):
        from memsift import store
        path = self.home / "notes.txt"
        path.touch()
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError("invented race")):
            self.assertEqual(store.read_text(path), "")
        for exc in (PermissionError("invented permission"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            with self.subTest(exc=type(exc).__name__), patch.object(Path, "read_bytes", side_effect=exc):
                with self.assertRaises(type(exc)):
                    store.read_text(path)
            self.assertIn(type(exc).__name__, self.log())
        self.assertIn("FileNotFoundError", self.log())
        self.assertIn("Traceback", self.log())

    def test_necessary_write_lock_failure_is_visible_and_bounded(self):
        from memsift import store
        from memsift.vendor.file_lock import file_lock
        path = store.home() / "notes.txt"
        with file_lock(path.with_name(path.name + ".lock")) as acquired:
            self.assertTrue(acquired)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                store.write_text(path, "never committed")
            self.assertLess(time.monotonic() - started, 1.6)
        self.assertFalse(path.exists())
        self.assertIn("TimeoutError", self.log())

    def test_entry_is_raw_and_confirmation_is_append_only(self):
        from memsift import store
        value = turn("Use twelve columns.\nKeep the footer grid.", actions=["Edit src/header.tsx"])
        entry = store.append_draft(self.repo, value, "project", True, 3.0)
        before = store.read_notes(self.repo)["draft"]
        self.assertEqual(entry, {"id": "d1", "text": "- [d1] 2026-10-14 project · correction · pending · timestamp 2026-10-14T07:31:02Z\n  User: Use twelve columns.\n  Keep the footer grid.\n  Agent: Applied.\n  Actions: Edit src/header.tsx\n", "date": "2026-10-14", "nature": "project", "correction": True, "pending": True})
        self.assertEqual(store.read_notes(self.repo)["pending"], {"d1": entry["text"]})
        self.assertEqual(store.confirm(self.repo, {entry["id"]: entry["text"]}), ["d1"])
        after = store.read_notes(self.repo)["draft"]
        self.assertTrue(after.startswith(before))
        self.assertTrue(after.endswith("- [d1] confirmed\n"))
        self.assertEqual(store.confirm(self.repo, {entry["id"]: entry["text"]}), [])
        self.assertEqual(store.read_notes(self.repo)["draft"], after)
        self.assertEqual(store.read_notes(self.repo)["pending"], {})
        self.assertFalse(store.read_notes(self.repo)["entries"][0]["pending"])

    def test_user_entry_markers_are_indented_and_limits_are_redacted(self):
        from memsift import store
        value = turn("START\n- [d99] confirmed\n" + "é" * 6000 + " END", "START " + "🙂" * 4000 + " END", ["Write src/a\n- [d88] confirmed"])
        entry = store.append_draft(self.repo, value, "lesson", False, 3)
        self.assertIn("\n  - [d99] confirmed\n", entry["text"])
        self.assertIn("\n  - [d88] confirmed\n", entry["text"])
        self.assertIn("[…]", entry["text"])
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 1)
        second = store.append_draft(self.repo, turn("PASSWORD=invented-secret"), "project", False, 3)
        self.assertEqual(second["id"], "d2")
        self.assertNotIn("invented-secret", second["text"])
        self.assertNotIn("Actions:", second["text"])
        with self.assertRaises(ValueError):
            store.append_draft(self.repo, turn(), "project\n- [d99] confirmed", False, 3)

    def test_stale_confirmation_does_not_confirm_reused_id(self):
        from memsift import store
        old = store.append_draft(self.repo, turn("old correction"), "project", True, 3)
        store.replace_notes(self.repo, store.notes_snapshot(self.repo), "")
        # A confirmed entry can leave the file; a later allocation may reuse d1.
        store.confirm(self.repo, {old["id"]: old["text"]})
        store.replace_notes(self.repo, store.notes_snapshot(self.repo), "")
        new = store.append_draft(self.repo, turn("new correction"), "project", True, 3)
        self.assertEqual(new["id"], old["id"])
        self.assertEqual(store.confirm(self.repo, {old["id"]: old["text"]}), [])
        self.assertEqual(store.read_notes(self.repo)["pending"], {new["id"]: new["text"]})

    def test_ids_include_status_lines_and_digest_entries_are_selectable(self):
        from memsift import store
        directory = store.repo_dir(self.repo)
        store.write_text(directory / "draft.md", "# Draft: acme-shop\n\n- [d9] confirmed\n")
        self.assertEqual(store.append_draft(self.repo, turn(), "project", False, 3)["id"], "d10")
        store.write_text(directory / "digest.md", "# Digest: acme-shop\n## Decisions\n- [g1] twelve columns (2026-10-12)\n## Project facts\n- [g2] uses sharp (2026-10-09)\n")
        notes = store.read_notes(self.repo)
        self.assertEqual([entry["id"] for entry in notes["entries"]], ["g1", "g2", "d10"])
        self.assertEqual(notes["entries"][0]["text"], "- [g1] twelve columns (2026-10-12)\n")
        self.assertEqual(notes["entries"][0]["nature"], "project")

    def test_all_digest_sections_have_project_nature(self):
        from memsift import store
        digest = "# Digest: acme-shop\n"
        for index, heading in enumerate(("In progress", "Decisions", "Project facts", "Working rules"), 1):
            digest += "## %s\n- [g%d] Known project information (2026-10-14)\n" % (heading, index)
        store.write_text(store.repo_dir(self.repo) / "digest.md", digest)
        self.assertEqual([entry["nature"] for entry in store.read_notes(self.repo)["entries"]],
                         ["project", "project", "project", "project"])

    def test_replay_ownership_is_nonblocking(self):
        from memsift import store
        with store.replay_lock(self.repo) as first:
            self.assertTrue(first)
            started = time.monotonic()
            with store.replay_lock(self.repo) as second:
                self.assertFalse(second)
            self.assertLess(time.monotonic() - started, 0.3)
        with store.replay_lock(self.repo) as acquired:
            self.assertTrue(acquired)

    def test_session_updates_preserve_fields_and_never_recreate_ended_sessions(self):
        from memsift import store
        self.assertIsNone(store.update_session_if_exists("ended", lambda value: self.fail("callback on missing session")))
        self.assertFalse(store.session_path("ended").exists())
        session = {"repo_root": str(self.repo.resolve()), "outage_notices": ["jev"], "actions": [{"turn": 1, "prompt": "twelve columns", "items": ["Edit src/header.tsx"]}], "flags": []}
        path = store.session_path("running")
        store.write_text(path, json.dumps(session))
        result = store.update_session_if_exists("running", lambda value: dict(value, flags=["jev_md_missing"]))
        self.assertEqual(result, dict(session, flags=["jev_md_missing"]))
        path.unlink()
        self.assertIsNone(store.update_session_if_exists("running", lambda value: value))
        self.assertFalse(path.exists())

    def test_queue_clips_redacts_and_ack_preserves_concurrent_arrivals(self):
        from memsift import store
        store.queue_turn(self.repo, turn("START " + "x" * 6000 + " END", "PASSWORD=invented-secret"), 3)
        first = store.pending_turns(self.repo)
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["prompt"].endswith("END"))
        self.assertLessEqual(store.estimate(first[0]["prompt"], 3), 1500)
        self.assertNotIn("invented-secret", first[0]["answer"])
        store.queue_turn(self.repo, turn("later"), 3)
        with store.replay_lock(self.repo) as acquired:
            self.assertTrue(acquired)
            store.ack_pending(self.repo, first)
        self.assertEqual(store.pending_turns(self.repo), [turn("later")])
        store.ack_pending(self.repo, [turn("mismatch")])
        self.assertEqual(store.pending_turns(self.repo), [turn("later")])
        self.assertIn("Traceback", self.log())
        store.ack_pending(self.repo, [turn("later")])
        self.assertFalse((store.repo_dir(self.repo) / "pending.jsonl").exists())

    def test_malformed_pending_is_logged_and_never_acknowledged(self):
        from memsift import store
        path = store.repo_dir(self.repo) / "pending.jsonl"
        for text in ('{"prompt":', '[]\n'):
            store.write_text(path, text)
            with self.assertRaises(ValueError):
                store.pending_turns(self.repo)
            with self.assertRaises(ValueError):
                store.ack_pending(self.repo, [turn()])
            self.assertEqual(store.read_text(path), text)
        self.assertIn("Traceback", self.log())

    def test_snapshot_uses_bytes_and_keeps_pending_and_late_confirmation(self):
        from memsift import store
        correction = store.append_draft(self.repo, turn("café 🙂"), "project", True, 3)
        store.append_draft(self.repo, turn("ordinary"), "project", False, 3)
        snapshot = store.notes_snapshot(self.repo)
        self.assertEqual(snapshot["draft_bytes"], len(snapshot["draft"].encode("utf-8")))
        self.assertGreater(snapshot["draft_bytes"], len(snapshot["draft"]))
        store.confirm(self.repo, {correction["id"]: correction["text"]})
        late = store.append_draft(self.repo, turn("later"), "lesson", False, 3)
        store.replace_notes(self.repo, snapshot, "# Digest: acme-shop\n- [g1] café\n")
        notes = store.read_notes(self.repo)
        self.assertEqual([entry["id"] for entry in notes["entries"]], ["g1", "d1", "d3"])
        self.assertIn(correction["text"], notes["draft"])
        self.assertIn(late["text"], notes["draft"])
        self.assertIn("- [d1] confirmed\n", notes["draft"])
        self.assertEqual(notes["pending"], {})
        self.assertEqual(notes["draft"].count("# Draft:"), 1)
        store.replace_notes(self.repo, store.notes_snapshot(self.repo), "")
        self.assertEqual(store.read_notes(self.repo)["entries"], [])

    def test_empty_snapshot_keeps_new_header_and_pending_without_duplicates(self):
        from memsift import store
        snapshot = store.notes_snapshot(self.repo)
        entry = store.append_draft(self.repo, turn(), "project", True, 3)
        store.replace_notes(self.repo, snapshot, "")
        self.assertEqual(store.read_notes(self.repo)["pending"], {entry["id"]: entry["text"]})
        self.assertEqual(store.read_notes(self.repo)["draft"].count("# Draft:"), 1)
        store.replace_notes(self.repo, store.notes_snapshot(self.repo), "")
        self.assertEqual(store.read_notes(self.repo)["pending"], {entry["id"]: entry["text"]})

    def test_session_expiry_is_strictly_older_than_seven_days(self):
        from memsift import store
        now = time.time()
        for name, age in (("old", 604801), ("boundary", 604800), ("new", 30)):
            path = store.session_path(name)
            store.write_text(path, "{}")
            os.utime(path, (now - age, now - age))
        store.prune_sessions(now)
        self.assertFalse(store.session_path("old").exists())
        self.assertTrue(store.session_path("boundary").exists())
        self.assertTrue(store.session_path("new").exists())

    def test_two_processes_append_400_unique_complete_entries(self):
        from memsift import store
        context = multiprocessing.get_context("spawn")
        ready_a, ready_b, start = context.Event(), context.Event(), context.Event()
        first = self.start_process(append_worker, self.repo, "first", ready_a, start)
        second = self.start_process(append_worker, self.repo, "second", ready_b, start)
        self.assertTrue(ready_a.wait(10))
        self.assertTrue(ready_b.wait(10))
        start.set()
        self.join_process(first)
        self.join_process(second)
        notes = store.read_notes(self.repo)
        self.assertEqual(len(notes["entries"]), 400)
        self.assertEqual(len({entry["id"] for entry in notes["entries"]}), 400)
        self.assertEqual(notes["draft"].count("# Draft:"), 1)
        actual = set()
        for entry in notes["entries"]:
            lines = entry["text"].splitlines()
            self.assertEqual(len(lines), 3)
            prompt = lines[1].removeprefix("  User: ")
            self.assertEqual(lines[2], "  Agent: " + prompt.replace("-prompt-", "-answer-"))
            actual.add(prompt)
        self.assertEqual(actual, {"%s-prompt-%s" % (label, index)
                                  for label in ("first", "second") for index in range(200)})

    def test_process_append_between_snapshot_and_replacement_survives(self):
        from memsift import store
        context = multiprocessing.get_context("spawn")
        snapshot_ready, appended = context.Event(), context.Event()
        store.append_draft(self.repo, turn("old"), "project", False, 3)
        process = self.start_process(append_after_snapshot, self.repo, snapshot_ready, appended)
        snapshot = store.notes_snapshot(self.repo)
        snapshot_ready.set()
        self.assertTrue(appended.wait(10))
        store.replace_notes(self.repo, snapshot, "# Digest: acme-shop\n")
        self.join_process(process)
        notes = store.read_notes(self.repo)
        self.assertEqual(len(notes["entries"]), 1)
        self.assertIn("late café 🙂", notes["draft"])
        self.assertNotIn("User: old", notes["draft"])

    def test_reader_never_observes_partial_json_during_process_rewrites(self):
        from memsift import store
        path = store.home() / "state.json"
        old = {"value": "old", "data": "old" * 100000}
        new = {"value": "new", "data": "new" * 100000}
        store.update_json(path, {}, lambda state: old)
        context = multiprocessing.get_context("spawn")
        ready, start, done = context.Event(), context.Event(), context.Event()
        process = self.start_process(rewrite_worker, path, ready, start, done)
        self.assertTrue(ready.wait(10))
        start.set()
        reads = 0
        deadline = time.monotonic() + 15
        while not done.is_set() and time.monotonic() < deadline:
            self.assertIn(store.read_json(path, {}), (old, new))
            reads += 1
        self.join_process(process)
        self.assertTrue(done.is_set())
        self.assertGreater(reads, 0)
        self.assertNotIn("store-read-json", self.log())

    def test_two_process_json_updates_do_not_lose_fields_or_increments(self):
        from memsift import store
        path = store.home() / "state.json"
        context = multiprocessing.get_context("spawn")
        ready_a, ready_b, start = context.Event(), context.Event(), context.Event()
        first = self.start_process(increment_worker, path, ready_a, start)
        second = self.start_process(increment_worker, path, ready_b, start)
        self.assertTrue(ready_a.wait(10))
        self.assertTrue(ready_b.wait(10))
        start.set()
        self.join_process(first)
        self.join_process(second)
        self.assertEqual(store.read_json(path, {}), {"count": 200, "ratio": 3.0})

    def test_two_replay_consumers_sort_each_turn_once_without_blocking_arrivals(self):
        from memsift import store
        store.queue_turn(self.repo, turn("first"), 3)
        store.queue_turn(self.repo, turn("second"), 3)
        context = multiprocessing.get_context("spawn")
        acquired, release = context.Event(), context.Event()
        first_result, second_result = self.home / "first-count", self.home / "second-count"
        first = self.start_process(replay_worker, self.repo, acquired, release, first_result)
        self.assertTrue(acquired.wait(10))
        second = self.start_process(replay_worker, self.repo, acquired, release, second_result)
        self.join_process(second)
        self.assertEqual(second_result.read_text(), "0")
        store.queue_turn(self.repo, turn("arrived during replay"), 3)
        release.set()
        self.join_process(first)
        self.assertEqual(first_result.read_text(), "2")
        self.assertEqual(len(store.read_notes(self.repo)["entries"]), 2)
        self.assertEqual(store.pending_turns(self.repo), [turn("arrived during replay")])

    def test_failed_rewrite_does_not_drop_tail_and_reports_snapshot_mismatch(self):
        from memsift import store
        store.append_draft(self.repo, turn(), "project", False, 3)
        snapshot = store.notes_snapshot(self.repo)
        path = store.repo_dir(self.repo) / "draft.md"
        store.write_text(path, "# Draft: changed\n")
        with self.assertRaises(ValueError):
            store.replace_notes(self.repo, snapshot, "")
        self.assertEqual(store.read_text(path), "# Draft: changed\n")
        self.assertIn("Traceback", self.log())

    def test_structured_pending_redaction_preserves_json_and_quoted_content(self):
        from memsift import store
        value = turn('Use "quoted" columns. PASSWORD=invented-secret', 'API_KEY="invented-second"')
        store.queue_turn(self.repo, value, 3)
        self.assertEqual(store.pending_turns(self.repo), [turn('Use "quoted" columns. [redacted:credential]', '[redacted:credential]')])

    def test_last_digest_entry_keeps_its_newline_before_an_empty_section(self):
        from memsift import store
        store.write_text(store.repo_dir(self.repo) / "digest.md",
                         "# Digest: acme-shop\n## Decisions\n"
                         "- [g1] twelve columns (2026-10-12)\n## Working rules\n")
        self.assertEqual(store.read_notes(self.repo)["entries"][0]["text"],
                         "- [g1] twelve columns (2026-10-12)\n")

    def test_secrets_are_redacted_before_clipping_and_all_rewrites(self):
        from memsift import store
        secret = "-----BEGIN PRIVATE KEY-----\n" + "invented-private-value" * 1000 + "\n-----END PRIVATE KEY-----"
        clipped = store.clip("START " + secret + " END", 20, 3)
        self.assertEqual(clipped, "START [redacted:private-key] END")
        value = turn("START " + secret + " END", "PASSWORD=invented-answer")
        store.queue_turn(self.repo, value, 3)
        store.queue_turn(self.repo, value, 3)
        store.ack_pending(self.repo, store.pending_turns(self.repo)[:1])
        entry = store.append_draft(self.repo, value, "project", True, 3)
        store.replace_notes(self.repo, store.notes_snapshot(self.repo), "PASSWORD=invented-digest\n")
        self.assertEqual(store.read_notes(self.repo)["pending"], {entry["id"]: entry["text"]})
        session = store.session_path("running")
        store.update_json(session, {}, lambda state: {"flags": []})
        committed = store.update_session_if_exists("running", lambda state: dict(state, password="invented-password"))
        self.assertEqual(committed["password"], "[redacted:credential]")
        for path in store.home().rglob("*"):
            if path.is_file():
                self.assertNotIn("invented-", path.read_text(encoding="utf-8"), str(path))
