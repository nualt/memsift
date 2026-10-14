"""Offline contracts for scoped Cognee recall and durable packets."""

import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from tests.support import IsolatedTest


NAME = "acme-shop 2026-10-14 15h32 header grid.txt"
A = "12345678-1234-5678-1234-567812345678"
B = "87654321-4321-8765-4321-876543218765"


class CogneeTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        from memsift import breaker, cognee, store
        self.breaker, self.cognee, self.store = breaker, cognee, store

    def log(self):
        return self.store.read_text(self.store.home() / "debug.log")

    def failures(self):
        return self.breaker.state()["services"]["cognee"]["failures"]

    def dataset(self, name="memory", ident=""):
        return {"name": name, "id": ident, "items": []}

    def test_failed_upload_preserves_original_name_and_content(self):
        path = self.cognee.queue_packet(self.repo, NAME, "The header follows the footer grid.")
        with patch("memsift.cognee.remember_http.do_remember", side_effect=TimeoutError("timeout")):
            self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertEqual(path.name, NAME)
        self.assertEqual(path.read_text(), "The header follows the footer grid.")
        self.assertEqual(self.failures(), 1)
        self.assertIn("Traceback", self.log())

    def test_no_search_all_when_no_dataset_selected(self):
        self.assertEqual(self.cognee.recall(self.config, "header", []), [])
        self.assertEqual(self.fake.requests, [])
        self.assertFalse((self.store.home() / "state.json").exists())

    def test_storage_directory_upload_needs_no_live_session(self):
        other = self.home / "other" / "acme-shop"
        other.mkdir(parents=True)
        first = self.cognee.queue_packet(other, NAME, "first")
        second = self.cognee.queue_packet(other, NAME, "second")
        third = self.cognee.queue_packet(other, NAME, "second")
        self.assertEqual(second.name, NAME[:-4] + " (2).txt")
        self.assertEqual(third.name, NAME[:-4] + " (3).txt")
        for _ in range(3):
            self.fake.push({"ok": True})
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(other), self.config), 3)
        self.assertFalse(any(p.exists() for p in (first, second, third)))
        self.assertFalse((self.store.home() / "sessions").exists())
        wire = self.fake.requests[0]["body"].decode()
        self.assertIn('filename="' + NAME + '"', wire)
        self.assertIn('name="datasetName"\r\n\r\nmemsift', wire)
        self.assertIn('name="node_set"\r\n\r\nmemsift', wire)

    def test_slow_body_fails_once_without_a_late_passage(self):
        self.fake.push_chunks([b"["] + [b" "] * 20 + [b"]"], interval=0.05,
                              path="/api/v1/recall")
        began = time.monotonic()
        with self.assertRaises(self.breaker.ServiceError):
            self.cognee.recall(self.config, "header", [self.dataset()], timeout=0.15)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(self.failures(), 1)
        for thread in threading.enumerate():
            if thread.name.startswith("memsift-recall"):
                thread.join(2)
        self.assertEqual(self.failures(), 1)
        self.assertEqual(len(self.fake.requests), 1)
        self.assertIn("Traceback", self.log())

    def test_parallel_round_robin_preserves_selection_order(self):
        started = []
        opener = self.fake.open
        def observed(request, timeout):
            started.append(time.monotonic())
            return opener(request, timeout)
        for ident, prefix in ((A, "a"), (B, "b")):
            self.fake.push([{"source": "graph", "text": "\n---\n".join(prefix + str(i) for i in range(1, 4))}],
                           delay=0.2, path="/api/v1/recall?test=" + ident)
        def routed(request, timeout, **kwargs):
            ident = json.loads(request.data)["dataset_ids"][0]
            request.full_url += "?test=" + ident
            return observed(request, timeout)
        with patch("memsift.transport.open_request", side_effect=routed):
            result = self.cognee.recall(self.config, "header", [self.dataset("A", A), self.dataset("B", B)])
        self.assertEqual([p["text"] for p in result], ["a1", "b1", "a2", "b2", "a3"])
        self.assertEqual([p["dataset"] for p in result], ["A", "B", "A", "B", "A"])
        self.assertEqual([p["id"] for p in result], ["p1", "p2", "p3", "p4", "p5"])
        self.assertLess(abs(started[0] - started[1]), 0.15)
        for req in self.fake.requests:
            self.assertEqual(req["body"]["scope"], ["graph"])
            self.assertEqual(req["body"]["query"], "header")
            self.assertEqual(req["body"]["top_k"], 5)
            self.assertNotIn("session_id", req["body"])
            self.assertNotIn("code_query", req["body"])

    def test_one_slow_constituent_discards_fast_recall(self):
        self.fake.push(["fast"])
        self.fake.push(["late"], delay=0.7)
        began = time.monotonic()
        with self.assertRaises(self.breaker.ServiceError):
            self.cognee.recall(self.config, "header", [self.dataset("A", A), self.dataset("B", B)], timeout=0.15)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual(self.failures(), 1)
        for thread in threading.enumerate():
            if thread.name.startswith("memsift-recall"):
                thread.join(2)
        self.assertEqual(self.failures(), 1)

    def test_queue_rejects_paths_and_redacts_before_creating_files(self):
        for name in ("../escape.txt", "/escape.txt", "bad\\name.txt", "bad\nname.txt", "file.json", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.cognee.queue_packet(self.repo, name, "safe")
        path = self.cognee.queue_packet(self.repo, "acme-shop PASSWORD=invented-title.txt", "API_KEY=invented-content")
        self.assertNotIn("invented-title", path.name)
        self.assertNotIn("invented-content", path.read_text())
        self.fake.push({"ok": True})
        self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config)
        self.assertNotIn(b"invented-", self.fake.requests[0]["body"])

    def test_queued_packet_is_synced_to_disk_before_it_is_published(self):
        with patch("memsift.cognee.os.fsync") as fsync:
            path = self.cognee.queue_packet(self.repo, NAME, "staged before the notes are rewritten")
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(path.read_text(), "staged before the notes are rewritten")

    def test_409_renames_without_overwriting_local_collision(self):
        first = self.cognee.queue_packet(self.repo, NAME, "first")
        second = self.cognee.queue_packet(self.repo, NAME, "second")
        self.fake.push({"detail": "collision"}, status=409)
        self.fake.push({"ok": True})
        self.fake.push({"ok": True})
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 2)
        wires = [req["body"].decode() for req in self.fake.requests]
        self.assertIn('filename="' + NAME[:-4] + ' (3).txt"', wires[1])
        self.assertIn("first", wires[1])
        self.assertIn("second", wires[2])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertIn("cognee status=409", self.log())

    def test_non409_failure_stops_oldest_first_and_retry_keeps_identity(self):
        later = self.cognee.queue_packet(self.repo, NAME.replace("15h32", "16h32"), "newer")
        first = self.cognee.queue_packet(self.repo, NAME, "older")
        self.fake.push({"detail": "down"}, status=500)
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertTrue(first.exists())
        self.assertTrue(later.exists())
        self.assertEqual(len(self.fake.requests), 1)
        self.fake.push({"ok": True})
        self.fake.push({"ok": True})
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 2)
        self.assertIn(('filename="' + NAME + '"').encode(), self.fake.requests[1]["body"])
        self.assertIn(b"older", self.fake.requests[1]["body"])
        self.assertEqual(self.failures(), 0)

    def test_concurrent_senders_upload_a_packet_once(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        self.fake.push({"ok": True}, delay=0.2)
        results = []
        worker = threading.Thread(target=lambda: results.append(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config)))
        worker.start()
        deadline = time.monotonic() + 1
        while not self.fake.requests and time.monotonic() < deadline:
            time.sleep(0.005)
        results.append(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config))
        worker.join(2)
        self.assertEqual(sorted(results), [0, 1])
        self.assertFalse(path.exists())
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(self.failures(), 0)

    def test_descriptions_ignored_and_index_own_prefix_order(self):
        path = self.store.home() / "datasets.md"
        text = "# Datasets\n- custom: our memory: project decisions\n- articles: IGNORED\n- lessons: past fixes\n"
        self.store.write_text(path, text)
        self.assertEqual(self.cognee.descriptions(), {"custom": "our memory: project decisions", "articles": "IGNORED", "lessons": "past fixes"})
        self.config["MEMSIFT_DATASET"] = "custom"
        self.fake.push([{"id": A, "name": "custom"}, {"id": B, "name": "articles"}, {"id": "lesson/id", "name": "lessons"}, {"id": "unknown", "name": "new"}])
        items = [NAME.replace("15h32", "14h32"), NAME, NAME[:-4] + " (2).txt", NAME.replace("acme-shop ", "acme-shop-old "), "other 2026-10-14 20h00 footer.txt"]
        self.fake.push([{"id": str(i), "name": name} for i, name in enumerate(items)])
        self.fake.push([{"id": "item", "name": "footer fix.txt"}])
        self.fake.push([])
        self.assertEqual(self.cognee.rebuild_index(self.repo, self.config), ["new"])
        index = json.loads(self.store.read_text(self.store.repo_dir(self.repo) / "cognee-index.json"))
        self.assertEqual([d["name"] for d in index["datasets"]], ["custom", "lessons", "new"])
        self.assertEqual(index["datasets"][0]["items"], [NAME[:-4] + " (2).txt", NAME, NAME.replace("15h32", "14h32")])
        self.assertEqual(index["datasets"][1]["items"], ["footer fix.txt"])
        self.assertRegex(index["fetched_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIn("/lesson%2Fid/data", self.fake.requests[2]["url"])
        self.assertEqual(path.read_text(), text)
        self.assertTrue(all(req["headers"]["X-Api-Key"] == self.config["COGNEE_API_KEY"] for req in self.fake.requests))

    def test_ignored_selection_never_calls_recall(self):
        self.store.write_text(self.store.home() / "datasets.md", "# Datasets\n- articles: IGNORED\n")
        self.assertEqual(self.cognee.recall(self.config, "header", [self.dataset("articles", A)]), [])
        self.assertEqual(self.fake.requests, [])

    def test_index_prunes_own_oldest_then_external_equal_shares(self):
        with patch.dict(os.environ, {"MEMSIFT_DEBUG": "1"}):
            self.store.write_text(self.store.home() / "datasets.md", "# Datasets\n- A: fixes\n- B: decisions\n")
            self.fake.push([{"id": "own", "name": "memsift"}, {"id": A, "name": "A"}, {"id": B, "name": "B"}])
            own = [NAME + str(i) + "x" * 100 for i in range(10)]
            external = [str(i) + "x" * 100 for i in range(50)]
            self.fake.push([{"name": name} for name in own])
            self.fake.push([{"name": name} for name in external])
            self.fake.push([{"name": name} for name in external])
            self.cognee.rebuild_index(self.repo, self.config)
        text = self.store.read_text(self.store.repo_dir(self.repo) / "cognee-index.json")
        index = json.loads(text)
        self.assertLessEqual(self.store.estimate(text, self.breaker.ratio()), 1000)
        self.assertEqual(index["datasets"][0]["items"], [])
        lengths = [len(d["items"]) for d in index["datasets"][1:]]
        self.assertEqual(lengths[0], lengths[1])
        self.assertGreater(lengths[0], 0)
        for d in index["datasets"][1:]:
            self.assertEqual(d["items"], external[:len(d["items"])])
        self.assertEqual(self.cognee.descriptions(), {"A": "fixes", "B": "decisions"})
        self.assertIn("DECISION cognee-index-cut", self.log())

    def test_index_prunes_oldest_own_items_only_when_external_fits(self):
        self.fake.push([{"id": A, "name": "memsift"}, {"id": B, "name": "lessons"}])
        own = [NAME.replace("15h32", "%02dh00" % i) + "x" * 800 for i in range(10, 15)]
        self.fake.push([{"name": name} for name in own])
        self.fake.push([{"name": "footer.txt"}])
        self.cognee.rebuild_index(self.repo, self.config)
        index = json.loads(self.store.read_text(self.store.repo_dir(self.repo) / "cognee-index.json"))
        kept = index["datasets"][0]["items"]
        self.assertGreater(len(kept), 0)
        self.assertLess(len(kept), len(own))
        self.assertEqual(kept, list(reversed(own))[:len(kept)])
        self.assertEqual(index["datasets"][1]["items"], ["footer.txt"])

    def test_incomplete_index_retains_previous_cache(self):
        path = self.store.repo_dir(self.repo) / "cognee-index.json"
        self.store.write_text(path, '{"old": true}\n')
        self.fake.push([{"id": A, "name": "memsift"}])
        self.fake.push({"detail": "PASSWORD=invented-response"}, status=500)
        with self.assertRaises(self.breaker.ServiceError):
            self.cognee.rebuild_index(self.repo, self.config)
        self.assertEqual(path.read_text(), '{"old": true}\n')
        self.assertEqual(self.failures(), 1)
        self.assertIn("cognee status=500", self.log())
        self.assertNotIn("invented-response", self.log())

    def test_mandatory_metadata_overflow_retains_cache(self):
        path = self.store.repo_dir(self.repo) / "cognee-index.json"
        self.store.write_text(path, '{"old": true}\n')
        self.fake.push([{"id": A, "name": "x" * 4000}])
        self.fake.push([])
        with self.assertRaises(self.breaker.ServiceError):
            self.cognee.rebuild_index(self.repo, self.config)
        self.assertEqual(path.read_text(), '{"old": true}\n')
        self.assertIn("mandatory metadata", self.log())

    def test_api_malformed_objects_raise_and_count(self):
        for body in ({"datasets": []}, [{"id": "", "name": "bad"}], [{"id": A, "name": 7}], [None], [{"id": A, "name": "bad\nname"}], "not JSON"):
            with self.subTest(body=body):
                self.breaker.resume("cognee")
                self.fake.push(body)
                with self.assertRaises(self.breaker.ServiceError):
                    self.cognee.list_datasets(self.config)
                self.assertEqual(self.failures(), 1)
        self.assertIn("Traceback", self.log())
        self.assertIn("cognee status=200", self.log())

    def test_malformed_items_preserve_cache(self):
        for body in ({"items": []}, [None], [{"name": 7}], [{"id": "item"}]):
            with self.subTest(body=body):
                self.breaker.resume("cognee")
                self.fake.push([{"id": A, "name": "memsift"}])
                self.fake.push(body)
                with self.assertRaises(self.breaker.ServiceError):
                    self.cognee.rebuild_index(self.repo, self.config)
                self.assertFalse((self.store.repo_dir(self.repo) / "cognee-index.json").exists())
                self.assertEqual(self.failures(), 1)

    def test_recall_wrapper_empty_and_malformed(self):
        wrapper = "old history\nThe question is: `header` context: `## Relevant passages\none\n---\ntwo\n## Relevant entities\nnoise`\n\n## Active session guidance\nignore"
        self.fake.push([{"source": "graph", "text": wrapper}])
        result = self.cognee.recall(self.config, "header", [self.dataset()])
        self.assertEqual([p["text"] for p in result], ["one", "two"])
        for body in ([], [""], [{"source": "graph", "text": ""}]):
            self.fake.push(body)
            self.assertEqual(self.cognee.recall(self.config, "header", [self.dataset()]), [])
        for body in ({"error": "bad"}, [7], [{"text": 7}], [{"source": "graph"}]):
            with self.subTest(body=body):
                self.breaker.resume("cognee")
                self.fake.push(body)
                with self.assertRaises(self.breaker.ServiceError):
                    self.cognee.recall(self.config, "header", [self.dataset()])
                self.assertEqual(self.failures(), 1)

    def test_recall_404_is_empty_but_other_statuses_keep_classification(self):
        for status, failures, paused in ((404, 0, False), (401, 1, True), (403, 1, True), (429, 0, False), (529, 0, False), (400, 1, False), (500, 1, False)):
            with self.subTest(status=status):
                self.breaker.resume("cognee")
                self.fake.push({"detail": "DatasetNotFoundError" if status == 404 else "PASSWORD=invented-response"}, status=status)
                if status == 404:
                    self.assertEqual(self.cognee.recall(self.config, "header", [self.dataset()]), [])
                else:
                    with self.assertRaises(self.breaker.ServiceError) as caught:
                        self.cognee.recall(self.config, "header", [self.dataset()])
                    self.assertEqual(caught.exception.status, status)
                    self.assertIsNotNone(caught.exception.__cause__)
                self.assertEqual(self.failures(), failures)
                self.assertEqual(self.breaker.paused("cognee"), paused)
        self.assertIn("duration=", self.log())
        self.assertNotIn("invented-response", self.log())

    def test_upload_http_errors_and_malformed_acceptance_preserve_file(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        for status, body, failures, paused in ((401, {}, 1, True), (403, {}, 1, True), (429, {}, 0, False), (529, {}, 0, False), (500, {}, 1, False), (200, {"ok": False}, 1, False), (200, "bad JSON", 1, False)):
            with self.subTest(status=status, body=body):
                self.breaker.resume("cognee")
                self.fake.push(body, status=status)
                self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
                self.assertTrue(path.exists())
                self.assertEqual(self.failures(), failures)
                self.assertEqual(self.breaker.paused("cognee"), paused)

    def test_probe_bypasses_pause_without_mutation(self):
        self.breaker.record("cognee", {"kind": "auth", "reason": "key refused", "status": 401})
        before = self.breaker.state()
        self.fake.push([])
        self.cognee.probe(self.config)
        self.assertEqual(self.breaker.state(), before)
        self.fake.push({"detail": "refused"}, status=403)
        with self.assertRaises(self.breaker.ServiceError):
            self.cognee.probe(self.config)
        self.assertEqual(self.breaker.state(), before)
        with self.assertRaises(self.breaker.PausedError):
            self.cognee.list_datasets(self.config)
        with self.assertRaises(self.breaker.PausedError):
            self.cognee.recall(self.config, "header", [self.dataset()])
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertTrue(path.exists())
        self.assertEqual(len(self.fake.requests), 2)

    def test_hostile_write_routing_cannot_change_configured_dataset(self):
        self.config["MEMSIFT_DATASET"] = "custom"
        self.cognee.queue_packet(self.repo, NAME, "one")
        self.fake.push({"ok": True})
        with patch.dict(os.environ, {"COGNEE_PLUGIN_DATASET": "rogue", "COGNEE_PLUGIN_WRITE_DATASET_ID": B}):
            self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 1)
        wire = self.fake.requests[0]["body"].decode()
        self.assertIn('name="datasetName"\r\n\r\ncustom', wire)
        self.assertIn('name="node_set"\r\n\r\ncustom', wire)
        self.assertNotIn(B, wire)
        self.assertNotIn("rogue", wire)

    def test_failed_queue_write_does_not_publish_partial_packet(self):
        actual = Path.open
        class FailingWrite:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, text):
                self.stream.write(text[:3])
                self.stream.flush()
                raise OSError("injected packet write failure")
        def failing(path, *args, **kwargs):
            stream = actual(path, *args, **kwargs)
            if args and args[0] in ("x", "w") and path.name.startswith("acme-shop "):
                return FailingWrite(stream)
            return stream
        with patch.object(Path, "open", failing), self.assertRaises(OSError):
            self.cognee.queue_packet(self.repo, NAME, "complete content")
        self.assertEqual(list((self.store.repo_dir(self.repo) / "outbox").glob("*.txt")), [])
        self.assertIn("Traceback", self.log())

    def test_collision_failure_is_counted_before_retry_and_pause_keeps_suffix(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        for _ in range(2):
            self.breaker.record("cognee", {"kind": "failure", "reason": "timeout", "status": 0})
        self.fake.push({}, status=409)
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertFalse(path.exists())
        suffixed = path.with_name(NAME[:-4] + " (2).txt")
        self.assertEqual(suffixed.read_text(), "one")
        self.assertEqual(self.failures(), 3)
        self.assertTrue(self.breaker.paused("cognee"))
        self.assertEqual(len(self.fake.requests), 1)

    def test_timeout_through_transport_then_retry_has_same_identity(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        self.fake.push({"ok": True}, delay=0.4)
        with patch.object(self.cognee, "UPLOAD_TIMEOUT", 0.1):
            self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertTrue(path.exists())
        self.fake.push({"ok": True})
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 1)
        for req in self.fake.requests:
            self.assertIn(('filename="' + NAME + '"').encode(), req["body"])
            self.assertIn(b"\r\n\r\none\r\n", req["body"])
        self.assertEqual(self.failures(), 0)

    def test_http_error_body_read_fault_is_logged(self):
        import io
        import urllib.error
        class BrokenBody(io.BytesIO):
            def read1(self, *args):
                raise OSError("injected body read failure")
        exc = urllib.error.HTTPError(self.fake.url + "/api/v1/datasets", 503, "down", {}, BrokenBody())
        with patch("memsift.transport.open_request", side_effect=exc), self.assertRaises(self.breaker.ServiceError) as caught:
            self.cognee.list_datasets(self.config)
        self.assertIs(caught.exception.__cause__, exc)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.failures(), 1)
        self.assertIn("injected body read failure", self.log())
        self.assertIn("cognee status=503", self.log())

    def test_outbox_delete_fault_retains_accepted_packet(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        self.fake.push({"ok": True})
        with patch.object(Path, "unlink", side_effect=OSError("injected delete failure")):
            self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.failures(), 0)
        self.assertIn("injected delete failure", self.log())

    def test_index_atomic_write_fault_preserves_old_cache(self):
        path = self.store.repo_dir(self.repo) / "cognee-index.json"
        self.store.write_text(path, '{"old": true}\n')
        self.fake.push([])
        actual = os.replace
        def failed_replace(source, destination):
            if destination == path:
                raise OSError("injected index replace failure")
            return actual(source, destination)
        with patch("memsift.store.os.replace", side_effect=failed_replace):
            with self.assertRaises(self.breaker.ServiceError):
                self.cognee.rebuild_index(self.repo, self.config)
        self.assertEqual(path.read_text(), '{"old": true}\n')
        self.assertIn("injected index replace failure", self.log())

    def test_empty_outbox_does_not_create_storage(self):
        directory = self.store.repo_dir(self.repo)
        self.assertEqual(self.cognee.send_outbox(directory, self.config), 0)
        self.assertFalse(directory.exists())
        self.assertEqual(self.fake.requests, [])

    def test_repeated_remote_collisions_advance_suffix_across_resume(self):
        path = self.cognee.queue_packet(self.repo, NAME, "one")
        for _ in range(3):
            self.fake.push({}, status=409)
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
        fourth = path.with_name(NAME[:-4] + " (4).txt")
        self.assertTrue(fourth.exists())
        self.assertTrue(self.breaker.paused("cognee"))
        self.breaker.resume("cognee")
        self.fake.push({}, status=409)
        self.fake.push({"ok": True})
        self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 1)
        self.assertIn(('filename="' + NAME[:-4] + ' (5).txt"').encode(), self.fake.requests[-1]["body"])

    def test_stale_concurrent_drain_cannot_skip_renamed_failed_oldest(self):
        from contextlib import contextmanager
        first = self.cognee.queue_packet(self.repo, NAME, "older")
        later = self.cognee.queue_packet(self.repo, NAME.replace("15h32", "16h32"), "newer")
        self.fake.push({}, status=409)
        self.fake.push({}, status=500)
        self.fake.push({"ok": True})
        reached, release = threading.Event(), threading.Event()
        actual = self.cognee.file_lock
        @contextmanager
        def paused_lock(path, *args, **kwargs):
            if threading.current_thread().name == "stale-drain" and path.name == first.name + ".send.lock":
                reached.set()
                if not release.wait(2):
                    raise AssertionError("drain synchronization timed out")
            with actual(path, *args, **kwargs) as acquired:
                yield acquired
        results = []
        with patch.object(self.cognee, "file_lock", paused_lock):
            worker = threading.Thread(name="stale-drain", target=lambda: results.append(
                self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config)))
            worker.start()
            try:
                self.assertTrue(reached.wait(1))
                self.assertEqual(self.cognee.send_outbox(self.store.repo_dir(self.repo), self.config), 0)
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(results, [0])
        self.assertTrue(first.with_name(NAME[:-4] + " (2).txt").exists())
        self.assertTrue(later.exists())
        self.assertEqual(len(self.fake.requests), 2)
        self.assertEqual(self.failures(), 2)
