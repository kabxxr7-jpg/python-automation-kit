import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import pytest


@dataclass
class Req:
    index: int
    path: str
    query: dict[str, str]
    headers: dict[str, str]


Response = tuple[int, dict[str, str], Any]  # status, headers, JSON body (or str/bytes for raw)


class ScriptedServer:
    """A real HTTP server on 127.0.0.1 whose answers come from a function the test supplies."""

    def __init__(self, responder: Callable[[Req], Response]) -> None:
        self.responder = responder
        self.requests: list[Req] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
                req = Req(len(outer.requests), parts.path, query, {k.lower(): v for k, v in self.headers.items()})
                outer.requests.append(req)
                status, headers, body = outer.responder(req)
                payload = body if isinstance(body, (bytes, bytearray)) else (
                    body.encode() if isinstance(body, str) else json.dumps(body).encode()
                )
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/items"

    def start(self) -> "ScriptedServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def make_server():
    servers: list[ScriptedServer] = []

    def factory(responder: Callable[[Req], Response]) -> ScriptedServer:
        server = ScriptedServer(responder).start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()


@dataclass
class FakeRuntime:
    """A clock that only moves when sleep() is called, so retry/rate tests run instantly."""

    now: float = 1_700_000_000.0
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now


@pytest.fixture
def fake():
    from automation_kit.api_pull import Runtime

    clock = FakeRuntime()
    runtime = Runtime(sleep=clock.sleep, monotonic=clock.monotonic, wall=clock.wall)
    return clock, runtime
