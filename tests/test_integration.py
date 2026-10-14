"""Both hosts through the runner, with real workers and loopback-only services."""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.support import IsolatedTest, child_env
from tests.test_compaction import answer, envelope
from tests.test_core import stop_body
from tests.test_decisions import noul
from tests.test_host_contracts import load_fixture

ROOT = Path(__file__).parents[1]
SECRETS = (
    ("PASSWORD=invented-password-value", "invented-password-value"),
    ("-----BEGIN PRIVATE KEY-----\ninvented-private-key-body\n-----END PRIVATE KEY-----",
     "invented-private-key-body"),
    ("sk-proj-abcdefghijklmnop123456", "sk-proj-abcdefghijklmnop123456"),
    ("ghp_abcdefghijklmnop123456", "ghp_abcdefghijklmnop123456"),
    ("xoxb-abcdefghijklmnop123456", "xoxb-abcdefghijklmnop123456"),
)


class IntegrationTests(IsolatedTest):
    def invoke(self, kind, host_name, sid="acme-shop-a", **changes):
        from memsift import run
        if kind in ("resume", "doctor"):
            payload = ""
            args = [kind, "--host", host_name, "--cwd", str(self.repo)]
        else:
            name = "tool-edit" if kind == "tool" else kind
            raw = load_fixture(ROOT / "tests/fixtures/hooks" / host_name / (name + ".json"),
                               self.repo, self.transcript)
            value = dict(json.loads(raw), session_id=sid, **changes)
            payload = json.dumps(value)
            args = [kind, "--host", host_name]
        output = io.StringIO()
        with patch.dict(os.environ, child_env(self.fake.url, self.home), clear=True), patch(
                "sys.stdin", io.StringIO(payload)), contextlib.redirect_stdout(output):
            result = run.main(args)
            # Wait before inspecting effects or deleting the isolated HOME.
            for worker in run._workers:
                try:
                    self.assertEqual(worker.wait(timeout=15), 0)
                finally:
                    if worker.poll() is None:
                        worker.kill()
                        worker.wait()
            run._workers.clear()
        self.assertEqual(result, 0)
        text = output.getvalue()
        if kind == "prompt" and text:
            value = json.loads(text)["hookSpecificOutput"]
            self.assertEqual(value["hookEventName"], "UserPromptSubmit")
            return value["additionalContext"]
        if kind not in ("resume", "doctor"):
            self.assertEqual(text, "")
        return text

    def push_prompt(self, has_info=1, cognee=0, **choices):
        from memsift import store
        for _ in range(2 + bool(store.read_notes(self.repo)["entries"])):
            self.fake.push({"answers": {"has_info": noul(has_info), "cognee": noul(cognee),
                                        "dataset_memsift": noul(0),
                                        **{key: noul(value) for key, value in choices.items()}},
                            "usage": {"input_tokens": 500}},
                           path="/api/alpha/decisions")

    def push_index(self):
        self.fake.push([{"id": "acme-memory", "name": "memsift"}], path="/api/v1/datasets")
        self.fake.push([], path="/api/v1/datasets/acme-memory/data")

    def push_probes(self):
        self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}},
                       path="/api/alpha/decisions")
        self.fake.push(envelope(answer()), path="/chat/completions")
        self.fake.push([{"id": "acme-memory", "name": "memsift"}], path="/api/v1/datasets")

    def assert_private(self):
        for path in (self.home / ".memsift").rglob("*"):
            for _, secret in SECRETS:
                self.assertNotIn(secret, path.name)
            if path.is_file():
                text = path.read_bytes().decode("utf-8", errors="replace")
                for _, secret in SECRETS:
                    with self.subTest(artifact=path.name, secret=secret):
                        self.assertNotIn(secret, text)
                self.assertNotIn("DO_NOT_SEND_TOOL_OUTPUT", text)
        for request in self.fake.requests:
            content = repr((request["url"], request["body"]))
            for _, secret in SECRETS:
                self.assertNotIn(secret, content)
            self.assertNotIn("DO_NOT_SEND_TOOL_OUTPUT", content)
            headers = {key.lower(): value for key, value in request["headers"].items()}
            if "/api/v1/" in request["url"]:
                self.assertEqual(headers["x-api-key"], self.config["COGNEE_API_KEY"])
            else:
                key = "OPENROUTER_API_KEY" if "/decisions" in request["url"] else "MEMSIFT_COMPACTOR_API_KEY"
                self.assertEqual(headers["authorization"], "Bearer " + self.config[key])

    def lifecycle(self, host_name):
        from memsift import breaker, store
        self.transcript = self.home / (host_name + ".jsonl")
        self.transcript.write_bytes((ROOT / "tests/fixtures/transcripts" /
                                     (host_name + "-plain.jsonl")).read_bytes())
        card = self.repo / "jev.md"
        secret_input = "\n".join(content for content, _ in SECRETS)
        card.write_text("acme-shop: product catalog and twelve-column grid.\n" + secret_input,
                        encoding="utf-8")
        descriptions = store.home() / "datasets.md"
        store.write_text(descriptions, "- memsift: past acme-shop decisions " + SECRETS[0][0] + "\n")
        owned = {path: path.read_bytes() for path in (card, descriptions)}
        self.push_index()
        self.invoke("start", host_name)
        prompt = "Use the footer grid because the header must align."
        self.push_prompt()
        before = len(self.fake.requests)
        self.assertEqual(self.invoke("prompt", host_name, prompt=prompt + "\n" + secret_input), "")
        self.assertEqual(len(self.fake.requests) - before, 2)
        state = next(r["body"]["state"] for r in self.fake.requests[before:]
                     if "has_info" in r["body"]["questions"])
        self.assertEqual({row["role"] for row in state["conversation"]}, {"user", "assistant"})
        self.assertNotIn("notes", state)
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 0)
        self.invoke("tool", host_name)
        self.fake.push(stop_body(), path="/api/alpha/decisions")
        self.invoke("stop", host_name, last_assistant_message="The header uses the footer grid.\n" + secret_input)
        first = store.read_notes(self.repo)["entries"][0]
        self.assertIn("User: " + prompt, first["text"])
        self.assertIn("Agent: The header uses the footer grid.", first["text"])
        self.assertIn("src/header.tsx", first["text"])
        self.assertEqual(first["nature"], "project")

        self.push_index()
        self.invoke("start", host_name, sid="acme-shop-b")
        self.push_prompt(0, entry_d1=1, dataset_memsift=0)
        self.assertEqual(self.invoke("prompt", host_name, sid="acme-shop-b", prompt="Back to the header."),
                         "memsift, from the working notes:\n" + first["text"])
        self.push_prompt(entry_d1=0, dataset_memsift=0)
        correction = "No, keep twelve columns even on the narrow header."
        self.invoke("prompt", host_name, sid="acme-shop-b", prompt=correction)
        self.fake.push(stop_body(correction=noul(1)), path="/api/alpha/decisions")
        self.invoke("stop", host_name, sid="acme-shop-b", last_assistant_message="I will keep twelve columns.")
        self.assertEqual(set(store.read_notes(self.repo)["pending"]), {"d2"})
        self.push_prompt(entry_d1=0, entry_d2=0, dataset_memsift=0)
        self.invoke("prompt", host_name, sid="acme-shop-b", prompt="Apply that correction.")
        self.invoke("tool", host_name, sid="acme-shop-b")
        self.fake.push(stop_body("nothing", confirmed_d2=noul(1)), path="/api/alpha/decisions")
        self.invoke("stop", host_name, sid="acme-shop-b", last_assistant_message="Applied and checked.")
        self.assertEqual(store.read_notes(self.repo)["pending"], {})
        self.assertIn("- [d2] confirmed\n", store.read_notes(self.repo)["draft"])

        self.fake.push(envelope(answer("The header reuses the footer grid.\n" + secret_input)),
                       path="/chat/completions")
        self.fake.push({"error": "PASSWORD=invented-password-value"}, status=401, path="/api/v1/remember")
        self.invoke("end", host_name, sid="acme-shop-b")
        self.assertFalse(store.session_path("acme-shop-b").exists())
        self.assertNotIn("User:", store.read_notes(self.repo)["draft"])
        self.assertFalse(any(entry["id"].startswith("d") for entry in store.read_notes(self.repo)["entries"]))
        self.assertIn("[g1] The header reuses the footer grid.", store.read_notes(self.repo)["digest"])
        packets = list((store.repo_dir(self.repo) / "outbox").glob("*.txt"))
        self.assertEqual(len(packets), 1)
        upload = next(request for request in self.fake.requests if "/remember" in request["url"])
        self.assertIn(b'name="datasetName"\r\n\r\nmemsift\r\n', upload["body"])
        self.assertTrue(breaker.paused("cognee"))
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

        self.invoke("start", host_name, sid="acme-shop-c")
        breaker.record("jev", {"kind": "auth", "reason": "invented refusal", "status": 401})
        before = len(self.fake.requests)
        queued_prompt = "Remember the shipping rule.\n" + secret_input
        self.assertIn("Memory is off", self.invoke("prompt", host_name, sid="acme-shop-c", prompt=queued_prompt))
        self.assertEqual(self.invoke("prompt", host_name, sid="acme-shop-c", prompt=queued_prompt), "")
        self.invoke("stop", host_name, sid="acme-shop-c", last_assistant_message="Ship after checks pass.\n" + secret_input)
        self.assertEqual(len(self.fake.requests), before)
        self.assertEqual(len(store.pending_turns(self.repo)), 1)
        self.push_probes()
        self.fake.push({"ok": True}, path="/api/v1/remember")
        self.fake.push(stop_body(), path="/api/alpha/decisions")
        result = self.invoke("resume", host_name)
        self.assertIn("1 turns sorted, 1 kept", result)
        self.assertIn("Cognee ✓", result)
        self.assertFalse(packets[0].exists())
        self.assertEqual(store.pending_turns(self.repo), [])
        self.assertIn("User: Remember the shipping rule.", store.read_notes(self.repo)["draft"])
        for service in breaker.state()["services"].values():
            self.assertEqual(service, {"failures": 0, "paused": False, "reason": ""})
        for sid in ("acme-shop-a", "acme-shop-c"):
            self.assertEqual(store.read_json(store.session_path(sid), {})["outage_notices"], [])
        before = len(self.fake.requests)
        self.push_prompt(0, 1, entry_g1=0, entry_d1=0, dataset_memsift=1)
        self.fake.push([{"text": "Allow sharp build scripts.\n" + secret_input}], path="/api/v1/recall")
        self.fake.push({"answers": {"passage_1": noul(1)},
                        "usage": {"input_tokens": 500}}, path="/api/alpha/decisions")
        result = self.invoke("prompt", host_name, sid="acme-shop-c", prompt="What fixed sharp?\n" + secret_input)
        self.assertIn("memsift, from Cognee (dataset memsift):\nAllow sharp build scripts.", result)
        *first_requests, recall, passage = self.fake.requests[before:]
        self.assertEqual(len(first_requests), 3)
        first_request = first_requests[0]
        self.assertEqual(recall["body"]["dataset_ids"], ["acme-memory"])
        self.assertLessEqual(first_request["timeout"], 2)
        self.assertLessEqual(recall["timeout"], 12)
        self.assertLessEqual(passage["timeout"], 2)
        before = breaker.state()
        self.push_probes()
        result = self.invoke("doctor", host_name)
        self.assertNotIn("✗", result)
        self.assertIn("0 turns pending · 0 packets waiting", result)
        self.assertEqual(breaker.state(), before)
        for path, original in owned.items():
            self.assertEqual(path.read_bytes(), original)
        self.assert_private()

    def test_claude_code_lifecycle(self):
        self.lifecycle("claude-code")

    def test_codex_lifecycle(self):
        self.lifecycle("codex")

    def test_no_secret_in_any_persisted_artifact(self):
        from memsift import cognee, store
        from memsift.vendor.logfiles import error
        for index, (content, _) in enumerate(SECRETS):
            turn = {"ts": "2026-10-14T07:31:02Z", "session": "acme-shop-a",
                    "prompt": content, "answer": content, "actions": [content]}
            store.append_draft(self.repo, turn, "project", False, 3.0)
            store.queue_turn(self.repo, turn, 3.0)
            store.write_text(store.repo_dir(self.repo) / "digest.md", content)
            store.write_text(store.repo_dir(self.repo) / "cognee-index.json", json.dumps({"items": [content]}))
            store.update_json(store.session_path("acme-shop-a"), {}, lambda _: {"prompt": content})
            name = "acme-shop 2026-10-14 15h32 grid %s %s.txt" % (index, content.replace("\n", " "))
            cognee.queue_packet(self.repo, name, content)
            try:
                raise ValueError(content)
            except ValueError as exc:
                error("stop", exc, response=content)
            self.fake.push({"ok": True}, path="/api/v1/remember")
            self.assert_private()
        self.assertEqual(cognee.send_outbox(store.repo_dir(self.repo), self.config), len(SECRETS))
        self.assertEqual(len(self.fake.requests), len(SECRETS))
        self.assert_private()

    def test_every_case_and_discovery_import_uses_isolated_home(self):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
        def cases(group):
            for item in group:
                if isinstance(item, unittest.TestSuite):
                    yield from cases(item)
                else:
                    yield item
        for case in cases(suite):
            self.assertIsInstance(case, IsolatedTest, case.id())
        environment = child_env(self.fake.url, self.home)
        source = """
import os, sys, unittest
from pathlib import Path
from unittest.mock import patch
home = Path(os.environ['HOME'])
def forbidden(*args, **kwargs):
    raise AssertionError('discovery accessed a user file')
with patch.object(Path, 'open', forbidden), patch('builtins.open', forbidden):
    suite = unittest.defaultTestLoader.discover(sys.argv[1], top_level_dir=sys.argv[2])
    def cases(group):
        for item in group:
            if isinstance(item, unittest.TestSuite):
                yield from cases(item)
            else:
                yield item
    assert all(not isinstance(case, unittest.loader._FailedTest) for case in cases(suite))
assert Path.home() == home
assert not (home / '.memsift').exists()
"""
        result = subprocess.run([sys.executable, "-c", source, str(ROOT / "tests"), str(ROOT)],
                                env=environment, cwd=self.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_network_guard_cannot_be_bypassed_by_raw_sockets(self):
        with patch("tests.fake_services._ORIGINAL_CONNECT", side_effect=AssertionError("unguarded connect")), patch(
                "tests.fake_services._ORIGINAL_GETADDRINFO", side_effect=AssertionError("unguarded DNS")):
            with self.assertRaises(PermissionError):
                socket.getaddrinfo("service.invalid", 443)
            for operation in ("connect", "connect_ex"):
                with socket.socket() as client, self.assertRaises(PermissionError):
                    getattr(client, operation)(("192.0.2.1", 443))
        source = """
import os, socket, unittest
from pathlib import Path
case = unittest.TestCase()
assert Path.home() == Path(os.environ['HOME'])
for operation in ('connect', 'connect_ex'):
    with socket.socket() as client, case.assertRaises(PermissionError):
        getattr(client, operation)(('192.0.2.1', 443))
"""
        child = source + "\nimport subprocess, sys\nsubprocess.run([sys.executable, '-c', %r], check=True)\n" % source
        result = subprocess.run([sys.executable, "-c", child], env=child_env(self.fake.url, self.home),
                                cwd=self.repo, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_logging_lock_error_preserves_record_and_redacted_traceback(self):
        from memsift import store
        from memsift.vendor.logfiles import append_line
        path = store.home() / "debug.log"
        with patch("memsift.vendor.file_lock.file_lock", side_effect=OSError("PASSWORD=invented-password-value")):
            self.assertTrue(append_line(path, "acme-shop diagnostic"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("acme-shop diagnostic\n", text)
        self.assertIn("ERROR log-lock", text)
        self.assertIn("Traceback", text)
        self.assert_private()

