"""Run the four non-REST NexusX demo-style transport applications together."""

from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass
from time import monotonic, sleep

from tricycle_reaction_db.core.config import get_settings


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    port: int
    target: str
    path: str


SERVICES = (
    ServiceSpec(
        "Direct-list GraphQL",
        8000,
        "tricycle_reaction_db.api.apps:graphql_playground_app",
        "/graphql",
    ),
    ServiceSpec(
        "Paginated GraphQL",
        8005,
        "tricycle_reaction_db.api.apps:paginated_graphql_app",
        "/graphql",
    ),
    ServiceSpec(
        "UseCase MCP",
        8006,
        "tricycle_reaction_db.api.apps:mcp_app",
        "/mcp",
    ),
    ServiceSpec(
        "Voyager visualization",
        8008,
        "tricycle_reaction_db.api.apps:voyager_app",
        "/voyager/",
    ),
)


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_until_ready(
    host: str,
    processes: list[tuple[ServiceSpec, subprocess.Popen[bytes]]],
) -> None:
    deadline = monotonic() + 30
    pending = {spec.port for spec, _process in processes}
    while pending and monotonic() < deadline:
        for spec, process in processes:
            if spec.port not in pending:
                continue
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(f"{spec.name} exited during startup with code {return_code}")
            if _port_is_open(host, spec.port):
                pending.remove(spec.port)
        if pending:
            sleep(0.1)
    if pending:
        raise TimeoutError(f"services did not listen within 30 seconds: {sorted(pending)}")


def main() -> None:
    host = get_settings().api_host
    occupied = [spec.port for spec in SERVICES if _port_is_open(host, spec.port)]
    if occupied:
        raise SystemExit(f"refusing to replace existing listeners on ports: {occupied}")

    processes: list[tuple[ServiceSpec, subprocess.Popen[bytes]]] = []
    try:
        for spec in SERVICES:
            command = [
                sys.executable,
                "-m",
                "uvicorn",
                spec.target,
                "--host",
                host,
                "--port",
                str(spec.port),
            ]
            process = subprocess.Popen(command)
            processes.append((spec, process))
        _wait_until_ready(host, processes)
        print("NexusX services:")
        for spec, _process in processes:
            print(f"  {spec.port}  {spec.name:<24} http://{host}:{spec.port}{spec.path}")
        print("Press Ctrl+C to stop all services.")
        for _spec, process in processes:
            process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for _spec, process in processes:
            if process.poll() is None:
                process.terminate()
        for _spec, process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()


__all__ = ["SERVICES", "ServiceSpec", "main"]
