"""Machine-wide service failure and token calibration contracts."""

from datetime import datetime, timezone
import io
import multiprocessing
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from tests.support import IsolatedTest, child_env


class FailureTests(IsolatedTest):
    def test_failure_policy(self):
        from memsift import breaker
        for service in ("jev", "compactor", "cognee"):
            with self.subTest(service=service):
                breaker.resume(service)
                for count in (1, 2, 3):
                    breaker.record(service, {"kind": "failure", "reason": "timeout", "status": 0})
                    current = breaker.state()["services"][service]
                    self.assertEqual(current["failures"], count)
                    self.assertEqual(current["paused"], count == 3)

    def test_overload_does_not_count_or_reset_existing_failure(self):
        from memsift import breaker
        breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        for status in (429, 529):
            breaker.record("jev", {"kind": "limited", "reason": "busy", "status": status})
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_missing_state_is_fresh_and_reads_create_nothing(self):
        from memsift import breaker
        expected = {"token_ratio": 3.0, "services": {
            "jev": {"failures": 0, "paused": False, "reason": ""},
            "compactor": {"failures": 0, "paused": False, "reason": ""},
            "cognee": {"failures": 0, "paused": False, "reason": ""}}}
        self.assertEqual(breaker.state(), expected)
        breaker.state()["services"]["jev"]["failures"] = 9
        self.assertEqual(breaker.state(), expected)
        self.assertEqual(breaker.ratio(), 3.0)
        self.assertFalse(breaker.paused("jev"))
        breaker.require_active("jev")
        self.assertFalse((self.home / ".memsift").exists())
        other = self.home / "other-home"
        breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        with patch.dict(os.environ, {"HOME": str(other)}):
            self.assertEqual(breaker.state(), expected)
            self.assertFalse(other.exists())

    def test_http_classification_including_all_other_4xx(self):
        from memsift import breaker
        for status in (0, 400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503, 529):
            expected = "auth" if status in (401, 403) else "limited" if status in (429, 529) else "failure"
            errors = [breaker.ServiceError("cognee", "refused", status)]
            if status:
                http_error = HTTPError("https://service.invalid/recall", status, "refused", {}, io.BytesIO())
                self.addCleanup(http_error.close)
                errors.append(http_error)
            for exc in errors:
                with self.subTest(status=status, error=type(exc).__name__):
                    self.assertEqual(breaker.classify(exc), {
                        "kind": expected, "reason": "refused", "status": status})
        self.assertEqual(breaker.classify(TimeoutError("timed out"))["kind"], "failure")
        self.assertEqual(breaker.classify(URLError(TimeoutError("timed out")))["status"], 0)
        self.assertEqual(breaker.classify(ValueError("invalid response"))["reason"], "invalid response")
        self.assertFalse((self.home / ".memsift").exists())

    def test_auth_pauses_immediately_and_explicit_resume_clears_pause(self):
        from memsift import breaker
        for service in ("jev", "compactor", "cognee"):
            for status in (401, 403):
                with self.subTest(service=service, status=status):
                    breaker.resume(service)
                    breaker.record(service, breaker.classify(breaker.ServiceError(service, "key refused", status)))
                    current = breaker.state()["services"][service]
                    self.assertTrue(current["paused"])
                    self.assertEqual(current["failures"], 1)
                    self.assertEqual(current["reason"], "key refused")
                    since = datetime.strptime(current["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    self.assertLess(abs((datetime.now(timezone.utc) - since).total_seconds()), 5)
                    with self.assertRaises(breaker.PausedError) as caught:
                        breaker.require_active(service)
                    self.assertEqual(caught.exception.service, service)
                    breaker.resume(service)
                    self.assertEqual(breaker.state()["services"][service], {
                        "failures": 0, "paused": False, "reason": ""})
        self.assertEqual(self.fake.requests, [])

    def test_success_resets_failures_without_automatically_resuming(self):
        from memsift import breaker
        for _ in range(2):
            breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        breaker.record("jev", {"kind": "success", "reason": "", "status": 200})
        self.assertEqual(breaker.state()["services"]["jev"], {"failures": 0, "paused": False, "reason": ""})
        for reason in ("first", "second", "pause trigger"):
            breaker.record("jev", {"kind": "failure", "reason": reason, "status": 500})
        paused = breaker.state()["services"]["jev"]
        breaker.record("jev", {"kind": "failure", "reason": "later error", "status": 500})
        breaker.record("jev", {"kind": "auth", "reason": "later auth", "status": 401})
        breaker.record("jev", {"kind": "success", "reason": "", "status": 200})
        current = breaker.state()["services"]["jev"]
        self.assertEqual(current, dict(paused, failures=0))
        breaker.record("jev", {"kind": "limited", "reason": "busy", "status": 529})
        self.assertEqual(breaker.state()["services"]["jev"], current)

    def test_service_error_redacts_diagnostics_and_keeps_original_cause(self):
        from memsift import breaker
        from memsift.vendor.logfiles import error
        original = ValueError("invented original cause")
        with self.assertRaises(breaker.ServiceError) as caught:
            try:
                raise original
            except ValueError as exc:
                error("test-service", exc)
                raise breaker.ServiceError("jev", "PASSWORD=invented-password", 500,
                                           "API_KEY=invented-response", 1.25) from exc
        exc = caught.exception
        self.assertIs(exc.__cause__, original)
        self.assertEqual((exc.service, exc.status, exc.duration), ("jev", 500, 1.25))
        self.assertNotIn("invented-password", str(exc))
        self.assertNotIn("invented-response", exc.response)
        breaker.record("jev", breaker.classify(exc))
        self.assertNotIn("invented-password", (self.home / ".memsift" / "state.json").read_text())
        self.assertIn("Traceback", self.log())

    def test_ratio_averages_usage_and_preserves_other_state(self):
        from memsift import breaker
        breaker.record("cognee", {"kind": "auth", "reason": "key refused", "status": 403})
        services = breaker.state()["services"]
        self.assertEqual(breaker.update_ratio(500, 100), 4.0)
        self.assertEqual(breaker.update_ratio(200, 100), 3.0)
        self.assertEqual(breaker.ratio(), 3.0)
        breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        breaker.resume("jev")
        self.assertEqual(breaker.state()["services"], services)
        self.assertEqual(breaker.ratio(), 3.0)
        with patch.dict(os.environ, {"MEMSIFT_DEBUG": "1"}):
            self.assertEqual(breaker.update_ratio(400, 100), 3.5)
        self.assertIn("DECISION", self.log())

    def test_invalid_usage_logs_tracebacks_without_overwriting_ratio(self):
        from memsift import breaker
        breaker.update_ratio(500, 100)
        before = breaker.state()
        for characters, tokens in ((0, 1), (-1, 1), (1, 0), (1, -1),
                                    (float("nan"), 1), (1, float("nan")),
                                    (float("inf"), 1), (1, float("inf")),
                                    ("invalid", 1), (1, None), (10 ** 400, 1),
                                    (1e308, 1e-308), (1e-308, 1e308)):
            with self.subTest(characters=characters, tokens=tokens):
                with self.assertRaises(ValueError):
                    breaker.update_ratio(characters, tokens)
                self.assertEqual(breaker.state(), before)
        self.assertIn("Traceback", self.log())
        self.assertIn("invalid Jev token usage", self.log())

    def test_corrupt_json_logs_and_recovers_defaults(self):
        from memsift import breaker
        directory = self.home / ".memsift"
        directory.mkdir()
        (directory / "state.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(breaker.ratio(), 3.0)
        self.assertFalse(breaker.paused("jev"))
        self.assertIn("Traceback", self.log())
        self.assertIn("JSONDecodeError", self.log())
        breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        self.assertEqual(breaker.state()["services"]["jev"]["failures"], 1)

    def test_lock_timeout_logs_and_preserves_state(self):
        from memsift import breaker
        from memsift.vendor.file_lock import file_lock
        breaker.update_ratio(500, 100)
        before = breaker.state()
        path = self.home / ".memsift" / "state.json.lock"
        with file_lock(path) as acquired:
            self.assertTrue(acquired)
            with patch("memsift.store.LOCK_WAIT", 0.01):
                with self.assertRaises(TimeoutError):
                    breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
                with self.assertRaises(TimeoutError):
                    breaker.resume("jev")
                with self.assertRaises(TimeoutError):
                    breaker.update_ratio(400, 100)
        self.assertEqual(breaker.state(), before)
        self.assertIn("Traceback", self.log())
        self.assertIn("Storage file lock unavailable", self.log())

    def test_parallel_ratio_and_service_updates_preserve_each_other(self):
        from memsift import breaker
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        workers = []
        for operation in ("ratio", "jev", "cognee"):
            ready, done = context.Event(), context.Event()
            workers.append(self.start_process(update_worker, operation, ready, start, done, 10))
            self.assertTrue(ready.wait(10))
        start.set()
        for process in workers:
            self.join_process(process)
        current = breaker.state()
        self.assertEqual(current["token_ratio"], 4.998046875)
        for service in ("jev", "cognee"):
            self.assertEqual(current["services"][service]["failures"], 10)
            self.assertTrue(current["services"][service]["paused"])
        self.assertEqual(current["services"]["compactor"], {"failures": 0, "paused": False, "reason": ""})

    def test_process_completion_order_controls_consecutive_failures(self):
        from memsift import breaker
        context = multiprocessing.get_context("spawn")
        for order, expected in ((("success", "jev"), 1), (("jev", "success"), 0)):
            with self.subTest(order=order):
                breaker.resume("jev")
                breaker.update_ratio(300, 100)
                breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
                workers = []
                for operation in order:
                    ready, start, done = context.Event(), context.Event(), context.Event()
                    workers.append((self.start_process(update_worker, operation, ready, start, done, 1), start, done))
                    self.assertTrue(ready.wait(10))
                for process, start, done in workers:
                    start.set()
                    self.assertTrue(done.wait(10))
                    self.join_process(process)
                self.assertEqual(breaker.state()["services"]["jev"]["failures"], expected)
                self.assertFalse(breaker.paused("jev"))
                self.assertEqual(breaker.ratio(), 3.0)

    def test_third_failure_is_visible_to_another_process(self):
        from memsift import breaker
        context = multiprocessing.get_context("spawn")
        for _ in range(2):
            breaker.record("jev", {"kind": "failure", "reason": "timeout", "status": 0})
        ready, start, done = context.Event(), context.Event(), context.Event()
        process = self.start_process(update_worker, "jev", ready, start, done, 1)
        self.assertTrue(ready.wait(10))
        start.set()
        self.join_process(process)
        self.assertTrue(breaker.paused("jev"))
        result = subprocess.run([sys.executable, "-c", '''
import unittest
from memsift import breaker
assert breaker.state()["services"]["jev"]["failures"] == 3
with unittest.TestCase().assertRaises(breaker.PausedError) as caught:
    breaker.require_active("jev")
assert caught.exception.service == "jev"
'''], cwd=self.repo, env=child_env(self.fake.url, self.home), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fake.requests, [])

    def log(self):
        path = self.home / ".memsift" / "debug.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def start_process(self, target, *args):
        process = multiprocessing.get_context("spawn").Process(target=target, args=args)
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
        self.assertFalse(process.is_alive(), "breaker child did not finish")
        self.assertEqual(process.exitcode, 0)


def update_worker(operation, ready, start, done, count):
    from memsift import breaker
    ready.set()
    assert start.wait(10)
    for _ in range(count):
        if operation == "ratio":
            breaker.update_ratio(500, 100)
        else:
            service = "jev" if operation == "success" else operation
            kind = "success" if operation == "success" else "failure"
            breaker.record(service, {"kind": kind, "reason": "timeout" if kind == "failure" else "", "status": 0})
    done.set()
