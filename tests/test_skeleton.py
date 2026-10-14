"""Package and offline harness contract tests."""

import importlib
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request

from tests.support import IsolatedTest, child_env


class SkeletonTests(IsolatedTest):
    def test_every_runtime_module_imports_without_io(self):
        for name in (
            "run",
            "host",
            "core",
            "store",
            "jev",
            "compactor",
            "cognee",
            "breaker",
            "doctor",
        ):
            with self.subTest(name=name):
                importlib.import_module("memsift." + name)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse((self.home / ".memsift").exists())

    def test_fake_routes_and_records_requests(self):
        self.fake.push({"answers": {"keep": True}}, path="/decision?turn=1")
        request = urllib.request.Request(
            "https://example.invalid/decision?turn=1",
            data=json.dumps({"prompt": "acme-shop"}).encode(),
            headers={
                "Authorization": "Bearer invented-key",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        with self.fake.open(request, timeout=1.25) as response:
            self.assertEqual(json.load(response), {"answers": {"keep": True}})

        self.assertEqual(len(self.fake.requests), 1)
        recorded = self.fake.requests[0]
        self.assertEqual(recorded["method"], "POST")
        self.assertEqual(recorded["url"], request.full_url)
        self.assertEqual(recorded["body"], {"prompt": "acme-shop"})
        self.assertEqual(recorded["timeout"], 1.25)
        self.assertEqual(recorded["headers"]["Authorization"], "Bearer invented-key")

    def test_fake_timeout_is_bounded(self):
        self.fake.push({"answers": {}}, delay=3.0)
        request = urllib.request.Request("https://example.invalid/decision")
        with self.assertRaises((TimeoutError, urllib.error.URLError)):
            self.fake.open(request, timeout=2.0)
        self.assertEqual(len(self.fake.requests), 1)

    def test_slow_flushed_body_is_real(self):
        self.fake.push_chunks([b'{"value":', b" true}"], interval=0.05)
        with self.fake.open(
            urllib.request.Request("https://example.invalid/stream"), timeout=1.0
        ) as response:
            self.assertEqual(json.load(response), {"value": True})
        self.assertEqual(len(self.fake.requests), 1)

    def test_guard_refuses_non_loopback_before_lookup(self):
        with self.assertRaises(PermissionError):
            socket.getaddrinfo("example.invalid", 443)
        with self.assertRaises(PermissionError):
            socket.create_connection(("192.0.2.1", 443), timeout=0.01)

        self.fake.push({"ok": True}, path="/loopback")
        with urllib.request.urlopen(self.fake.url + "/loopback", timeout=1.0) as response:
            self.assertEqual(json.load(response), {"ok": True})

    def test_child_and_grandchild_are_guarded_and_routed(self):
        self.fake.push({"process": "child"}, path="/probe")
        self.fake.push({"process": "grandchild"}, path="/probe")
        network_check = """
import socket
try:
    socket.getaddrinfo("example.invalid", 443)
except PermissionError:
    pass
else:
    raise AssertionError("example.invalid")
try:
    socket.create_connection(("192.0.2.1", 443), timeout=.01)
except PermissionError:
    pass
else:
    raise AssertionError("192.0.2.1")
"""
        grandchild = network_check + """
import json, socket, urllib.request
with urllib.request.urlopen(\"https://service.invalid/probe\", timeout=1) as response:
    assert json.load(response) == {\"process\": \"grandchild\"}
"""
        child = network_check + """
import json, socket, subprocess, sys, urllib.request
with urllib.request.urlopen(\"https://service.invalid/probe\", timeout=1) as response:
    assert json.load(response) == {\"process\": \"child\"}
subprocess.run([sys.executable, \"-c\", %r], check=True)
""" % grandchild

        subprocess.run(
            [sys.executable, "-c", child],
            cwd=self.repo,
            env=child_env(self.fake.url, self.home),
            check=True,
            timeout=5,
        )
        self.assertEqual(len(self.fake.requests), 2)
        self.assertTrue(
            all(request["url"] == "https://service.invalid/probe" for request in self.fake.requests)
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
