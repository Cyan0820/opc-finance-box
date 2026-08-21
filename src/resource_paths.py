from __future__ import annotations

import sysconfig
from pathlib import Path


RESOURCE_SHARE_NAME = "opc-finance-box"


def find_resource_root(
    *,
    local_root: str | Path | None = None,
    data_prefix: str | Path | None = None,
) -> Path:
    """Locate repository assets in editable/source and normal wheel installations."""
    source_root = Path(local_root) if local_root is not None else Path(__file__).resolve().parent.parent
    if (source_root / "packs").is_dir():
        return source_root
    prefix = Path(data_prefix) if data_prefix is not None else Path(sysconfig.get_path("data"))
    shared_root = prefix / "share" / RESOURCE_SHARE_NAME
    if (shared_root / "packs").is_dir():
        return shared_root
    # Return the source location so callers produce a precise missing-file error.
    return source_root
