from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PACK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACK_DIR.parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.box_runtime import BoxRuntime  # noqa: E402
from src.connector_testkit import run_connector_contract_test  # noqa: E402
from src.default_connectors import build_box_connector_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline Shopify Connector Pack contract")
    parser.add_argument("box_config")
    parser.add_argument("--packs", default=str(PROJECT_ROOT / "packs"))
    args = parser.parse_args()
    runtime = BoxRuntime(args.box_config, args.packs)
    registry = build_box_connector_registry(runtime)
    request = json.loads((PACK_DIR / "fixture-orders.json").read_text(encoding="utf-8"))
    report = run_connector_contract_test(
        registry,
        runtime,
        "shopify.orders",
        request,
        expected_minimum_counts={
            "commerce.shopify_orders": 1,
            "commerce.shopify_transactions": 2,
            "commerce.shopify_refunds": 1,
        },
    )
    monthly_request = json.loads(
        (PACK_DIR / "fixture-monthly-order-evidence.json").read_text(encoding="utf-8")
    )
    monthly_report = run_connector_contract_test(
        registry,
        runtime,
        "shopify.monthly_order_evidence",
        monthly_request,
        expected_minimum_counts={
            "commerce.shopify_orders": 2,
            "commerce.shopify_transactions": 2,
            "commerce.shopify_refunds": 1,
        },
    )
    output = {
        "passed": report["passed"] and monthly_report["passed"],
        "reports": [report, monthly_report],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
