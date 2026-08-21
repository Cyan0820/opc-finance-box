from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .api_auth import hash_token
from .box_runtime import BoxRuntime


class DeploymentSmokeError(ValueError):
    """Raised when an isolated workbench cannot prove its deployment boundary."""


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _ephemeral_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _read_json(
    opener: Any, url: str, *, stage: str, headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        with opener.open(Request(url, headers=headers or {}), timeout=1.5) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_RESPONSE_BYTES:
                raise DeploymentSmokeError("smoke endpoint response exceeds 2 MiB")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise DeploymentSmokeError("smoke endpoint response exceeds 2 MiB")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise DeploymentSmokeError("smoke endpoint must return a JSON object")
            return payload, {key.lower(): value for key, value in response.headers.items()}
    except DeploymentSmokeError:
        raise
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DeploymentSmokeError(f"smoke {stage} endpoint did not return trusted JSON") from exc


def _expect_http_status(
    opener: Any,
    request: Request,
    expected_status: int,
    *,
    stage: str,
) -> None:
    try:
        with opener.open(request, timeout=1.5):
            pass
    except HTTPError as exc:
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        if exc.code != expected_status or len(body) > MAX_RESPONSE_BYTES:
            raise DeploymentSmokeError(f"smoke {stage} returned an unexpected HTTP status") from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
            raise DeploymentSmokeError(f"smoke {stage} error response is not trusted JSON") from parse_error
        if not isinstance(payload, dict) or not payload.get("type"):
            raise DeploymentSmokeError(f"smoke {stage} error response is missing its type")
        return
    except (URLError, TimeoutError) as exc:
        raise DeploymentSmokeError(f"smoke {stage} endpoint was not reachable") from exc
    raise DeploymentSmokeError(f"smoke {stage} unexpectedly allowed the request")


def run_deployment_smoke(
    config_path: str | Path,
    packs_root: str | Path,
    *,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    """Start the real workbench on loopback with isolated data, probe it, then stop it."""
    if (
        not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool)
        or not 3 <= timeout_seconds <= 60
    ):
        raise DeploymentSmokeError("timeout_seconds must be an integer from 3 to 60")
    config = Path(config_path).expanduser().resolve()
    packs = Path(packs_root).expanduser().resolve()
    runtime = BoxRuntime(config, packs)
    expected_fingerprint = runtime.snapshot()["fingerprint"]
    started = time.monotonic()
    checks: list[dict[str, Any]] = []
    process: subprocess.Popen[bytes] | None = None
    process_exit_code: int | None = None
    isolated_root: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="opc-finance-smoke-") as temp_dir:
            isolated_root = Path(temp_dir)
            data_root = isolated_root / "runtime-data"
            reader_token = secrets.token_urlsafe(32)
            operator_token = secrets.token_urlsafe(32)
            auth_policy = isolated_root / "api-auth.json"
            auth_policy.write_text(json.dumps({
                "schema_version": 1,
                "principals": [
                    {
                        "principal_id": "smoke_reader",
                        "token_sha256": hash_token(reader_token),
                        "roles": ["reader"],
                    },
                    {
                        "principal_id": "smoke_operator",
                        "token_sha256": hash_token(operator_token),
                        "roles": ["operator"],
                    },
                ],
            }), encoding="utf-8")
            auth_policy.chmod(0o600)
            port = _ephemeral_loopback_port()
            module_root = Path(__file__).resolve().parent.parent
            environment = {
                key: os.environ[key]
                for key in ("PATH", "LANG", "LC_ALL", "TZ")
                if os.environ.get(key)
            }
            environment.update({
                "PYTHONPATH": str(module_root),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "OPC_FINANCE_BOX_CONFIG": str(config),
                "OPC_FINANCE_PACKS_ROOT": str(packs),
                "OPC_FINANCE_DATA_DIR": str(data_root),
                "OPC_FINANCE_HOST": "127.0.0.1",
                "OPC_FINANCE_PORT": str(port),
                "OPC_FINANCE_API_AUTH_FILE": str(auth_policy),
            })
            process = subprocess.Popen(
                [sys.executable, "-m", "src.server"],
                cwd=str(isolated_root), env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
            opener = build_opener(ProxyHandler({}))
            base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + timeout_seconds
            health: dict[str, Any] | None = None
            health_headers: dict[str, str] = {}
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise DeploymentSmokeError("workbench exited before the health endpoint became ready")
                try:
                    health, health_headers = _read_json(
                        opener, base_url + "/api/health", stage="liveness",
                    )
                    break
                except DeploymentSmokeError:
                    time.sleep(0.1)
            if health is None:
                raise DeploymentSmokeError("workbench did not become healthy before the smoke timeout")
            if health.get("status") != "ok":
                raise DeploymentSmokeError("health endpoint did not report ok")
            checks.append({"check_id": "http.liveness", "status": "pass"})

            _expect_http_status(
                opener, Request(base_url + "/api/box"), 401,
                stage="authentication required",
            )
            checks.append({"check_id": "auth.required", "status": "pass"})
            reader_headers = {"Authorization": f"Bearer {reader_token}"}
            box, box_headers = _read_json(
                opener, base_url + "/api/box", stage="Box readiness",
                headers=reader_headers,
            )
            checks.append({"check_id": "auth.reader_allowed", "status": "pass"})
            _expect_http_status(
                opener,
                Request(
                    base_url + "/api/box/pipeline-schedule/run",
                    data=b"{}",
                    headers={**reader_headers, "Content-Type": "application/json"},
                    method="POST",
                ),
                403,
                stage="reader/operator separation",
            )
            checks.append({"check_id": "auth.reader_operator_separation", "status": "pass"})
            _expect_http_status(
                opener,
                Request(
                    base_url + "/api/box/pipeline-schedule/run",
                    data=b"{}",
                    headers={
                        "Authorization": f"Bearer {operator_token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                ),
                409,
                stage="operator authorization",
            )
            checks.append({"check_id": "auth.operator_allowed", "status": "pass"})
            observed_fingerprint = ((box.get("context") or {}).get("runtime") or {}).get("fingerprint")
            if observed_fingerprint != expected_fingerprint:
                raise DeploymentSmokeError("workbench loaded a different Box runtime fingerprint")
            checks.append({"check_id": "runtime.expected_box", "status": "pass"})

            observability, observability_headers = _read_json(
                opener, base_url + "/api/box/pipeline-observability",
                stage="observability readiness", headers=reader_headers,
            )
            if observability.get("runtime_fingerprint") != expected_fingerprint:
                raise DeploymentSmokeError("observability runtime fingerprint does not match the Box")
            if observability.get("external_actions_performed") is not False:
                raise DeploymentSmokeError("observability did not preserve the external-action boundary")
            if observability.get("schedule_configured") is not False:
                raise DeploymentSmokeError("isolated smoke unexpectedly loaded a Pipeline schedule")
            checks.append({"check_id": "observability.read_only", "status": "pass"})

            connector_sync, connector_sync_headers = _read_json(
                opener, base_url + "/api/box/connector-sync",
                stage="connector sync control readiness", headers=reader_headers,
            )
            if connector_sync.get("counts") != {
                "attempts": 0, "checkpoints": 0,
                "checkpoint_candidates": 0, "quarantine": 0,
            }:
                raise DeploymentSmokeError("isolated smoke unexpectedly found connector sync state")
            if (
                connector_sync.get("raw_requests_included") is not False
                or connector_sync.get("raw_responses_included") is not False
                or connector_sync.get("external_actions_performed") is not False
            ):
                raise DeploymentSmokeError("connector sync status did not preserve its read-only boundary")
            checks.append({"check_id": "connector_sync.read_only", "status": "pass"})

            for headers in (
                health_headers, box_headers, observability_headers, connector_sync_headers,
            ):
                if headers.get("x-content-type-options") != "nosniff":
                    raise DeploymentSmokeError("smoke endpoint is missing required security headers")
                if "no-store" not in headers.get("cache-control", ""):
                    raise DeploymentSmokeError("smoke endpoint is missing no-store cache control")
            checks.append({"check_id": "http.security_headers", "status": "pass"})
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process is not None:
            process_exit_code = process.returncode
    if isolated_root is None or isolated_root.exists():
        raise DeploymentSmokeError("isolated smoke directory was not cleaned up")
    if process_exit_code != 0:
        raise DeploymentSmokeError("workbench did not complete a graceful SIGTERM shutdown")
    return {
        "schema_version": 1,
        "passed": True,
        "runtime_fingerprint": expected_fingerprint,
        "checks": checks,
        "counts": {"total": len(checks), "passed": len(checks), "failed": 0},
        "duration_ms": int((time.monotonic() - started) * 1000),
        "server_process_terminated": True,
        "server_exit_code": process_exit_code,
        "isolated_runtime_data_removed": True,
        "authentication_mode": "temporary_role_policy_on_loopback",
        "raw_smoke_tokens_returned": False,
        "connector_dispatch_performed": False,
        "schedule_dispatch_performed": False,
        "secret_values_inherited": False,
        "network_access": "loopback_only",
        "external_actions_performed": False,
    }
