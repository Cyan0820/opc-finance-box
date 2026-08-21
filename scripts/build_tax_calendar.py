#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.box_runtime import BoxRuntime  # noqa: E402
from src.tax_calendar import build_tax_calendar  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an evidence-backed OPC Finance Box tax calendar")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--period-year", type=int)
    parser.add_argument("--financial-year-end", help="Override entity FYE with YYYY-MM-DD")
    parser.add_argument("--gst-period-end", action="append", default=[], help="Repeatable YYYY-MM-DD")
    parser.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    args = parser.parse_args()

    anchors = {}
    if args.financial_year_end:
        anchors["financial_year_end"] = args.financial_year_end
    if args.gst_period_end:
        anchors["gst_period_end"] = args.gst_period_end
    result = build_tax_calendar(
        BoxRuntime(args.config, ROOT / "packs"),
        args.entity,
        period_year=args.period_year,
        anchors=anchors,
        as_of=args.as_of,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
