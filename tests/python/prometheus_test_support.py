"""Loopback HTTP support for Prometheus collection tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, _LoopbackServer)
        server.requests.append((self.command, self.path))
        if server.on_request is not None:
            server.on_request(self.command, self.path)
        status, chunks, declared_length, disconnect = server.response_for(self.path)
        self.send_response(status)
        if declared_length is not None:
            self.send_header("Content-Length", str(declared_length))
        self.end_headers()
        try:
            for delay, chunk in chunks:
                time.sleep(delay)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        if disconnect:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def log_message(self, format: str, *args: object) -> None:
        pass


class _LoopbackServer(ThreadingHTTPServer):
    requests: list[tuple[str, str]]
    response_for: Callable[
        [str], tuple[int, list[tuple[float, bytes]], int | None, bool]
    ]
    on_request: Callable[[str, str], None] | None


@contextmanager
def loopback_http_server(
    response_for: Callable[
        [str], tuple[int, list[tuple[float, bytes]], int | None, bool]
    ],
    *,
    on_request: Callable[[str, str], None] | None = None,
) -> Iterator[tuple[_LoopbackServer, str]]:
    server = _LoopbackServer(("127.0.0.1", 0), _LoopbackHandler)
    server.requests = []
    server.response_for = response_for
    server.on_request = on_request
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
