from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .box_config import load_pack_manifest


class ConnectorScaffoldError(ValueError):
    pass


def scaffold_connector_pack(
    output_root: str | Path,
    *,
    slug: str,
    display_name: str,
    secret_env: str,
    base_url: str,
) -> dict[str, Any]:
    slug = slug.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", slug):
        raise ConnectorScaffoldError("slug must use lowercase letters, digits and underscores")
    if not display_name.strip():
        raise ConnectorScaffoldError("display_name is required")
    if not re.fullmatch(r"OPC_[A-Z0-9_]+", secret_env):
        raise ConnectorScaffoldError("secret_env must be an OPC_ prefixed uppercase environment variable")
    parsed_url = urllib.parse.urlsplit(base_url)
    if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.fragment:
        raise ConnectorScaffoldError("base_url must be a fixed HTTPS URL without credentials or fragments")
    destination = Path(output_root) / slug
    if destination.exists():
        raise ConnectorScaffoldError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    pack_id = f"connector.{slug}"
    capability = f"connector.{slug}_commerce_import"
    connector_id = f"{slug}.commerce_api"
    manifest = {
        "id": pack_id, "kind": "connector", "display_name": display_name.strip(),
        "version": "0.1.0", "status": "experimental",
        "capabilities": [capability, "connector.batch_evidence_contract"],
        "requires": ["core.finance"], "conflicts": [], "manual_review_gates": [],
        "connector_provider": {"module": "provider.py", "factory": "register_connectors"},
    }
    provider = f'''from __future__ import annotations

import os
from typing import Any

from src.connector_sdk import ConnectorContext, ConnectorDefinition, ConnectorError, ConnectorRegistry
from src.connector_http import fetch_paginated_json, urllib_transport
from src.default_connectors import _commerce_api_example_handler

SECRET_ENV = "{secret_env}"
INLINE_SECRET_KEYS = {{"token", "api_key", "secret", "password", "authorization"}}
API_ENDPOINT = "{base_url}"
HTTP_TRANSPORT = urllib_transport
HTTP_SLEEPER = __import__("time").sleep


def _handler(request: dict[str, Any], context: ConnectorContext) -> dict[str, Any]:
    forbidden = sorted(key for key in request if key.lower() in INLINE_SECRET_KEYS)
    if forbidden:
        raise ConnectorError("credentials must not be passed in connector requests: " + ", ".join(forbidden))
    mode = str(request.get("mode") or "fixture")
    if mode == "fetch":
        credential = os.environ.get(SECRET_ENV)
        if not credential:
            raise ConnectorError(f"missing credential environment variable: {{SECRET_ENV}}")
        fetched = fetch_paginated_json(
            API_ENDPOINT, bearer_token=credential, source_name="{slug}",
            start_cursor=request.get("start_cursor"), transport=HTTP_TRANSPORT, sleeper=HTTP_SLEEPER,
        )
        orders, payouts, returns, return_receipts, import_costs = [], [], [], [], []
        for page in fetched["pages"]:
            if any(not isinstance(page.get(field) or [], list) for field in (
                "orders", "payouts", "returns", "returnReceipts", "importCosts",
            )):
                raise ConnectorError(
                    "API pages must contain orders, payouts, returns and returnReceipts lists when present"
                )
            orders.extend(page.get("orders") or [])
            payouts.extend(page.get("payouts") or [])
            returns.extend(page.get("returns") or [])
            return_receipts.extend(page.get("returnReceipts") or [])
            import_costs.extend(page.get("importCosts") or [])
        batch = _commerce_api_example_handler({{"payload": {{
            "batch_id": fetched["batch_id"], "source_name": fetched["source_name"],
            "orders": orders, "payouts": payouts, "returns": returns,
            "returnReceipts": return_receipts, "importCosts": import_costs,
        }}}}, context)
        batch["source"].update({{
            "network_access_performed": True, "page_count": fetched["page_count"],
            "retry_count": fetched["retry_count"],
        }})
        return batch
    if mode != "fixture":
        raise ConnectorError("mode must be fixture or fetch")
    return _commerce_api_example_handler(request, context)


def register_connectors(registry: ConnectorRegistry) -> None:
    registry.register(ConnectorDefinition(
        connector_id="{connector_id}", pack_id="{pack_id}", capability="{capability}",
        display_name="{display_name.strip()}",
        dataset_types=(
            "commerce.orders", "commerce.settlements", "commerce.returns",
            "commerce.return_receipts",
            "commerce.import_costs",
        ), handler=_handler,
        business_keys={{
            "commerce.orders": ("order_id",),
            "commerce.settlements": ("settlement_id",),
            "commerce.returns": ("return_id", "sku"),
            "commerce.return_receipts": ("receipt_id",),
            "commerce.import_costs": ("entry_line_id",),
        }},
        credential_env=(SECRET_ENV,), network_access=True,
    ))
'''
    fixture = {
        "mode": "fixture",
        "payload": {
            "batch_id": f"{slug}-fixture-2026-07", "source_name": f"{slug}-fixture",
            "orders": [{
                "id": "ORDER-1", "orderNumber": "ORDER-1", "entityId": "demo_entity",
                "processedAt": "2026-07-01T00:00:00Z", "store": "DTC", "destinationCountry": "US",
                "currency": "USD", "subtotal": 100, "discounts": 0, "shippingIncome": 0,
                "tax": 0, "refund": 0, "refundedTax": 0, "cost": 40,
                "fulfillmentCost": 5, "shippingCost": 5,
            }],
            "payouts": [{
                "id": "PAYOUT-1", "payoutId": "PAYOUT-1", "entityId": "demo_entity",
                "period": "2026-07", "store": "DTC", "currency": "USD", "orderInflow": 100,
                "fees": 3, "taxRemitted": 0, "adjustments": 0, "payout": 97,
            }],
        },
    }
    contract = {
        "schema_version": 1, "connector_id": connector_id, "secret_env": secret_env,
        "credentials_in_request_forbidden": True, "network_fetch_implemented": True,
        "base_url": base_url,
        "fixture": "fixture-request.json",
        "runner": "provider_contract_test.py",
        "required_tests": ["idempotent_batch", "evidence", "entity_scope", "pagination", "retry", "timeout", "redaction"],
    }
    for name, payload in (("manifest.json", manifest), ("fixture-request.json", fixture), ("provider-contract.json", contract)):
        (destination / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "provider.py").write_text(provider, encoding="utf-8")
    runner = f'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = next((path for path in HERE.parents if (path / "pyproject.toml").is_file()), None)
if PROJECT_ROOT is None:
    raise SystemExit("cannot locate OPC Finance Box project root")
sys.path.insert(0, str(PROJECT_ROOT))

from src.box_runtime import BoxRuntime
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry

parser = argparse.ArgumentParser(description="Run generated Connector fixture contract")
parser.add_argument("box_config", type=Path, help="Box config that selects {pack_id}")
parser.add_argument("--packs", type=Path, default=PROJECT_ROOT / "packs")
args = parser.parse_args()
runtime = BoxRuntime(args.box_config, args.packs)
fixture = json.loads((HERE / "fixture-request.json").read_text(encoding="utf-8"))
result = run_connector_contract_test(
    build_box_connector_registry(runtime), runtime, "{connector_id}", fixture,
    expected_minimum_counts={{"commerce.orders": 1, "commerce.settlements": 1}},
)
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["passed"] else 1)
'''
    runner_path = destination / "provider_contract_test.py"
    runner_path.write_text(runner, encoding="utf-8")
    runner_path.chmod(0o755)
    load_pack_manifest(destination / "manifest.json")
    return {
        "pack_id": pack_id, "connector_id": connector_id, "destination": str(destination),
        "secret_env": secret_env,
        "base_url": base_url,
        "next_steps": ["把 Pack 加入 Box connectors", "运行 provider_contract_test.py <box-config>", "按真实 API 字段修改映射并增加分页/失败 fixtures"],
    }
