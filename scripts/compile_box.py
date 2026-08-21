#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.box_compiler import compile_box_file, write_compiled_box  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile an OPC Finance Box deployment contract")
    parser.add_argument("config", type=Path, help="Box configuration JSON")
    parser.add_argument("--packs", type=Path, default=ROOT / "packs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compiled = compile_box_file(args.config, args.packs)
    paths = write_compiled_box(compiled, args.output)
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
