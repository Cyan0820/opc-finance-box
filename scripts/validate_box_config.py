#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.box_config import BoxConfigError, resolve_box_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and resolve an OPC Finance Box configuration")
    parser.add_argument("config", type=Path, help="Path to a Box JSON configuration")
    parser.add_argument("--packs", type=Path, default=ROOT / "packs", help="Pack catalog root")
    args = parser.parse_args()
    try:
        resolved = resolve_box_file(args.config, args.packs)
    except BoxConfigError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(resolved, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
