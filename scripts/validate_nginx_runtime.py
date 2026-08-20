from __future__ import annotations

import http.client
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

REPOSITORY_ROOT = Path(__file__).parents[1]
NGINX_CONFIGURATION = REPOSITORY_ROOT / "infra/nginx/tricycle.conf"
UPSTREAM_ADDRESS = ("127.0.0.1", 8000)
EDGE_ADDRESS = ("127.0.0.1", 8080)
STREAM_DELAY_SECONDS = 0.75


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    observed_paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        self.observed_paths.append(self.path)
        if self.path == "/mcp/stream-probe":
            payload = b"first\nsecond\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(b"first\n")
            self.wfile.flush()
            time.sleep(STREAM_DELAY_SECONDS)
            self.wfile.write(b"second\n")
            self.wfile.flush()
            return

        payload = f"upstream:{self.path}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        del format
        return


def _wait_for_edge(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"nginx exited before readiness:\n{stdout}\n{stderr}")
        try:
            connection = http.client.HTTPConnection(*EDGE_ADDRESS, timeout=0.2)
            connection.request("GET", "/health/live")
            response = connection.getresponse()
            response.read()
            connection.close()
            return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("nginx did not listen on 127.0.0.1:8080 within 5 seconds")


def _assert_proxy_paths() -> None:
    cases = {
        "/health/live": "/health/live",
        "/health/ready": "/health/ready",
        "/docs": "/docs",
        "/docs/oauth2-redirect": "/docs/oauth2-redirect",
        "/redoc": "/redoc",
        "/openapi.json": "/openapi.json",
        "/graphql": "/graphql",
        "/graphql/probe": "/graphql/probe",
        "/nexusx/graphql": "/graphql",
        "/nexusx/graphql/probe": "/graphql/probe",
        "/nexusx/core/docs": "/docs",
        "/nexusx/paginated-graphql/probe": "/graphql/probe",
        "/mcp/probe": "/mcp/probe",
        "/nexusx/mcp/probe": "/mcp/probe",
        "/nexusx/rest/api/artifacts": "/api/artifacts",
        "/nexusx/voyager/probe": "/voyager/probe",
    }
    for public_path, upstream_path in cases.items():
        with urlopen(f"http://127.0.0.1:8080{public_path}", timeout=2) as response:
            body = response.read().decode()
        expected = f"upstream:{upstream_path}"
        if body != expected:
            raise AssertionError(f"{public_path} returned {body!r}, expected {expected!r}")

    try:
        urlopen("http://127.0.0.1:8080/internal/metrics", timeout=2)
    except HTTPError as error:
        if error.code != 404:
            raise AssertionError(
                f"/internal/metrics returned HTTP {error.code}, expected 404"
            ) from error
    else:
        raise AssertionError("/internal/metrics was published by the edge proxy")
    if "/internal/metrics" in _ProbeHandler.observed_paths:
        raise AssertionError("/internal/metrics reached the private API upstream")


def _assert_mcp_stream_is_not_buffered() -> None:
    connection = http.client.HTTPConnection(*EDGE_ADDRESS, timeout=2)
    started = time.monotonic()
    connection.request("GET", "/mcp/stream-probe")
    response = connection.getresponse()
    first = response.read(6)
    first_byte_elapsed = time.monotonic() - started
    remainder = response.read()
    total_elapsed = time.monotonic() - started
    connection.close()

    if first != b"first\n" or remainder != b"second\n":
        raise AssertionError(f"unexpected MCP stream payload: {first + remainder!r}")
    if first_byte_elapsed >= STREAM_DELAY_SECONDS / 2:
        raise AssertionError(
            f"MCP first chunk was buffered for {first_byte_elapsed:.3f}s before delivery"
        )
    if total_elapsed < STREAM_DELAY_SECONDS * 0.8:
        raise AssertionError(
            f"stream probe did not preserve the upstream delay: {total_elapsed:.3f}s"
        )


def main() -> None:
    _ProbeHandler.observed_paths.clear()
    upstream = ThreadingHTTPServer(UPSTREAM_ADDRESS, _ProbeHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    nginx = subprocess.Popen(
        [
            "nginx",
            "-p",
            f"{REPOSITORY_ROOT}/",
            "-c",
            str(NGINX_CONFIGURATION),
            "-g",
            "daemon off;",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        _wait_for_edge(nginx)
        _assert_proxy_paths()
        _assert_mcp_stream_is_not_buffered()
    finally:
        nginx.terminate()
        try:
            stdout, stderr = nginx.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            nginx.kill()
            stdout, stderr = nginx.communicate()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)
    if nginx.returncode not in {0, -15}:
        raise RuntimeError(f"nginx exited with {nginx.returncode}:\n{stdout}\n{stderr}")
    print(
        "Nginx runtime probe passed: 16 proxy paths, private metrics boundary, "
        "and unbuffered MCP streaming."
    )


if __name__ == "__main__":
    main()
