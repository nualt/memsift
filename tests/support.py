"""Isolation and transport helpers shared by the test suite."""

import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.fake_services import FakeServices, install_http_guard, remove_http_guard


_CONFIG = {
    "COGNEE_API_KEY": "invented-cognee-key",
    "OPENROUTER_API_KEY": "invented-openrouter-key",
    "MEMSIFT_JEV_MODEL": "invented-jev-model",
    "MEMSIFT_COMPACTOR_API_KEY": "invented-compactor-key",
    "MEMSIFT_COMPACTOR_MODEL": "invented-compactor-model",
    "MEMSIFT_DATASET": "memsift",
    "MEMSIFT_DEBUG": "0",
}


def _scrubbed_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COGNEE_", "MEMSIFT_")) and key != "OPENROUTER_API_KEY"
    }


def child_env(server_url, home):
    """Return an isolated environment that guards child and descendant sockets."""
    home = Path(home)
    bootstrap = home / ".memsift-test-bootstrap"
    bootstrap.mkdir(parents=True, exist_ok=True)
    (bootstrap / "sitecustomize.py").write_text(
        "from tests.support import install_http_guard\n"
        "install_http_guard(" + repr(server_url) + ")\n",
        encoding="utf-8",
    )
    env = _scrubbed_environment()
    env.update(_CONFIG)
    env.update(
        {
            "HOME": str(home),
            "COGNEE_BASE_URL": server_url,
            "MEMSIFT_COMPACTOR_URL": server_url,
            "PYTHONPATH": os.pathsep.join((str(bootstrap), str(Path(__file__).parents[1]))),
        }
    )
    return env


def _remove(path):
    # Detached hook workers may still write a log line or their state as a test ends;
    # they finish within moments, so removal is retried instead of failing the test.
    for _ in range(20):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.1)
    shutil.rmtree(path, ignore_errors=True)


class IsolatedTest(unittest.TestCase):
    """Test case with a temporary HOME, acme-shop repo and loopback services."""

    def setUp(self):
        temporary = tempfile.mkdtemp()
        self.addCleanup(_remove, temporary)
        self.home = Path(temporary) / "home"
        self.repo = self.home / "acme-shop"
        self.repo.mkdir(parents=True)

        environment = _scrubbed_environment()
        environment.update(_CONFIG)
        environment["HOME"] = str(self.home)
        self._environment = patch.dict(os.environ, environment, clear=True)
        self._environment.start()
        self.addCleanup(self._environment.stop)

        self.fake = FakeServices().start()
        self.addCleanup(self.fake.stop)
        self.config = dict(_CONFIG)
        self.config.update(
            {
                "COGNEE_BASE_URL": self.fake.url,
                "MEMSIFT_COMPACTOR_URL": self.fake.url,
            }
        )
        os.environ.update(self.config)

        install_http_guard(self.fake.url)
        self.addCleanup(remove_http_guard)
