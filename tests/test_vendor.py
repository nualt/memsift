"""Offline checks for the retained upstream HTTP and safety modules."""

import contextlib
import errno
import importlib
import io
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
from unittest.mock import patch

from tests.support import IsolatedTest, child_env


class VendorTests(IsolatedTest):
    def log(self):
        path = self.home / ".memsift" / "debug.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_imports_do_not_read_config_build_openers_or_write(self):
        with patch("ssl.create_default_context", side_effect=AssertionError("import-time SSL")):
            for name in ("redact", "env_file", "file_lock", "logfiles", "recall_http", "remember_http", "recall_text", "dataset_access"):
                module = importlib.import_module("memsift.vendor." + name)
                importlib.reload(module)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse((self.home / ".memsift").exists())

    def test_environment_wins_and_backend_switch_is_ignored(self):
        from memsift.vendor.env_file import load_config
        path = self.home / ".cognee" / ".env"
        path.parent.mkdir()
        path.write_text("COGNEE_BASE_URL=https://file.invalid\nCOGNEE_BACKEND=local\nCOGNEE_API_KEY=invented-file-key\n", encoding="utf-8")
        with patch.dict(os.environ, {"COGNEE_BASE_URL": "https://env.invalid", "COGNEE_BACKEND": "local"}):
            config = load_config()
            self.assertEqual(config["COGNEE_BASE_URL"], "https://env.invalid")
            self.assertEqual(config["COGNEE_API_KEY"], "invented-cognee-key")
        self.assertNotIn("COGNEE_BACKEND", config)

    def test_only_nine_settings_and_no_legacy_env_file_override(self):
        from memsift.vendor.env_file import load_config
        rogue = self.home / "rogue.env"
        rogue.write_text("COGNEE_API_KEY=invented-rogue-key", encoding="utf-8")
        with patch.dict(os.environ, {"COGNEE_ENV_FILE": str(rogue), "COGNEE_PLUGIN_DATASET": "rogue", "COGNEE_CLAUDE_BACKEND": "local"}):
            config = load_config()
        self.assertEqual(len(config), 9)
        self.assertEqual(config["MEMSIFT_DATASET"], "memsift")
        self.assertEqual(config["COGNEE_API_KEY"], "invented-cognee-key")
        self.assertFalse((self.home / ".cognee").exists())

    def test_config_defaults_fallback_and_call_time_home(self):
        from memsift.vendor.env_file import load_config
        with patch.dict(os.environ, {"HOME": str(self.repo)}, clear=True):
            path = self.repo / ".cognee" / ".env"
            path.parent.mkdir()
            path.write_text("OPENROUTER_API_KEY=invented-file-key\nMEMSIFT_DEBUG=1\nPATH=rogue", encoding="utf-8")
            config = load_config()
            self.assertEqual(config["MEMSIFT_COMPACTOR_API_KEY"], "invented-file-key")
            self.assertEqual(config["MEMSIFT_JEV_MODEL"], "typesafe/jev-1.13")
            self.assertEqual(config["MEMSIFT_COMPACTOR_URL"], "https://openrouter.ai/api/v1")
            self.assertEqual(config["MEMSIFT_COMPACTOR_MODEL"], "anthropic/claude-haiku-5.5")
            self.assertEqual(config["MEMSIFT_DEBUG"], "1")
            self.assertNotIn("PATH", config)
            self.assertNotIn("PATH", os.environ)

    def test_dotenv_missing_and_parser_features(self):
        from memsift.vendor.env_file import parse_env_file
        path = self.home / "invented.env"
        self.assertEqual(parse_env_file(path), {})
        self.assertFalse(path.exists())
        path.write_text("\ufeff# comment\nexport KEY = 'invented=value'\nSECOND=\"literal $KEY\"\nno equals\nbad-key=ignored\n=ignored\n", encoding="utf-8")
        self.assertEqual(parse_env_file(path), {"KEY": "invented=value", "SECOND": "literal $KEY"})

    def test_dotenv_decoding_fallback_and_errors_are_logged(self):
        from memsift.vendor.env_file import parse_env_file
        path = self.home / "invented.env"
        path.write_bytes("KEY=invented-value".encode("utf-16"))
        self.assertEqual(parse_env_file(path), {"KEY": "invented-value"})
        self.assertIn("UnicodeDecodeError", self.log())
        path.write_bytes(b"\xff")
        self.assertEqual(parse_env_file(path), {})
        self.assertIn("Traceback", self.log())

    def test_dotenv_read_error_and_missing_file_race_are_logged(self):
        from memsift.vendor.env_file import parse_env_file
        path = self.home / "invented.env"
        path.touch()
        for exc in (PermissionError("PASSWORD=invented-value"), FileNotFoundError("invented race")):
            with self.subTest(exc=type(exc).__name__), patch.object(Path, "read_text", side_effect=exc):
                self.assertEqual(parse_env_file(path), {})
            self.assertIn(type(exc).__name__, self.log())
        self.assertNotIn("invented-value", self.log())

    def test_config_values_never_appear_in_output(self):
        from memsift.vendor.env_file import load_config
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            load_config()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(self.log(), "")

    def test_error_traceback_redaction_and_response_excerpt(self):
        from memsift.vendor.logfiles import error
        try:
            raise ValueError("PASSWORD=invented-value")
        except ValueError as exc:
            error("stop", exc, "cognee", 500, 0.125, "PASSWORD=invented-response " + "x" * 400 + "AFTER-EXCERPT")
        text = self.log()
        self.assertIn("Traceback", text)
        self.assertIn("ValueError", text)
        self.assertIn("cognee status=500 duration=0.125", text)
        self.assertNotIn("invented-value", text)
        self.assertNotIn("invented-response", text)
        self.assertNotIn("AFTER-EXCERPT", text)
        excerpt = text.split("duration=0.125\n", 1)[1].split("\n", 1)[0]
        self.assertEqual(len(excerpt), 300)

    def test_decision_debug_off_on_and_dotenv(self):
        from memsift.vendor.logfiles import decision
        decision("prompt", {"token_ratio": 3.2})
        self.assertEqual(self.log(), "")
        with patch.dict(os.environ, {"MEMSIFT_DEBUG": "1"}):
            decision("prompt", {"token_ratio": 3.2, "API_KEY": "invented-value"})
        self.assertIn('"token_ratio": 3.2', self.log())
        self.assertNotIn("invented-value", self.log())
        path = self.home / ".cognee" / ".env"
        path.parent.mkdir()
        path.write_text("MEMSIFT_DEBUG=1", encoding="utf-8")
        with patch.dict(os.environ):
            os.environ.pop("MEMSIFT_DEBUG")
            decision("from-file", {})
        self.assertIn("from-file", self.log())

    def test_log_rotates_before_crossing_cap_and_redacts(self):
        from memsift.vendor.logfiles import append_line
        path = self.home / ".memsift" / "debug.log"
        self.assertTrue(append_line(path, "first record", cap=32))
        self.assertTrue(append_line(path, "PASSWORD=invented-value", cap=32))
        self.assertEqual(path.with_name("debug.log.1").read_text(), "first record\n")
        self.assertEqual(path.read_text(), "[redacted:credential]\n")
        self.assertTrue(append_line(path, "third record", cap=32))
        self.assertLessEqual(sum(p.stat().st_size for p in (path, path.with_name("debug.log.1"))), 64)

    def test_oversized_record_truncates_without_secret_or_broken_utf8(self):
        from memsift.vendor.logfiles import append_line
        path = self.home / ".memsift" / "debug.log"
        for _ in range(3):
            self.assertTrue(append_line(path, "PASSWORD=invented-value " + "é" * 100, cap=64))
        for item in (path, path.with_name("debug.log.1")):
            self.assertLessEqual(item.stat().st_size, 64)
            text = item.read_text(encoding="utf-8")
            self.assertIn("[truncated]", text)
            self.assertNotIn("invented-value", text)
            self.assertTrue(text.endswith("\n"))

    def test_legacy_log_cap_cannot_disable_bound(self):
        from memsift.vendor import logfiles
        path = self.home / ".memsift" / "debug.log"
        with patch.dict(os.environ, {"COGNEE_PLUGIN_LOG_MAX_BYTES": "0"}), patch.object(logfiles, "LOG_MAX_BYTES", 64):
            self.assertTrue(logfiles.append_line(path, "x" * 100))
            self.assertLessEqual(path.stat().st_size, 64)

    def test_logging_lock_contention_still_appends_and_logs_traceback(self):
        from memsift.vendor.file_lock import file_lock
        from memsift.vendor.logfiles import append_line
        path = self.home / ".memsift" / "debug.log"
        with file_lock(path.with_name("debug.log.lock")) as acquired:
            self.assertTrue(acquired)
            self.assertTrue(append_line(path, "survives contention"))
        self.assertIn("survives contention", self.log())
        self.assertIn("Traceback", self.log())

    def test_rotation_failure_is_logged_and_cannot_exceed_cap(self):
        from memsift.vendor.logfiles import append_line
        path = self.home / "activity.log"
        self.assertTrue(append_line(path, "a" * 31, cap=32))
        with patch("os.replace", side_effect=PermissionError("PASSWORD=invented-value")):
            self.assertFalse(append_line(path, "next", cap=32))
        self.assertEqual(path.stat().st_size, 32)
        self.assertIn("PermissionError", self.log())
        self.assertNotIn("invented-value", self.log())

    def test_physically_unwritable_log_returns_false_without_recursion(self):
        from memsift.vendor.logfiles import append_line, error
        path = self.home / ".memsift" / "debug.log"
        path.parent.mkdir()
        path.mkdir()
        self.assertFalse(append_line(path, "record"))
        error("stop", RuntimeError("invented failure"))
        self.assertTrue(path.is_dir())

    def test_lock_nonblocking_timeout_and_release(self):
        from memsift.vendor.file_lock import file_lock
        path = self.home / "notes.lock"
        with file_lock(path) as first:
            self.assertTrue(first)
            start = time.monotonic()
            with file_lock(path, timeout=0.03) as second:
                self.assertFalse(second)
            self.assertLess(time.monotonic() - start, 0.5)
        with file_lock(path) as third:
            self.assertTrue(third)
        self.assertIn("Traceback", self.log())

    def test_actual_lock_error_is_logged_and_raised(self):
        if os.name == "nt":
            self.skipTest("POSIX fault injection")
        from memsift.vendor.file_lock import file_lock
        with patch("fcntl.flock", side_effect=OSError(errno.EIO, "PASSWORD=invented-value")):
            with self.assertRaises(OSError):
                with file_lock(self.home / "notes.lock"):
                    self.fail("lock error must propagate")
        self.assertIn("OSError", self.log())
        self.assertNotIn("invented-value", self.log())

    def test_dataset_fields_ignore_inherited_read_routing(self):
        from memsift.vendor.dataset_access import dataset_id, recall_fields, write_fields
        ident = "12345678-1234-5678-1234-567812345678"
        with patch.dict(os.environ, {"COGNEE_PLUGIN_READ_DATASET_IDS": '["87654321-4321-8765-4321-876543218765"]'}):
            self.assertEqual(recall_fields("memsift", ["graph"]), ({"datasets": ["memsift"]}, False))
            self.assertEqual(recall_fields(ident, ["graph"]), ({"dataset_ids": [ident]}, False))
        self.assertEqual(write_fields(ident), {"dataset_id": ident})
        self.assertEqual(write_fields("memsift"), {"dataset_name": "memsift"})
        self.assertEqual(dataset_id(None), "")
        self.assertIn("Traceback", self.log())

    def recall(self, **kwargs):
        from memsift.vendor.recall_http import do_recall
        return do_recall(self.config["COGNEE_BASE_URL"], self.config["COGNEE_API_KEY"], "PASSWORD=invented-query", "acme-session", ["graph"], 5, dataset="memsift", **kwargs)

    def remember(self, **kwargs):
        from memsift.vendor.remember_http import do_remember
        return do_remember(self.config["COGNEE_BASE_URL"], self.config["COGNEE_API_KEY"], "PASSWORD=invented-content", "memsift", "acme-shop", **kwargs)

    def test_recall_request_auth_content_and_call_time_opener(self):
        self.fake.push(["one", "two"])
        self.assertEqual(self.recall(), ["one", "two"])
        request = self.fake.requests[0]
        self.assertEqual(request["body"], {"query": "[redacted:credential]", "top_k": 5, "only_context": True, "scope": ["graph"], "session_id": "acme-session", "datasets": ["memsift"]})
        self.assertEqual(request["headers"]["X-Api-Key"], "invented-cognee-key")
        self.assertGreater(request["timeout"], 119)
        self.assertLessEqual(request["timeout"], 120.0)

    def test_recall_explicit_dataset_ids_and_legacy_settings(self):
        self.fake.push([])
        ident = "12345678-1234-5678-1234-567812345678"
        with patch.dict(os.environ, {"COGNEE_PLUGIN_READ_DATASET_IDS": "invalid", "COGNEE_RECALL_CONTEXT_CHARS": "0"}):
            self.assertEqual(self.recall(dataset_ids=ident, context_profile="PASSWORD=invented-profile", opener=self.fake.open), [])
        body = self.fake.requests[0]["body"]
        self.assertEqual(body["dataset_ids"], [ident])
        self.assertNotIn("datasets", body)
        self.assertNotIn("invented-profile", json.dumps(body))

    def test_recall_failed_calls_raise_original_and_log_redacted_diagnostics(self):
        for body, status, kind in (({"detail": "PASSWORD=invented-response"}, 401, urllib.error.HTTPError), ("invalid JSON", 200, json.JSONDecodeError), ({"error": "PASSWORD=invented-response"}, 200, ValueError), ({"unexpected": True}, 200, ValueError), ("", 200, json.JSONDecodeError), ([], 404, urllib.error.HTTPError)):
            with self.subTest(status=status, body=body):
                self.fake.push(body, status=status)
                with self.assertRaises(kind):
                    self.recall(opener=self.fake.open)
        self.assertIn("Traceback", self.log())
        self.assertIn("cognee status=401", self.log())
        self.assertNotIn("invented-response", self.log())

    def test_recall_injected_transport_error_is_same_exception(self):
        exc = ConnectionRefusedError("PASSWORD=invented-value")
        self.fake.push_error(exc)
        with self.assertRaises(ConnectionRefusedError) as caught:
            self.recall(opener=self.fake.open)
        self.assertIs(caught.exception, exc)
        self.assertIn("ConnectionRefusedError", self.log())
        self.assertNotIn("invented-value", self.log())

    def test_recall_delayed_headers_obey_wall_clock_deadline(self):
        self.fake.push([], delay=0.8)
        start = time.monotonic()
        with self.assertRaises((TimeoutError, urllib.error.URLError)):
            self.recall(opener=self.fake.open, timeout=0.15)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_recall_slow_body_deadline_discards_late_result(self):
        from memsift.vendor import recall_http
        self.fake.push_chunks([b"["] + [b" "] * 18 + [b"]"], interval=0.05)
        finished = threading.Event()
        actual = recall_http._recall_once
        def observed(*args, **kwargs):
            try:
                return actual(*args, **kwargs)
            finally:
                finished.set()
        with patch.object(recall_http, "_recall_once", side_effect=observed):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                self.recall(opener=self.fake.open, timeout=0.15)
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertTrue(finished.wait(2.0))
        self.fake.push(["next call"])
        self.assertEqual(self.recall(opener=self.fake.open), ["next call"])
        self.assertEqual(len(self.fake.requests), 2)

    def test_recall_late_error_is_logged_without_delivery(self):
        from memsift.vendor import recall_http
        finished = threading.Event()
        def late(*args, **kwargs):
            time.sleep(0.1)
            try:
                raise ValueError("PASSWORD=invented-value")
            finally:
                finished.set()
        with patch.object(recall_http, "_recall_once", side_effect=late):
            with self.assertRaises(TimeoutError):
                self.recall(timeout=0.01)
            self.assertTrue(finished.wait(1))
            # Join the daemon after its exception has been logged, before HOME cleanup.
            for thread in threading.enumerate():
                if thread.name == "memsift-recall":
                    thread.join(1)
        self.assertIn("ValueError", self.log())
        self.assertNotIn("invented-value", self.log())

    def test_remember_multipart_ignores_legacy_polling_and_preserves_auth(self):
        self.fake.push({"dataset_id": "12345678-1234-5678-1234-567812345678", "status": "queued"})
        with patch.dict(os.environ, {"COGNEE_REMEMBER_BACKGROUND": "false", "COGNEE_REMEMBER_WAIT_SECONDS": "100", "COGNEE_PLUGIN_DATASET": "rogue"}):
            self.assertEqual(self.remember(), {"ok": True, "dataset_id": "12345678-1234-5678-1234-567812345678", "status": "queued"})
        self.assertEqual(len(self.fake.requests), 1)
        request = self.fake.requests[0]
        text = request["body"].decode("utf-8")
        self.assertIn('name="datasetName"\r\n\r\nmemsift', text)
        self.assertIn('name="run_in_background"\r\n\r\ntrue', text)
        self.assertIn("[redacted:credential]", text)
        self.assertNotIn("invented-content", text)
        self.assertNotIn("rogue", text)
        self.assertEqual(request["headers"]["X-Api-Key"], "invented-cognee-key")

    def test_remember_file_name_content_and_explicit_dataset(self):
        path = self.repo / "acme-shop 2026-10-14 10h15 checkout.txt"
        path.write_text("API_KEY=invented-file-secret", encoding="utf-8")
        self.fake.push({"ok": True})
        self.assertEqual(self.remember(file_path=path, dataset_id="12345678-1234-5678-1234-567812345678", opener=self.fake.open), {"ok": True})
        text = self.fake.requests[0]["body"].decode()
        self.assertIn('filename="acme-shop 2026-10-14 10h15 checkout.txt"', text)
        self.assertIn('name="datasetId"', text)
        self.assertNotIn("invented-file-secret", text)

    def test_remember_failed_calls_raise_and_log(self):
        for body, status, kind in (({}, 409, urllib.error.HTTPError), ("malformed", 200, json.JSONDecodeError), ({"error": "PASSWORD=invented-response"}, 200, ValueError), ([], 200, ValueError), ({"ok": False}, 200, ValueError)):
            with self.subTest(status=status, body=body):
                self.fake.push(body, status=status)
                with self.assertRaises(kind):
                    self.remember(opener=self.fake.open)
        self.assertIn("cognee status=409", self.log())
        self.assertNotIn("invented-response", self.log())

    def test_remember_transport_and_file_errors_raise_original(self):
        exc = TimeoutError("PASSWORD=invented-value")
        self.fake.push_error(exc)
        with self.assertRaises(TimeoutError) as caught:
            self.remember(opener=self.fake.open)
        self.assertIs(caught.exception, exc)
        with self.assertRaises(FileNotFoundError):
            self.remember(file_path=self.repo / "missing.txt")
        self.assertIn("FileNotFoundError", self.log())
        self.assertNotIn("invented-value", self.log())

    def test_recall_text_extracts_wrapper_sections_and_passages(self):
        from memsift.vendor.recall_text import split_only_context, split_sections, split_passages
        context = "preamble\n## Relevant passages\nfirst\n\n---\n\ninside first\n---\nsecond\n## Relevant entities\nentity\n## Related facts\nfact"
        wrapper = "old conversation\nThe question is: `checkout` context: `" + context + "`\n\n## Active session guidance\nignore this"
        layers = split_only_context(wrapper, "checkout")
        self.assertEqual(layers, {"history": "old conversation", "context": context, "guidance": "## Active session guidance\nignore this", "parsed": True})
        sections = split_sections(layers["context"])
        self.assertEqual(split_passages(sections["passages"]), ["first\n\n---\n\ninside first", "second"])
        self.assertEqual(sections["entities"], "entity\n")
        self.assertEqual(sections["facts"], "fact")
        self.assertEqual(split_only_context("plain context", "checkout"), {"history": "", "context": "plain context", "guidance": "", "parsed": False})


    def test_http_error_body_read_failure_keeps_original_and_logs_both_errors(self):
        class BrokenBody(io.BytesIO):
            def read1(self, *args):
                raise OSError("PASSWORD=invented-body-secret")
        for operation in (self.recall, self.remember):
            with self.subTest(operation=operation.__name__):
                exc = urllib.error.HTTPError("https://service.invalid/api/v1", 503,
                                             "invented failure", {}, BrokenBody())
                self.fake.push_error(exc)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    operation(opener=self.fake.open)
                self.assertIs(caught.exception, exc)
                self.assertIn("OSError", self.log())
                self.assertIn("HTTPError", self.log())
                self.assertIn("cognee status=503", self.log())
                self.assertNotIn("invented-body-secret", self.log())

    def test_recall_invalid_utf8_body_retains_http_status(self):
        self.fake.push(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            self.recall(opener=self.fake.open)
        self.assertIn("cognee status=200", self.log())
        self.assertIn("UnicodeDecodeError", self.log())

    def test_remember_rejects_error_envelopes_and_nonboolean_ok(self):
        for body in ({"error": ""}, {"error": None}, {"ok": "false"}, {"ok": 0}, {"ok": None}):
            with self.subTest(body=body):
                self.fake.push(body)
                with self.assertRaises(ValueError):
                    self.remember(opener=self.fake.open)

    def test_transport_logs_do_not_include_authentication_headers_or_keys(self):
        from memsift.vendor.recall_http import do_recall
        from memsift.vendor.remember_http import do_remember
        for operation in (do_recall, do_remember):
            self.fake.push({"error": "invented failure"}, status=403)
            args = ("checkout", "acme-session", '["graph"]', "5") if operation is do_recall else ("checkout", "memsift", "acme-shop")
            with self.assertRaises(urllib.error.HTTPError):
                operation(self.fake.url, "invented-authentication-secret", *args, opener=self.fake.open)
        self.assertNotIn("invented-authentication-secret", self.log())
        self.assertNotIn("X-Api-Key", self.log())
        self.assertTrue(all(record["headers"]["X-Api-Key"] == "invented-authentication-secret" for record in self.fake.requests))

    def test_multipart_redacts_fields_and_filename_before_sanitizing_headers(self):
        from memsift.vendor.remember_http import do_remember
        path = self.repo / 'PASSWORD=invented-filename-secret"\nextra.txt'
        path.write_text("safe content", encoding="utf-8")
        self.fake.push({"ok": True})
        do_remember(self.fake.url, "invented-key", "ignored", "API_KEY=invented-dataset-secret",
                    "PASSWORD=invented-node-secret", file_path=path, opener=self.fake.open)
        record = self.fake.requests[0]
        wire = record["body"]
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + record["headers"]["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + wire
        )
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 4)
        fields = {part.get_param("name", header="content-disposition"): part.get_content().strip() for part in parts}
        self.assertEqual(fields["datasetName"], "[redacted:credential]")
        self.assertEqual(fields["node_set"], "[redacted:credential]")
        self.assertEqual(fields["data"], "safe content")
        self.assertNotIn("invented-filename-secret", parts[-1].get_filename())
        self.assertNotIn("\n", parts[-1].get_filename())
        self.assertNotIn(b"invented-dataset-secret", wire)
        self.assertNotIn(b"invented-node-secret", wire)

    def test_complete_traceback_record_is_preserved_without_truncation(self):
        from memsift.vendor import logfiles
        try:
            raise RuntimeError("invented failure")
        except RuntimeError as exc:
            with patch.object(logfiles, "LOG_MAX_BYTES", 4000):
                logfiles.error("stop", exc)
        self.assertIn("Traceback (most recent call last):", self.log())
        self.assertIn("RuntimeError: invented failure", self.log())
        self.assertNotIn("[truncated]", self.log())

    def test_cross_process_lock_contention_and_release(self):
        from memsift.vendor.file_lock import file_lock
        path = self.home / "notes.lock"
        script = """
from pathlib import Path
from memsift.vendor.file_lock import file_lock
import sys
with file_lock(Path(sys.argv[1])) as acquired:
    print(int(acquired))
"""
        with file_lock(path) as acquired:
            self.assertTrue(acquired)
            result = subprocess.run([sys.executable, "-c", script, str(path)],
                                    env=child_env(self.fake.url, self.home), capture_output=True,
                                    text=True, check=True, timeout=5)
            self.assertEqual(result.stdout.strip(), "0")
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                env=child_env(self.fake.url, self.home), capture_output=True,
                                text=True, check=True, timeout=5)
        self.assertEqual(result.stdout.strip(), "1")

    def test_write_fault_is_logged_and_does_not_retry_partial_append(self):
        from memsift.vendor.logfiles import append_line
        original = os.write
        def short_write(fd, data):
            if data == b"record\n":
                return original(fd, data[:3])
            return original(fd, data)
        path = self.home / "activity.log"
        with patch("os.write", side_effect=short_write):
            self.assertFalse(append_line(path, "record"))
        self.assertEqual(path.read_bytes(), b"rec")
        self.assertIn("incomplete log append", self.log())

    def test_recall_validates_arguments_and_supports_explicit_code_query(self):
        from memsift.vendor.recall_http import do_recall
        for scope, top_k, code_query in (("invalid JSON", 5, None), ([], -1, None), (["graph"], 5, "[]"), ({"graph": True}, 5, None)):
            with self.subTest(scope=scope, top_k=top_k, code_query=code_query):
                with self.assertRaises(ValueError):
                    do_recall(self.fake.url, "invented-key", "checkout", "", scope, top_k,
                              code_query=code_query, opener=self.fake.open)
        self.assertEqual(self.fake.requests, [])
        self.fake.push([])
        do_recall(self.fake.url, "invented-key", "checkout", "", '["code"]', "5",
                  dataset_ids="12345678-1234-5678-1234-567812345678,87654321-4321-8765-4321-876543218765",
                  code_query='{"operation":"impact_analysis","targets":["checkout"]}', opener=self.fake.open)
        body = self.fake.requests[0]["body"]
        self.assertEqual(body["dataset_ids"], ["12345678-1234-5678-1234-567812345678"])
        self.assertEqual(body["code_query"], {"operation": "impact_analysis", "targets": ["checkout"]})
        self.assertNotIn("session_id", body)


    def test_remember_invalid_utf8_response_is_in_redacted_excerpt(self):
        self.fake.push(b"response-prefix PASSWORD=invented-secret \xff")
        with self.assertRaises(UnicodeDecodeError):
            self.remember(opener=self.fake.open)
        self.assertIn("response-prefix [redacted:credential]", self.log())
        self.assertIn("cognee status=200", self.log())
        self.assertNotIn("invented-secret", self.log())
