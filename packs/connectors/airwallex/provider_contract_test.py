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
    parser = argparse.ArgumentParser(description="Run the offline Airwallex Connector contract")
    parser.add_argument("box_config")
    parser.add_argument("--packs", default=str(PROJECT_ROOT / "packs"))
    args = parser.parse_args()
    runtime = BoxRuntime(args.box_config, args.packs)
    request = json.loads(
        (PACK_DIR / "fixture-approved-expenses.json").read_text(encoding="utf-8")
    )
    report = run_connector_contract_test(
        build_box_connector_registry(runtime), runtime,
        "airwallex.approved_expenses", request,
        expected_minimum_counts={"finance.expense_evidence": 2},
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
