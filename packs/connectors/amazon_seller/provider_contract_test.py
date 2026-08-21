from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.box_runtime import BoxRuntime
from src.connector_testkit import run_connector_contract_test
from src.default_connectors import build_box_connector_registry


def main() -> int:
    config = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "examples/boxes/us_marketplace_amazon_seller_c_corp.json"
    runtime = BoxRuntime(config, ROOT / "packs")
    registry = build_box_connector_registry(runtime)
    cases = [
        (
            "amazon_seller.transaction_activity",
            "fixture-transactions.json",
            {"commerce.amazon_seller_transactions": 3},
        ),
        (
            "amazon_seller.marketplace_evidence",
            "fixture-marketplace-evidence.json",
            {
                "commerce.amazon_seller_orders": 3,
                "commerce.amazon_seller_inventory": 2,
                "commerce.amazon_seller_transactions": 2,
            },
        ),
    ]
    reports = []
    for connector_id, fixture_name, counts in cases:
        fixture = json.loads(
            Path(__file__).with_name(fixture_name).read_text(encoding="utf-8")
        )
        reports.append(run_connector_contract_test(
            registry, runtime, connector_id, fixture, expected_minimum_counts=counts,
        ))
    print(json.dumps({"reports": reports}, ensure_ascii=False, sort_keys=True))
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
