#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.jurisdiction_scaffold import scaffold_jurisdiction_pack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a source-backed jurisdiction Pack")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--country-code", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--source-authority", required=True)
    parser.add_argument("--source-title", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--verified-at", required=True)
    parser.add_argument("--rules-effective-at", required=True)
    args = parser.parse_args()
    result = scaffold_jurisdiction_pack(
        args.output_root,
        slug=args.slug,
        country_code=args.country_code,
        display_name=args.display_name,
        source_authority=args.source_authority,
        source_title=args.source_title,
        source_url=args.source_url,
        verified_at=args.verified_at,
        rules_effective_at=args.rules_effective_at,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
