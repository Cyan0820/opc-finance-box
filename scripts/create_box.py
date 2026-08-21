#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.box_config import load_pack_catalog  # noqa: E402
from src.box_scaffold import create_box_config, list_box_options  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a validated OPC Finance Box configuration")
    parser.add_argument("spec", type=Path, nargs="?", help="Simplified JSON specification")
    parser.add_argument("--packs", type=Path, default=ROOT / "packs")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--list-options", action="store_true")
    args = parser.parse_args()
    catalog = load_pack_catalog(args.packs)
    if args.list_options:
        print(json.dumps(list_box_options(catalog), ensure_ascii=False, indent=2))
        return 0
    if not args.spec or not args.output:
        parser.error("spec and --output are required unless --list-options is used")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    config = create_box_config(spec, catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
