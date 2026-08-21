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
from src.commerce_runner import run_commerce_box  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Commerce/DTC OPC Finance Box")
    parser.add_argument("inputs", nargs="+", type=Path, help="Order and settlement CSV/XLSX files")
    parser.add_argument(
        "--box",
        type=Path,
        default=ROOT / "examples" / "boxes" / "cn_dtc_store.json",
        help="Box configuration JSON",
    )
    parser.add_argument("--packs", type=Path, default=ROOT / "packs", help="Pack catalog root")
    parser.add_argument("--entity", help="Default legal entity ID for rows without an entity")
    parser.add_argument("--channel", help="Default channel for rows without a channel")
    args = parser.parse_args()
    runtime = BoxRuntime(args.box, args.packs)
    payload = run_commerce_box(
        runtime,
        args.inputs,
        default_entity_id=args.entity,
        default_channel=args.channel,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
