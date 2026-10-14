"""Read-only setup checks with isolated service probes and redacted diagnostics."""

from unittest.mock import patch

from tests.support import IsolatedTest
from tests.test_core import turn
from tests.test_decisions import noul
from tests.test_compaction import answer, envelope


class DoctorTests(IsolatedTest):
    def healthy(self):
        self.fake.push([{"id": "m1", "name": "memsift"}, {"id": "c1", "name": "clients"}],
                       path="/api/v1/datasets")
        self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}},
                       path="/api/alpha/decisions")
        self.fake.push(envelope(answer()), path="/chat/completions")

    def check(self, config=None):
        from memsift import doctor
        self.assertTrue(callable(getattr(doctor, "check", None)), "doctor check is missing")
        return doctor.check(self.repo, self.config if config is None else config)

    def test_healthy_and_paused_probes_preserve_state_and_project(self):
        from memsift import breaker, cognee, store
        (self.repo / "jev.md").write_text("Product grid", encoding="utf-8")
        store.write_text(store.home() / "datasets.md", "- memsift: Memory\n- clients: IGNORED\n")
        store.queue_turn(self.repo, turn(), 3)
        packet = cognee.queue_packet(self.repo, "acme-shop 2026-10-14 08h00 grid.txt", "Grid")
        for paused in (False, True):
            if paused:
                for service in ("jev", "cognee", "compactor"):
                    breaker.record(service, {"kind": "auth", "reason": "refused", "status": 401})
            before = breaker.state()
            state_bytes = store.read_text(store.home() / "state.json")
            self.healthy()
            result = self.check()
            self.assertIn("✓ Cognee reachable, key accepted, dataset memsift", result)
            self.assertIn("✓ Jev reachable", result)
            self.assertIn("✓ compactor reachable", result)
            self.assertIn("1 turns pending · 1 packets waiting", result)
            self.assertIn("✓ this repo: jev.md found", result)
            self.assertEqual(breaker.state(), before)
            self.assertEqual(store.read_text(store.home() / "state.json"), state_bytes)
            self.assertTrue(packet.is_file())
            self.assertEqual(store.pending_turns(self.repo), [turn()])
            self.assertEqual((self.repo / "jev.md").read_text(), "Product grid")
            self.assertEqual(len(self.fake.requests), 3 if not paused else 6)
            self.assertTrue(all(line.startswith(("✓ ", "✗ ")) for line in result.splitlines()))

    def test_failures_are_safe_and_logged_without_changing_counters(self):
        from memsift import breaker, store
        before = breaker.state()
        for path in ("/api/v1/datasets", "/api/alpha/decisions", "/chat/completions"):
            self.fake.push({"error": "PASSWORD=invented-private-value"}, status=401, path=path)
        result = self.check()
        self.assertEqual(result.count("HTTP 401"), 3)
        self.assertIn("✗ this repo: create jev.md", result)
        self.assertNotIn("invented-private-value", result)
        self.assertEqual(breaker.state(), before)
        log = store.read_text(store.home() / "debug.log")
        self.assertIn("Traceback", log)
        self.assertNotIn("invented-private-value", log)

    def test_missing_config_skips_only_dependent_services(self):
        config = dict(self.config, COGNEE_BASE_URL="", COGNEE_API_KEY="", OPENROUTER_API_KEY="")
        self.fake.push(envelope(answer()), path="/chat/completions")
        result = self.check(config)
        self.assertIn("COGNEE_BASE_URL", result)
        self.assertIn("OPENROUTER_API_KEY", result)
        self.assertIn("✗ Cognee: skipped", result)
        self.assertIn("✗ Jev: skipped", result)
        self.assertIn("✓ compactor reachable", result)
        self.assertEqual(len(self.fake.requests), 1)
        for value in self.config.values():
            if "key" in value:
                self.assertNotIn(value, result)

    def test_missing_dataset_descriptions_card_and_python_checks(self):
        from memsift import store
        self.fake.push([{"id": "c1", "name": "clients"}], path="/api/v1/datasets")
        self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}},
                       path="/api/alpha/decisions")
        self.fake.push(envelope(answer()), path="/chat/completions")
        with patch("sys.version_info", (3, 8, 0)):
            result = self.check()
        self.assertIn("✗ Python 3.8", result)
        self.assertIn("create dataset memsift", result)
        self.assertIn('✗ datasets.md: "clients" has no description', result)
        self.assertFalse((self.repo / "jev.md").exists())
        self.assertFalse((store.home() / "state.json").exists())
        self.assertEqual(list(store.home().glob("doctor-*")), [])

    def test_local_check_errors_are_logged_and_other_checks_continue(self):
        from memsift import store
        for target, check in (("memsift.doctor.tempfile.TemporaryFile", "~/.memsift"),
                              ("memsift.breaker.state", "memory queues"),
                              ("memsift.cognee.descriptions", "datasets.md"),
                              ("pathlib.Path.is_file", "this repo")):
            with self.subTest(target=target):
                self.healthy()
                with patch(target, side_effect=PermissionError("PASSWORD=invented-private-value")):
                    result = self.check()
                self.assertIn("✗ " + check + ": PermissionError", result)
                self.assertNotIn("invented-private-value", result)
                self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_probe_non_http_error_never_displays_key_or_exception_text(self):
        from memsift import store
        self.fake.push([], path="/api/v1/datasets")
        self.fake.push(envelope(answer()), path="/chat/completions")
        with patch("memsift.jev.probe", side_effect=TimeoutError("PASSWORD=invented-private-value")):
            result = self.check()
        self.assertIn("✗ Jev: TimeoutError", result)
        self.assertIn("✓ compactor", result)
        self.assertNotIn("invented-private-value", result)
        self.assertIn("Traceback", store.read_text(store.home() / "debug.log"))

    def test_compactor_missing_key_skips_probe_and_recommends_configuration(self):
        self.fake.push([], path="/api/v1/datasets")
        self.fake.push({"answers": {"available": noul(1)}, "usage": {"input_tokens": 500}},
                       path="/api/alpha/decisions")
        result = self.check(dict(self.config, MEMSIFT_COMPACTOR_API_KEY=""))
        self.assertIn("✗ compactor: skipped, set MEMSIFT_COMPACTOR_API_KEY", result)
        self.assertEqual(len(self.fake.requests), 2)

