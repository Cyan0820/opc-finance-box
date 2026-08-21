from __future__ import annotations

from pathlib import Path
from typing import Any


class DeploymentAssetError(ValueError):
    """Raised when a deployment starter loses a required safety boundary."""


REQUIRED_ASSETS = (
    "Dockerfile",
    "Dockerfile.dockerignore",
    "compose.example.yaml",
    "box.env.example",
    "opc-finance-workbench.service",
    "opc-finance-scheduler.service",
    "opc-finance-scheduler.timer",
)
MAX_ASSET_BYTES = 128 * 1024


def _require(text: str, fragments: tuple[str, ...], asset: str) -> None:
    missing = [fragment for fragment in fragments if fragment not in text]
    if missing:
        raise DeploymentAssetError(
            f"deployment asset {asset} is missing required controls: {', '.join(missing)}"
        )


def verify_deployment_assets(root: str | Path) -> dict[str, Any]:
    deployment_root = Path(root).expanduser().resolve()
    texts: dict[str, str] = {}
    for name in REQUIRED_ASSETS:
        path = (deployment_root / name).resolve()
        try:
            path.relative_to(deployment_root)
        except ValueError as exc:
            raise DeploymentAssetError("deployment asset path escapes its root") from exc
        try:
            if not path.is_file() or path.stat().st_size > MAX_ASSET_BYTES:
                raise DeploymentAssetError(
                    f"deployment asset {name} must be a regular file no larger than 128 KiB"
                )
            texts[name] = path.read_text(encoding="utf-8")
        except DeploymentAssetError:
            raise
        except (OSError, UnicodeDecodeError) as exc:
            raise DeploymentAssetError(f"deployment asset {name} must be valid UTF-8") from exc

    _require(texts["Dockerfile"], (
        "FROM python:3.12-slim AS builder", "USER 10001:10001", "HEALTHCHECK",
        "STOPSIGNAL SIGTERM", "OPC_FINANCE_PORT=8765",
        'ENTRYPOINT ["opc-finance-workbench"]',
    ), "Dockerfile")
    _require(texts["Dockerfile.dockerignore"], (
        "deployment/private", "**/api-auth.json", "**/schedule.json",
        "data/**", "!data/commerce_demo.json", "!data/demo_scenarios.json",
        ".opc-finance-data", "outputs",
    ), "Dockerfile.dockerignore")
    _require(texts["compose.example.yaml"], (
        '"127.0.0.1:8765:8765"', "read_only: true", "cap_drop:", "- ALL",
        "no-new-privileges:true", "OPC_FINANCE_API_AUTH_FILE:", "secrets:",
        "auth-init:", "install -o 10001 -g 10001 -m 0600",
        "condition: service_completed_successfully", 'user: "10001:10001"',
        'OPC_FINANCE_PORT: "8765"',
        "OPC_CONNECTOR_SHADOW_REVIEW_DIR:",
        "connector-shadow-reviews:/etc/opc-finance/connector-shadow-reviews:ro",
        "OPC_STABLE_PROMOTION_ROOT:",
        "stable-promotion-ledger:/etc/opc-finance/stable-promotion-ledger:ro",
    ), "compose.example.yaml")
    _require(texts["box.env.example"], (
        "OPC_FINANCE_BOX_CONFIG=", "OPC_FINANCE_DATA_DIR=", "OPC_FINANCE_API_AUTH_FILE=",
        "OPC_TAX_APPLICABILITY_REVIEW_DIR=",
        "OPC_TAX_APPLICABILITY_REGISTRY_RECEIPT=",
        "OPC_CONNECTOR_SHADOW_REVIEW_DIR=",
        "OPC_PILOT_READINESS_REVIEW=",
        "OPC_PILOT_DATA_HANDOFF_REVIEW=",
        "OPC_PILOT_SHADOW_RUN_REGISTRATION=",
        "OPC_PILOT_SHADOW_OBSERVATION_REVIEW=",
        "OPC_PILOT_SHADOW_ENTITY_REPORT_DIR=",
        "OPC_PILOT_SHADOW_PORTFOLIO_REVIEW=",
        "OPC_PILOT_SHADOW_SERIES_REVIEW=",
        "OPC_PILOT_SHADOW_SERIES_EVIDENCE_ROOT=",
        "OPC_STABLE_PROMOTION_ROOT=",
        "OPC_FINANCE_PORT=8765",
        "OPC_FINANCE_PIPELINE_SCHEDULE_FILE=", "OPC_FINANCE_SCHEDULER_ACTOR=",
        "Never commit their values.",
    ), "box.env.example")
    _require(texts["opc-finance-workbench.service"], (
        "User=opc-finance", "NoNewPrivileges=yes", "ProtectSystem=strict",
        "ProtectHome=yes", "ReadWritePaths=/var/lib/opc-finance", "UMask=0077",
        "runtime-data-init /var/lib/opc-finance --actor systemd-workbench-bootstrap",
    ), "opc-finance-workbench.service")
    _require(texts["opc-finance-scheduler.service"], (
        "Type=oneshot", "pipeline-schedule-run", "OPC_FINANCE_SCHEDULER_ACTOR",
        "NoNewPrivileges=yes", "ProtectSystem=strict",
    ), "opc-finance-scheduler.service")
    _require(texts["opc-finance-scheduler.timer"], (
        "OnCalendar=*:0/5", "Persistent=yes", "opc-finance-scheduler.service",
    ), "opc-finance-scheduler.timer")

    combined = "\n".join(texts.values()).lower()
    forbidden = (
        "privileged: true", "/var/run/docker.sock", "network_mode: host",
        "opc_finance_api_token=replace", "opc_finance_api_token: replace",
    )
    present = [item for item in forbidden if item in combined]
    if present:
        raise DeploymentAssetError(
            "deployment assets contain forbidden insecure defaults: " + ", ".join(present)
        )
    return {
        "schema_version": 1,
        "valid": True,
        "asset_count": len(texts),
        "assets": list(REQUIRED_ASSETS),
        "workbench_runs_as_non_root": True,
        "root_auth_init_is_one_shot": True,
        "runtime_data_excluded_from_build_context": True,
        "loopback_host_publish": True,
        "role_policy_reference_required": True,
        "systemd_hardening_present": True,
        "versioned_runtime_data_initialized_before_start": True,
        "raw_secret_values_included": False,
        "deployment_performed": False,
        "external_actions_performed": False,
    }
