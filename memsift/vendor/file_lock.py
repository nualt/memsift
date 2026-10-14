# Originally from cognee-integrations (https://github.com/topoteretes/cognee-integrations),
# Copyright 2026 Topoteretes UG, Apache-2.0. Modified by memsift.
"""Small cross-process lock with bounded acquisition on POSIX and Windows."""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def file_lock(path: Path, timeout: float = 0.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")

            def lock():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

            def unlock():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(fd, fcntl.LOCK_UN)

        deadline = time.monotonic() + max(0.0, timeout)
        waited_since = None
        while True:
            try:
                lock()
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                    from .logfiles import error

                    error("file-lock", exc)
                    raise
                # Busy: another memsift process holds it. Normal; wait and retry.
                if waited_since is None:
                    waited_since = time.monotonic()
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.005)
        if waited_since is not None:
            from .logfiles import decision

            decision("lock", {"file": os.path.basename(str(path)), "busy_ms": round((time.monotonic() - waited_since) * 1000),
                              "taken": acquired})
        yield acquired
    finally:
        if acquired:
            unlock()
        os.close(fd)
