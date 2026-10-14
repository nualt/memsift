"""Host entry points, detached worker lifetime and offline CLI contracts."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

from tests.support import IsolatedTest, child_env
from tests.test_core import event, stop_body, turn
from tests.test_decisions import noul

ROOT = Path(__file__).parents[1]


class HookTests(IsolatedTest):
    def invoke(self, args, payload):
        from memsift import run
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(output):
            result = run.main(args)
        return result, output.getvalue()

    def log(self):
        from memsift import store
        return store.read_text(store.home() / "debug.log")

    def cli(self, kind, payload, host="claude-code", **kwargs):
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(getattr(self, "runner", ROOT / "memsift/run.py")), kind,
                                 "--host", host], input=json.dumps(payload), text=True,
                                capture_output=True, env=child_env(self.fake.url, self.home),
                                cwd=self.repo, timeout=22, **kwargs)
        return result, time.monotonic() - started

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_core_exception_never_fails_host(self):
        from memsift import run
        self.assertTrue(callable(getattr(run, "main", None)), "runner main is missing")
        with patch("memsift.core.prepare", side_effect=RuntimeError("invented failure")):
            result = self.invoke(["prompt", "--host", "claude-code"], json.dumps(
                {"session_id": "a1", "cwd": str(self.repo), "prompt": "Fix a typo."}))
        self.assertEqual(result, (0, ""))
        self.assertIn("RuntimeError", self.log())
        self.assertIn("Traceback", self.log())

    def test_invalid_input_and_arguments_log_without_output(self):
        from memsift import run
        self.assertTrue(callable(getattr(run, "main", None)), "runner main is missing")
        for args, payload in [(["prompt"], ""), (["prompt"], "{"), (["prompt"], "[]"),
                              (["prompt"], "{}"), (["unknown"], "{}"),
                              (["prompt", "--host", "unknown"], "{}"),
                              (["prompt", "--unknown"], "{}"),
                              (["prompt"], '{"session_id": 1}'),
                              (["prompt"], '{"session_id":"a1","prompt":false}')]:
            with self.subTest(args=args, payload=payload):
                self.assertEqual(self.invoke(args, payload), (0, ""))
                self.assertIn("Traceback", self.log())

    def test_prompt_renders_only_context_and_tool_stays_local(self):
        from memsift import run
        self.assertTrue(callable(getattr(run, "main", None)), "runner main is missing")
        from memsift import core, store
        (self.repo / "jev.md").write_text("Product grid", encoding="utf-8")
        store.append_draft(self.repo, turn(), "project", False, 3)
        for host in ("claude-code", "codex"):
            core.prepare(event(self.repo, "start", host=host))
            for _ in range(3):
                self.fake.push({"answers": {"has_info": noul(0), "entry_d1": noul(1),
                                            "cognee": noul(0)}, "usage": {"input_tokens": 500}})
            result, output = self.invoke(["prompt", "--host", host], json.dumps(
                {"session_id": "a1", "cwd": str(self.repo), "prompt": "Continue"}))
            self.assertEqual(result, 0)
            envelope = json.loads(output)["hookSpecificOutput"]
            self.assertEqual(envelope["hookEventName"], "UserPromptSubmit")
            self.assertIn("Keep twelve columns.", envelope["additionalContext"])
            before = len(self.fake.requests)
            self.assertEqual(self.invoke(["tool", "--host", host], json.dumps(
                {"session_id": "a1", "cwd": str(self.repo), "tool_name": "Write",
                 "tool_input": {"file_path": str(self.repo / "grid.txt")}})), (0, ""))
            self.assertEqual(len(self.fake.requests), before)

    def test_cached_plugin_runs_every_event_for_both_hosts(self):
        from memsift import store
        from tests.test_host_contracts import load_fixture
        from tests.test_compaction import answer, envelope
        cache = self.home / "plugin cache with spaces"
        shutil.copytree(ROOT / "memsift", cache / "memsift", ignore=shutil.ignore_patterns("__pycache__"))
        self.runner = cache / "memsift/run.py"
        for host in ("claude-code", "codex"):
            sid = "a1-" + host
            transcript = self.home / (host + "-invented.jsonl")
            def payload(name):
                value = json.loads(load_fixture(ROOT / "tests/fixtures/hooks" / host / (name + ".json"),
                                                self.repo, transcript))
                value["session_id"] = sid
                return value
            self.fake.push([], path="/api/v1/datasets")
            result, elapsed = self.cli("start", payload("start"), host)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertLess(elapsed, 1)
            index = store.repo_dir(self.repo) / "cognee-index.json"
            self.wait_for(index.exists)
            entries = store.read_notes(self.repo)["entries"]
            for _ in range(2 + bool(entries)):
                self.fake.push({"answers": {"has_info": noul(1), "cognee": noul(0),
                                            **{"entry_" + entry["id"]: noul(0) for entry in entries}},
                                "usage": {"input_tokens": 500}}, path="/api/alpha/decisions")
            before_prompt = len(self.fake.requests)
            result, _ = self.cli("prompt", payload("prompt"), host)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.fake.requests) - before_prompt, 2 + bool(entries))
            self.assertIn("jev.md", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])
            result, _ = self.cli("tool", payload("tool-write"), host)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.fake.push(stop_body(), path="/api/alpha/decisions")
            result, elapsed = self.cli("stop", payload("stop"), host)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertLess(elapsed, 1)
            self.wait_for(lambda: bool(store.read_notes(self.repo)["entries"]))
            self.fake.push(envelope(answer()), path="/chat/completions")
            self.fake.push({"ok": True}, path="/api/v1/remember")
            before_end = len(self.fake.requests)
            result, elapsed = self.cli("end", payload("end"), host)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertLess(elapsed, 1)
            self.assertFalse(store.session_path(sid).exists())
            self.wait_for(lambda: len(self.fake.requests) >= before_end + 2 and
                          not list((store.repo_dir(self.repo) / "outbox").glob("*.txt")))
            index.unlink()

    def test_slow_stop_survives_parent_exit_and_end_without_recreating_session(self):
        from memsift import core, store
        core.prepare(event(self.repo, "start"))
        core.prepare(event(self.repo, "prompt", prompt="Keep twelve columns."))
        store.update_session_if_exists("a1", lambda s: dict(s, actions=[
            {"turn": 1, "prompt": "Keep twelve columns.", "items": ["Edit grid.txt"]}]))
        self.fake.push(stop_body(), delay=1.5, path="/api/alpha/decisions")
        result, elapsed = self.cli("stop", {"session_id": "a1", "cwd": str(self.repo),
                                            "last_assistant_message": "Applied."})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertLess(elapsed, 1)
        self.wait_for(lambda: bool(self.fake.requests))
        core.prepare(event(self.repo, "prompt", prompt="Next prompt"))
        result, elapsed = self.cli("end", {"session_id": "a1", "cwd": str(self.repo)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 1)
        self.wait_for(lambda: not store.session_path("a1").exists())
        self.wait_for(lambda: bool(store.read_notes(self.repo)["entries"]))
        draft = store.read_notes(self.repo)["draft"]
        self.assertIn("Keep twelve columns.", draft)
        self.assertIn("Edit grid.txt", draft)
        self.assertNotIn("Next prompt", draft)
        self.assertFalse(store.session_path("a1").exists())

    def test_large_captured_input_returns_before_worker_and_is_redacted(self):
        from memsift import core, store
        core.prepare(event(self.repo, "start"))
        core.prepare(event(self.repo, "prompt", prompt="Keep twelve columns. " + "grid " * 100000))
        self.fake.push(stop_body(), delay=1.2, path="/api/alpha/decisions")
        result, elapsed = self.cli("stop", {"session_id": "a1", "cwd": str(self.repo),
                                            "last_assistant_message": "PASSWORD=invented-private-value " + "done " * 100000})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.wait_for(lambda: bool(store.read_notes(self.repo)["entries"]))
        self.assertLess(elapsed, 1)
        self.assertNotIn("invented-private-value", store.read_notes(self.repo)["draft"])
        self.assertNotIn("invented-private-value", json.dumps(self.fake.requests[0]["body"]))
        self.assertEqual(list(store.home().glob("tmp*")), [])

    def test_prompt_jev_timeout_is_silent_and_counts_once(self):
        from memsift import breaker, core
        (self.repo / "jev.md").write_text("Grid", encoding="utf-8")
        core.prepare(event(self.repo, "start"))
        for _ in range(2):
            self.fake.push({}, delay=3, path="/api/alpha/decisions")
        result, elapsed = self.cli("prompt", {"session_id": "a1", "cwd": str(self.repo), "prompt": "Continue"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertGreater(elapsed, 1.8)
        self.assertLess(elapsed, 2.8)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)
        self.assertEqual(len(self.fake.requests), 2)

    def test_config_spawn_dispatch_and_log_errors_never_escape(self):
        from memsift import core, store
        core.prepare(event(self.repo, "start"))
        body = json.dumps({"session_id": "a1", "cwd": str(self.repo), "prompt": "Continue"})
        for target, kind in (("memsift.vendor.env_file.load_config", "prompt"),
                             ("memsift.core.dispatch", "prompt"),
                             ("subprocess.Popen", "stop"),
                             ("tempfile.TemporaryFile", "stop")):
            with self.subTest(target=target), patch(target, side_effect=OSError("PASSWORD=invented-private-value")):
                self.assertEqual(self.invoke([kind], body), (0, ""))
            self.assertIn("Traceback", self.log())
            self.assertNotIn("invented-private-value", self.log())
        with patch("memsift.core.prepare", side_effect=RuntimeError("core failure")), patch(
                "memsift.vendor.logfiles.error", side_effect=OSError("logging failure")):
            self.assertEqual(self.invoke(["prompt"], body), (0, ""))
        self.assertIn("logging failure", self.log())
        self.assertIn("core failure", self.log())
        with patch("memsift.core.prepare", side_effect=RuntimeError("core failure")), patch(
                "memsift.vendor.logfiles._append_unlocked", side_effect=PermissionError("physical log failure")):
            self.assertEqual(self.invoke(["prompt"], body), (0, ""))

    def test_worker_error_is_logged_and_worker_does_not_prepare_again(self):
        from memsift import core, store
        work = core.prepare(event(self.repo, "start"))
        work["event"]["event"] = "stop"
        work["turn"] = {"bad": "turn"}
        result = subprocess.run([sys.executable, str(ROOT / "memsift/run.py"), "stop", "--worker"],
                                input=json.dumps(work), text=True, capture_output=True,
                                env=child_env(self.fake.url, self.home), cwd=self.repo, timeout=8)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertIn("Traceback", self.log())

    def test_claude_failed_tool_event_does_not_record_a_write(self):
        from memsift import core, store
        core.prepare(event(self.repo, "start"))
        core.prepare(event(self.repo, "prompt", prompt="Write the grid"))
        result, _ = self.cli("tool", {"session_id": "a1", "cwd": str(self.repo),
                                     "hook_event_name": "PostToolUseFailure", "tool_name": "Write",
                                     "tool_input": {"file_path": str(self.repo / "grid.txt")},
                                     "error": "write refused"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(store.read_json(store.session_path("a1"), {})["actions"], [])
        self.assertEqual(self.fake.requests, [])

    def test_direct_execution_without_pythonpath_preserves_cwd(self):
        cache = self.home / "plugin cache with spaces"
        shutil.copytree(ROOT / "memsift", cache / "memsift", ignore=shutil.ignore_patterns("__pycache__"))
        environment = child_env(self.fake.url, self.home)
        environment.pop("PYTHONPATH")
        result = subprocess.run([sys.executable, str(cache / "memsift/run.py"), "tool", "--host", "codex"],
                                input=json.dumps({"session_id": "direct", "cwd": str(self.repo),
                                                  "tool_name": "Read", "tool_input": {}}),
                                env=environment, cwd=self.repo, text=True, capture_output=True, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.log(), "")

    def test_detached_worker_closes_inherited_fd_and_refuses_external_sockets(self):
        from memsift import store
        environment = child_env(self.fake.url, self.home)
        marker = self.home / "worker-checked"
        with (self.home / "inherited-fd").open("wb") as inherited:
            descriptor = inherited.fileno()
            bootstrap = self.home / ".memsift-test-bootstrap/sitecustomize.py"
            with bootstrap.open("a", encoding="utf-8") as script:
                script.write("\nimport os, socket, sys\nfrom pathlib import Path\n"
                             "from memsift import core\n"
                             "if '--worker' in sys.argv:\n"
                             "    def dispatch(work, config):\n"
                             "        try:\n"
                             "            socket.create_connection(('example.invalid', 80))\n"
                             "        except PermissionError:\n"
                             "            pass\n"
                             "        else:\n"
                             "            raise AssertionError('worker socket unguarded')\n"
                             "        try:\n"
                             "            os.fstat(" + str(descriptor) + ")\n"
                             "        except OSError:\n"
                             "            pass\n"
                             "        else:\n"
                             "            raise AssertionError('descriptor inherited')\n"
                             "        Path(" + repr(str(marker)) + ").touch()\n"
                             "        return ''\n"
                             "    core.dispatch = dispatch\n")
            result = subprocess.run([sys.executable, str(ROOT / "memsift/run.py"), "start"],
                                    input=json.dumps({"session_id": "fd", "cwd": str(self.repo), "source": "startup"}),
                                    capture_output=True, text=True, env=environment, cwd=self.repo,
                                    pass_fds=(descriptor,), timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.wait_for(marker.exists)
        self.assertEqual(self.log(), "")
        self.assertTrue(store.session_path("fd").exists())
        # The same bootstrap protects the foreground process before DNS too.
        result = subprocess.run([sys.executable, "-c", "import socket; socket.create_connection(('example.invalid',80))"],
                                capture_output=True, text=True, env=environment, cwd=self.repo, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tests refuse non-loopback", result.stderr)

    def test_explicit_commands_use_current_repo_and_capture_resume_context(self):
        from memsift import core, store
        from tests.test_compaction import answer, envelope
        environment = child_env(self.fake.url, self.home)
        for kind in ("doctor", "resume"):
            self.fake.push([], path="/api/v1/datasets")
            self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}},
                           path="/api/alpha/decisions")
            self.fake.push(envelope(answer()), path="/chat/completions")
            result = subprocess.run([sys.executable, str(ROOT / "memsift/run.py"), kind, "--host", "codex",
                                     "--cwd", str(self.repo)], input="", text=True, capture_output=True,
                                    env=environment, cwd=self.home, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Jev", result.stdout)
            self.assertIn("✓", result.stdout)
        core.prepare(event(self.repo, "start"))
        store.update_session_if_exists("a1", lambda s: dict(s, actions=[
            {"turn": 1, "prompt": "Keep twelve columns.", "items": ["Edit grid.txt"]}]))
        store.queue_turn(self.repo, turn(), 3)
        self.fake.push(stop_body(), path="/api/alpha/decisions")
        with patch("memsift.jev.probe"), patch("memsift.cognee.probe"), patch("memsift.compactor.probe"):
            result, output = self.invoke(["resume", "--cwd", str(self.repo)],
                                         json.dumps({"session_id": "a1", "cwd": str(self.repo), "prompt": "Resume"}))
        self.assertEqual(result, 0)
        self.assertIn("1 turns sorted, 1 kept", output)
        self.assertEqual(self.fake.requests[-1]["body"]["state"]["recent_actions"], ["Edit grid.txt"])

    def test_slow_parallel_cognee_body_is_discarded_at_shared_deadline(self):
        from memsift import breaker, core, store
        (self.repo / "jev.md").write_text("Grid", encoding="utf-8")
        core.prepare(event(self.repo, "start"))
        store.write_text(store.home() / "datasets.md", "- A: Lessons\n- B: Decisions\n")
        store.write_text(store.repo_dir(self.repo) / "cognee-index.json", json.dumps({"datasets": [
            {"name": "A", "id": "a", "items": []}, {"name": "B", "id": "b", "items": []}]}))
        for _ in range(2):
            self.fake.push({"answers": {"has_info": noul(0), "cognee": noul(1),
                                        "dataset_A": noul(1), "dataset_B": noul(1)},
                            "usage": {"input_tokens": 500}}, path="/api/alpha/decisions")
        self.fake.push(["fast passage"], path="/api/v1/recall")
        self.fake.push_chunks([b'["late', b' passage"]'], interval=13, path="/api/v1/recall")
        result, elapsed = self.cli("prompt", {"session_id": "a1", "cwd": str(self.repo), "prompt": "Continue"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertGreater(elapsed, 11.8)
        self.assertLess(elapsed, 13)
        self.assertEqual(breaker.state()["services"]["cognee"]["failures"], 1)
        self.assertEqual(len(self.fake.requests), 4)

    def test_manifest_routes_and_command_discovery(self):
        for host, manifest, root_variable in (("claude-code", ".claude-plugin", "CLAUDE_PLUGIN_ROOT"),
                                               ("codex", ".codex-plugin", "PLUGIN_ROOT")):
            path = ROOT / manifest / "plugin.json"
            self.assertTrue(path.exists(), "host manifest missing")
            data = json.loads(path.read_text())
            self.assertEqual(data["hooks"], "./hooks/" + host + ".json")
            routes = json.loads((ROOT / data["hooks"]).read_text())["hooks"]
            expected = {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}
            if host == "claude-code":
                expected.add("PostToolUseFailure")
            self.assertEqual(set(routes), expected)
            self.fake.push([], path="/api/v1/datasets")
            command = routes["SessionStart"][0]["hooks"][0]
            env = child_env(self.fake.url, self.home)
            env[root_variable] = str(ROOT)
            result = subprocess.run(command["command"], shell=True, env=env, cwd=self.repo,
                                    input=json.dumps({"session_id": "manifest-" + host, "cwd": str(self.repo),
                                                      "source": "startup"}), capture_output=True, text=True, timeout=5)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.wait_for(lambda: len(self.fake.requests) >= (1 if host == "claude-code" else 2))
            if host == "codex":
                self.assertEqual(data["skills"], "./skills/")
                self.assertEqual(routes["SessionStart"][0]["matcher"], "startup|resume|clear|compact|fork")
                for name in ("resume", "doctor"):
                    self.assertTrue((ROOT / data["skills"] / ("memsift-" + name) / "SKILL.md").exists())
            else:
                for name in ("resume", "doctor"):
                    self.assertTrue((ROOT / "commands" / ("memsift-" + name + ".md")).exists())
            for entries in routes.values():
                for entry in entries:
                    self.assertGreaterEqual(entry["hooks"][0]["timeout"], 20)
        claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(claude["plugins"][0]["source"], "./")
        self.assertEqual(codex["plugins"][0]["source"]["path"], "./")

