"""Scripted loopback HTTP services for offline tests."""

from collections import defaultdict, deque
import http.server
import json
import socket
import threading
import time
import urllib.parse
import urllib.request


_REAL_URLOPEN = urllib.request.urlopen
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_GUARD_INSTALLED = False


def _response_script(body, status, header_delay, interval, path):
    if isinstance(body, bytes):
        chunks = [body]
    elif isinstance(body, str):
        chunks = [body.encode("utf-8")]
    else:
        chunks = [json.dumps(body).encode("utf-8")]
    return {
        "chunks": chunks,
        "status": status,
        "header_delay": header_delay,
        "interval": interval,
        "path": path,
    }


def _routed_request(request, server_url, data=None):
    if not isinstance(request, urllib.request.Request):
        request = urllib.request.Request(request, data=data)
    elif data is not None:
        request = urllib.request.Request(
            request.full_url,
            data=data,
            headers=dict(request.header_items()),
            method=request.get_method(),
        )
    parsed = urllib.parse.urlsplit(request.full_url)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    headers = dict(request.header_items())
    headers["X-Memsift-Test-Url"] = request.full_url
    return urllib.request.Request(
        server_url.rstrip("/") + path,
        data=request.data,
        headers=headers,
        method=request.get_method(),
    )


def _route_open(server_url, request, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs):
    routed = _routed_request(request, server_url, data)
    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
        routed.add_header("X-Memsift-Test-Timeout", repr(timeout))
    return _REAL_URLOPEN(routed, timeout=timeout, **kwargs)


def _require_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host != "127.0.0.1":
        raise PermissionError("tests refuse non-loopback network access")


def install_http_guard(server_url):
    """Route urllib through the fake server and refuse every non-loopback socket."""
    global _GUARD_INSTALLED

    def guarded_getaddrinfo(host, *args, **kwargs):
        _require_loopback(host)
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        _require_loopback(address[0])
        return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)

    def guarded_connect(sock, address):
        _require_loopback(address[0] if isinstance(address, tuple) else address)
        return _ORIGINAL_CONNECT(sock, address)

    def guarded_connect_ex(sock, address):
        _require_loopback(address[0] if isinstance(address, tuple) else address)
        return _ORIGINAL_CONNECT_EX(sock, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.create_connection = guarded_create_connection
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    urllib.request.urlopen = lambda request, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, **kwargs: _route_open(
        server_url, request, data, timeout, **kwargs
    )
    _GUARD_INSTALLED = True


def remove_http_guard():
    """Restore process-global networking after an isolated test."""
    global _GUARD_INSTALLED
    if not _GUARD_INSTALLED:
        return
    socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    socket.create_connection = _ORIGINAL_CREATE_CONNECTION
    socket.socket.connect = _ORIGINAL_CONNECT
    socket.socket.connect_ex = _ORIGINAL_CONNECT_EX
    urllib.request.urlopen = _REAL_URLOPEN
    _GUARD_INSTALLED = False


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def log_message(self, format, *args):
        pass

    def _handle(self):
        owner = self.server.owner
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        headers = dict(self.headers.items())
        original_url = headers.pop("X-Memsift-Test-Url", owner.url + self.path)
        timeout_text = headers.pop("X-Memsift-Test-Timeout", None)
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw_body
        record = {
            "method": self.command,
            "url": original_url,
            "headers": headers,
            "body": body,
            "timeout": float(timeout_text) if timeout_text is not None else None,
        }
        with owner._lock:
            owner.requests.append(record)
            queue = owner._paths.get(self.path)
            if queue:
                script = queue.popleft()
            elif owner._scripts:
                script = owner._scripts.popleft()
            else:
                script = None
                owner._errors.append("unscripted request: " + original_url)
        if script is None:
            self.send_error(500, "unscripted request")
            return
        time.sleep(script["header_delay"])
        try:
            self.send_response(script["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(sum(map(len, script["chunks"]))))
            self.end_headers()
            for chunk in script["chunks"]:
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(script["interval"])
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeServices:
    """FIFO scripted HTTP server bound only to 127.0.0.1."""

    def __init__(self):
        self._lock = threading.Lock()
        self._scripts = deque()
        self._paths = defaultdict(deque)
        self._open_errors = deque()
        self._errors = []
        self.requests = []
        self._server = None
        self._thread = None

    @property
    def url(self):
        port = self._server.server_address[1]
        return "http://127.0.0.1:" + str(port)

    def start(self):
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.owner = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._server = None
        if self._errors:
            raise AssertionError("; ".join(self._errors))

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()

    def push(self, body, status=200, delay=0.0, path=None):
        self._enqueue(_response_script(body, status, delay, 0.0, path))

    def push_chunks(self, chunks, interval, status=200, header_delay=0.0, path=None):
        self._enqueue(
            {
                "chunks": chunks,
                "status": status,
                "header_delay": header_delay,
                "interval": interval,
                "path": path,
            }
        )

    def push_error(self, exc):
        with self._lock:
            self._open_errors.append(exc)

    def open(self, request, timeout):
        with self._lock:
            if self._open_errors:
                raise self._open_errors.popleft()
        return _route_open(self.url, request, timeout=timeout)

    def _enqueue(self, script):
        with self._lock:
            if script["path"] is None:
                self._scripts.append(script)
            else:
                self._paths[script["path"]].append(script)
