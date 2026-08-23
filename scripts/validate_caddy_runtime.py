from __future__ import annotations

import http.client
import os
import platform
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).parents[1]
CADDY_CONFIGURATION = REPOSITORY_ROOT / "infra/caddy/Caddyfile"
UPSTREAM_ADDRESS = ("127.0.0.1", 18000)
UPSTREAM_LISTEN_ADDRESS = ("0.0.0.0", UPSTREAM_ADDRESS[1])
HTTP_ADDRESS = ("127.0.0.1", 28080)
HTTPS_ADDRESS = ("127.0.0.1", 28443)
EDGE_HOST = "localhost"
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


def _headers() -> dict[str, str]:
    return {"Host": "localhost"}


def _wait_for_edge(process: subprocess.Popen[str]) -> None:
    context = ssl._create_unverified_context()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"caddy exited before readiness:\n{stdout}\n{stderr}")
        try:
            request = Request(
                f"https://{EDGE_HOST}:{HTTPS_ADDRESS[1]}/health/live",
                headers=_headers(),
            )
            with urlopen(request, context=context, timeout=0.5) as response:
                response.read()
            return
        except (OSError, HTTPError):
            time.sleep(0.1)
    process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    raise TimeoutError(
        "caddy did not listen on 127.0.0.1:28443 within 15 seconds\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}"
    )


def _request(path: str) -> tuple[int, bytes, dict[str, str]]:
    context = ssl._create_unverified_context()
    request = Request(
        f"https://{EDGE_HOST}:{HTTPS_ADDRESS[1]}{path}",
        headers=_headers(),
    )
    try:
        with urlopen(request, context=context, timeout=3) as response:
            return response.status, response.read(), dict(response.headers.items())
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers.items())


def _assert_proxy_paths() -> None:
    cases = {
        "/api/probe": "/api/probe",
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
        status, body, _ = _request(public_path)
        expected = f"upstream:{upstream_path}".encode()
        if status != 200 or body != expected:
            raise AssertionError(
                f"{public_path} returned {status} {body!r}, expected 200 {expected!r}"
            )

    status, _, _ = _request("/internal/metrics")
    if status != 404:
        raise AssertionError(f"/internal/metrics returned HTTP {status}, expected 404")
    if "/internal/metrics" in _ProbeHandler.observed_paths:
        raise AssertionError("/internal/metrics reached the private API upstream")

    status, _, headers = _request("/api/cache-probe")
    cache_control = headers.get("Cache-Control") or headers.get("cache-control")
    if status != 200 or cache_control != "private, no-store":
        raise AssertionError(
            f"/api did not receive the private no-store cache boundary: {headers!r}"
        )


def _assert_http_redirect() -> None:
    connection = http.client.HTTPConnection(*HTTP_ADDRESS, timeout=3)
    connection.request("GET", "/health/live", headers=_headers())
    response = connection.getresponse()
    status = response.status
    location = response.getheader("Location", "")
    response.read()
    connection.close()
    if status != 308:
        raise AssertionError(
            f"HTTP edge returned {status}, expected 308; headers={dict(response.headers)!r}"
        )
    if not location.startswith("https://localhost:28443/"):
        raise AssertionError(f"unexpected HTTPS redirect location: {location!r}")


def _assert_mcp_stream_is_not_buffered() -> None:
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(
        EDGE_HOST, HTTPS_ADDRESS[1], timeout=3, context=context
    )
    started = time.monotonic()
    connection.request("GET", "/mcp/stream-probe", headers=_headers())
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


def _caddy_command(
    caddy_binary: str | None,
    config_path: str,
    temporary_directory: str,
    data_directory: str,
    config_directory: str,
) -> list[str]:
    upstream = f"{UPSTREAM_ADDRESS[0]}:{UPSTREAM_ADDRESS[1]}"
    environment = {
        "CADDY_SERVER_NAME": "localhost",
        "CADDY_HTTP_PORT": str(HTTP_ADDRESS[1]),
        "CADDY_HTTPS_PORT": str(HTTPS_ADDRESS[1]),
        "CADDY_API_UPSTREAM": upstream,
        "CADDY_FRONTEND_UPSTREAM": upstream,
    }
    if caddy_binary:
        return [caddy_binary, "run", "--config", config_path, "--adapter", "caddyfile"]

    if not shutil.which("docker"):
        raise RuntimeError("caddy is not installed and docker is unavailable")
    command = [
        "docker",
        "run",
        "--rm",
        "-p",
        f"127.0.0.1:{HTTP_ADDRESS[1]}:{HTTP_ADDRESS[1]}",
        "-p",
        f"127.0.0.1:{HTTPS_ADDRESS[1]}:{HTTPS_ADDRESS[1]}",
        "-v",
        f"{temporary_directory}:/test:ro",
        "-v",
        f"{data_directory}:/data",
        "-v",
        f"{config_directory}:/config",
    ]
    if platform.system() != "Darwin":
        command[3:3] = ["--add-host", "host.docker.internal:host-gateway"]
    environment["CADDY_API_UPSTREAM"] = f"host.docker.internal:{UPSTREAM_ADDRESS[1]}"
    environment["CADDY_FRONTEND_UPSTREAM"] = f"host.docker.internal:{UPSTREAM_ADDRESS[1]}"
    for key, value in environment.items():
        command.extend(("-e", f"{key}={value}"))
    command.extend(("caddy:2.10.2-alpine", "caddy", "run", "--config", "/test/Caddyfile"))
    return command


def main() -> None:
    _ProbeHandler.observed_paths.clear()
    upstream = ThreadingHTTPServer(UPSTREAM_LISTEN_ADDRESS, _ProbeHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with tempfile.TemporaryDirectory(prefix="tricycle-caddy-") as temporary_directory:
        data_directory = Path(temporary_directory) / "data"
        config_directory = Path(temporary_directory) / "config"
        certificate = Path(temporary_directory) / "localhost.crt"
        private_key = Path(temporary_directory) / "localhost.key"
        data_directory.mkdir()
        config_directory.mkdir()
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-nodes",
                "-newkey",
                "rsa:2048",
                "-days",
                "1",
                "-keyout",
                str(private_key),
                "-out",
                str(certificate),
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        caddy_binary = None
        if os.environ.get("CADDY_RUNTIME_USE_DOCKER") != "1":
            caddy_binary = os.environ.get("CADDY_BINARY") or shutil.which("caddy")
        certificate_path = str(certificate) if caddy_binary else "/test/localhost.crt"
        private_key_path = str(private_key) if caddy_binary else "/test/localhost.key"
        test_configuration = Path(temporary_directory) / "Caddyfile"
        test_configuration.write_text(
            CADDY_CONFIGURATION.read_text(encoding="utf-8").replace(
                "{$CADDY_SERVER_NAME:localhost} {",
                f"{{$CADDY_SERVER_NAME:localhost}} {{\n\ttls {certificate_path} {private_key_path}",
                1,
            ),
            encoding="utf-8",
        )
        command = _caddy_command(
            caddy_binary,
            str(test_configuration),
            temporary_directory,
            str(data_directory),
            str(config_directory),
        )
        environment = os.environ.copy()
        environment.update(
            {
                "CADDY_SERVER_NAME": "localhost",
                "CADDY_HTTP_PORT": str(HTTP_ADDRESS[1]),
                "CADDY_HTTPS_PORT": str(HTTPS_ADDRESS[1]),
                "CADDY_API_UPSTREAM": f"{UPSTREAM_ADDRESS[0]}:{UPSTREAM_ADDRESS[1]}",
                "CADDY_FRONTEND_UPSTREAM": f"{UPSTREAM_ADDRESS[0]}:{UPSTREAM_ADDRESS[1]}",
            }
        )
        caddy = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            _wait_for_edge(caddy)
            _assert_http_redirect()
            _assert_proxy_paths()
            _assert_mcp_stream_is_not_buffered()
        finally:
            caddy.terminate()
            try:
                stdout, stderr = caddy.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                caddy.kill()
                stdout, stderr = caddy.communicate()
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=2)
    if caddy.returncode not in {0, -15, 143}:
        raise RuntimeError(f"caddy exited with {caddy.returncode}:\n{stdout}\n{stderr}")
    print(
        "Caddy runtime probe passed: 17 proxy paths, HTTPS redirect, private metrics boundary, "
        "cache boundary, and unbuffered MCP streaming."
    )


if __name__ == "__main__":
    main()
