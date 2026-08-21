from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .deployment_assets import REQUIRED_ASSETS
from .handoff_verify import _safe_member_name
from .resource_paths import find_resource_root


class SourceKitError(ValueError):
    """Raised when a fork-ready source archive cannot be built or trusted."""


MANIFEST_NAME = "source-kit-manifest.json"
GUIDE_NAME = "SOURCE-KIT.md"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBER_COUNT = 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
TOP_LEVEL_FILES = (
    ".gitignore",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "design-qa.md",
    "pyproject.toml",
    "run.py",
)
TREE_RULES: tuple[tuple[str, frozenset[str]], ...] = (
    ("src", frozenset({".py"})),
    ("tests", frozenset({".py"})),
    ("packs", frozenset({".json", ".py"})),
    ("examples", frozenset({".json", ".csv", ".md"})),
    ("docs", frozenset({".md"})),
    ("public", frozenset({".html", ".css", ".js"})),
    ("box", frozenset({".json"})),
    ("evals", frozenset({".json"})),
    ("skills", frozenset({".md", ".yaml"})),
    ("scripts", frozenset({".py", ".mjs"})),
)
DATA_FILES = ("commerce_demo.json", "demo_scenarios.json")
WORKFLOW_FILES = ("tests.yml",)
IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__", "node_modules"})
MANIFEST_KEYS = {
    "schema_version",
    "product",
    "source_kit_schema_version",
    "content_fingerprint",
    "file_count",
    "category_counts",
    "files",
    "fork_ready_source_tree",
    "editable_source_included",
    "tests_included",
    "demo_data_included",
    "dependency_lock_included",
    "vendored_dependencies_included",
    "git_history_included",
    "runtime_data_included",
    "private_evidence_included",
    "build_artifacts_included",
    "credentials_included",
    "real_financial_source_files_included",
    "external_actions_performed",
}
FILE_RECORD_KEYS = {"path", "size_bytes", "sha256"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _source_layout(project_root: str | Path | None) -> tuple[Path, Path, Path]:
    resource_root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else find_resource_root().resolve()
    )
    if not (resource_root / "packs").is_dir():
        raise SourceKitError("Source Kit resource root is missing the Pack catalog")
    if (resource_root / "pyproject.toml").is_file():
        extras_root = resource_root
        source_root = resource_root / "src"
    else:
        extras_root = resource_root / "source-kit-root"
        source_root = Path(__file__).resolve().parent
    if not (extras_root / "pyproject.toml").is_file() or not source_root.is_dir():
        raise SourceKitError("Source Kit editable source assets are not installed")
    return resource_root, extras_root, source_root


def _read_source_file(path: Path, *, maximum_bytes: int = MAX_MEMBER_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SourceKitError("Source Kit allowlist contains a non-regular file")
    file_stat = path.stat()
    if file_stat.st_size < 0 or file_stat.st_size > maximum_bytes:
        raise SourceKitError("Source Kit member exceeds the allowed size")
    body = path.read_bytes()
    if len(body) != file_stat.st_size:
        raise SourceKitError("Source Kit source file changed while being read")
    return body


def _add_tree(
    files: dict[str, bytes],
    *,
    source: Path,
    output_prefix: str,
    allowed_suffixes: frozenset[str],
) -> None:
    if source.is_symlink() or not source.is_dir():
        raise SourceKitError(f"Source Kit required directory is missing: {output_prefix}")
    found = 0
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if IGNORED_DIRECTORY_NAMES.intersection(relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise SourceKitError("Source Kit source tree contains a symbolic link")
        if not path.is_file():
            continue
        if path.suffix not in allowed_suffixes:
            raise SourceKitError(
                f"Source Kit source tree contains an unexpected file type: {output_prefix}"
            )
        name = f"{output_prefix}/{relative.as_posix()}"
        if not _safe_member_name(name) or name in files:
            raise SourceKitError("Source Kit source tree contains an unsafe or duplicate path")
        files[name] = _read_source_file(path)
        found += 1
    if found == 0:
        raise SourceKitError(f"Source Kit required directory is empty: {output_prefix}")


def _guide() -> bytes:
    return (
        "# OPC Finance Box · Fork-ready Source Kit\n\n"
        "此归档包含可编辑源码、测试、Pack、三类 Box 样板、文档和部署模板；"
        "不包含 Git 历史、运行数据、私有证据、构建产物或凭据。\n\n"
        "在归档外验证并安全初始化到一个不存在的绝对目录：\n\n"
        "```bash\n"
        "opc-finance-box source-kit-verify opc-finance-box-source-kit-*.zip\n"
        "opc-finance-box source-kit-unpack opc-finance-box-source-kit-*.zip /absolute/new/fork --actor RECIPIENT\n"
        "opc-finance-box source-kit-unpack-verify /absolute/new/fork\n"
        "```\n\n"
        "离线重验只适用于尚未修改、尚未 git init、尚未安装依赖的原始工作区。"
        "进入新目录后再运行：\n\n"
        "```bash\n"
        "python3 -m venv .venv\n"
        "source .venv/bin/activate\n"
        "python -m pip install -e .\n"
        "python -m unittest discover -s tests\n"
        "python -m src.cli pack-audit\n"
        "python -m src.cli eval evals/core_packs.json\n"
        "python -m src.cli starter-init /absolute/new/my-box --profile dtc --country NL --actor founder\n"
        "python -m src.cli starter-compose /absolute/new/global-box --profile dtc --entity CN=cn_ops --entity NL=nl_sales --reporting-currency EUR --actor founder\n"
        "python -m src.cli trial-init /absolute/new/my-trial --profile dtc --country NL --actor founder\n"
        "python -m src.cli trial-verify /absolute/new/my-trial\n"
        "python -m src.cli trial-onboarding /absolute/new/my-trial\n"
        "python -m src.cli connector-preflight /absolute/new/my-box/box.json\n"
        "python -m src.cli connector-access-request-init /absolute/new/my-box/box.json --pack connector.shopify --entity ENTITY_ID --output /absolute/private/shopify-access.json\n"
        "python -m src.cli connector-access-request-verify /absolute/new/my-box/box.json /absolute/private/shopify-access.json\n"
        "# Run only after operator approval; authorized probes must persist a private receipt.\n"
        "python -m src.cli connector-access-probe /absolute/new/my-box/box.json /absolute/private/shopify-access.json --allow-network --output /absolute/private/shopify-access-receipt.json\n"
        "python -m src.cli connector-access-receipt-verify /absolute/new/my-box/box.json /absolute/private/shopify-access.json /absolute/private/shopify-access-receipt.json\n"
        "python -m src.cli trial-run /absolute/new/my-trial\n"
        "python run.py\n"
        "```\n\n"
        "此包没有依赖 lock；fork 后应按自己的 Python/供应链策略生成并审计 lock。"
        "测试通过不等于 Pack stable、真实财务复核或外部申报授权。\n"
    ).encode("utf-8")


def _collect_source_files(project_root: str | Path | None = None) -> dict[str, bytes]:
    resource_root, extras_root, source_root = _source_layout(project_root)
    files: dict[str, bytes] = {}
    for name in TOP_LEVEL_FILES:
        files[name] = _read_source_file(extras_root / name)
    for output_prefix, suffixes in TREE_RULES:
        if output_prefix == "src":
            source = source_root
        elif output_prefix in {"tests", "scripts"}:
            source = extras_root / output_prefix
        else:
            source = resource_root / output_prefix
        _add_tree(
            files,
            source=source,
            output_prefix=output_prefix,
            allowed_suffixes=suffixes,
        )
    deployment_root = resource_root / "deployment"
    for name in REQUIRED_ASSETS:
        files[f"deployment/{name}"] = _read_source_file(deployment_root / name)
    for name in DATA_FILES:
        files[f"data/{name}"] = _read_source_file(resource_root / "data" / name)
    for name in WORKFLOW_FILES:
        files[f".github/workflows/{name}"] = _read_source_file(
            extras_root / ".github" / "workflows" / name,
        )
    files[GUIDE_NAME] = _guide()
    return files


def _category_counts(files: dict[str, bytes]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in files:
        category = name.split("/", 1)[0] if "/" in name else "root"
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    records = [
        {"path": name, "size_bytes": len(body), "sha256": _sha256(body)}
        for name, body in sorted(files.items())
    ]
    return {
        "schema_version": 1,
        "product": "opc-finance-box",
        "source_kit_schema_version": 1,
        "content_fingerprint": _sha256(_canonical_bytes(records)),
        "file_count": len(records),
        "category_counts": _category_counts(files),
        "files": records,
        "fork_ready_source_tree": True,
        "editable_source_included": True,
        "tests_included": True,
        "demo_data_included": True,
        "dependency_lock_included": False,
        "vendored_dependencies_included": False,
        "git_history_included": False,
        "runtime_data_included": False,
        "private_evidence_included": False,
        "build_artifacts_included": False,
        "credentials_included": False,
        "real_financial_source_files_included": False,
        "external_actions_performed": False,
    }


def build_source_kit_bundle(
    project_root: str | Path | None = None,
) -> tuple[bytes, str, dict[str, Any]]:
    """Build one deterministic, allowlisted, fork-ready source archive."""
    files = _collect_source_files(project_root)
    manifest = _manifest(files)
    files[MANIFEST_NAME] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9,
    ) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
    body = output.getvalue()
    if len(body) > MAX_ARCHIVE_BYTES:
        raise SourceKitError("Source Kit archive exceeds the allowed size")
    return (
        body,
        f"opc-finance-box-source-kit-{manifest['content_fingerprint'][:12]}.zip",
        manifest,
    )


def write_source_kit_bundle(
    output: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    body, suggested_filename, manifest = build_source_kit_bundle(project_root)
    requested = Path(output).expanduser()
    destination = requested.parent.resolve() / requested.name
    if destination.suffix.lower() != ".zip":
        raise SourceKitError("Source Kit output must use a .zip suffix")
    if destination.parent.is_symlink() or not destination.parent.is_dir():
        raise SourceKitError("Source Kit output parent must be an existing real directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise SourceKitError("Source Kit output already exists; refusing to overwrite") from exc
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "schema_version": 1,
        "written": True,
        "suggested_filename": suggested_filename,
        "size_bytes": len(body),
        "sha256": _sha256(body),
        "content_fingerprint": manifest["content_fingerprint"],
        "file_count": manifest["file_count"],
        "fork_ready_source_tree": True,
        "tests_included": True,
        "git_history_included": False,
        "vendored_dependencies_included": False,
        "runtime_data_included": False,
        "private_evidence_included": False,
        "build_artifacts_included": False,
        "credentials_included": False,
        "external_actions_performed": False,
        "output_path_returned": False,
    }


def _read_archive(path: str | Path) -> bytes:
    requested = Path(path).expanduser()
    if requested.suffix.lower() != ".zip" or requested.is_symlink():
        raise SourceKitError("Source Kit input must be an existing regular .zip file")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(requested, flags)
    except OSError as exc:
        raise SourceKitError("Source Kit input must be an existing regular .zip file") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or not 0 < file_stat.st_size <= MAX_ARCHIVE_BYTES:
            raise SourceKitError("Source Kit archive size or type is invalid")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            body = stream.read(MAX_ARCHIVE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(body) != file_stat.st_size:
        raise SourceKitError("Source Kit archive changed while being read")
    return body


def _archive_members(
    body: bytes,
    archive: zipfile.ZipFile,
) -> dict[str, tuple[zipfile.ZipInfo, bytes]]:
    infos = archive.infolist()
    if (
        not infos
        or len(infos) > MAX_MEMBER_COUNT
        or len(body) < 22
        or body[-22:-18] != b"PK\x05\x06"
        or body[-2:] != b"\x00\x00"
        or archive.comment
        or min(info.header_offset for info in infos) != 0
    ):
        raise SourceKitError("Source Kit ZIP envelope is not canonical")
    members: dict[str, tuple[zipfile.ZipInfo, bytes]] = {}
    total = 0
    for info in infos:
        name = info.filename
        mode = info.external_attr >> 16
        if name in members or info.is_dir() or not _safe_member_name(name):
            raise SourceKitError("Source Kit contains an unsafe or duplicate member")
        if (
            info.flag_bits & ~0x800
            or info.extra
            or info.comment
            or info.compress_type != zipfile.ZIP_DEFLATED
            or info.create_system != 3
            or stat.S_IFMT(mode) != stat.S_IFREG
            or stat.S_IMODE(mode) != 0o644
            or info.date_time != (1980, 1, 1, 0, 0, 0)
            or info.file_size > MAX_MEMBER_BYTES
        ):
            raise SourceKitError("Source Kit member metadata is not canonical")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise SourceKitError("Source Kit content exceeds the allowed size")
        try:
            member_body = archive.read(info)
        except (RuntimeError, zipfile.BadZipFile) as exc:
            raise SourceKitError("Source Kit member could not be verified") from exc
        if len(member_body) != info.file_size:
            raise SourceKitError("Source Kit member size is invalid")
        members[name] = (info, member_body)
    return members


def _validate_manifest(
    manifest: dict[str, Any],
    members: dict[str, tuple[zipfile.ZipInfo, bytes]],
) -> None:
    if set(manifest) != MANIFEST_KEYS:
        raise SourceKitError("Source Kit manifest contract is invalid")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("product") != "opc-finance-box"
        or manifest.get("source_kit_schema_version") != 1
        or manifest.get("fork_ready_source_tree") is not True
        or manifest.get("editable_source_included") is not True
        or manifest.get("tests_included") is not True
        or manifest.get("demo_data_included") is not True
        or manifest.get("dependency_lock_included") is not False
        or manifest.get("vendored_dependencies_included") is not False
        or manifest.get("git_history_included") is not False
        or manifest.get("runtime_data_included") is not False
        or manifest.get("private_evidence_included") is not False
        or manifest.get("build_artifacts_included") is not False
        or manifest.get("credentials_included") is not False
        or manifest.get("real_financial_source_files_included") is not False
        or manifest.get("external_actions_performed") is not False
    ):
        raise SourceKitError("Source Kit manifest safety boundary is invalid")
    fingerprint = manifest.get("content_fingerprint")
    records = manifest.get("files")
    file_count = manifest.get("file_count")
    category_counts = manifest.get("category_counts")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(records, list)
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count != len(records)
        or not isinstance(category_counts, dict)
    ):
        raise SourceKitError("Source Kit manifest counts or fingerprint are invalid")
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != FILE_RECORD_KEYS:
            raise SourceKitError("Source Kit manifest file record is invalid")
        name = record.get("path")
        size = record.get("size_bytes")
        digest = record.get("sha256")
        if (
            not isinstance(name, str)
            or not _safe_member_name(name)
            or name == MANIFEST_NAME
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_MEMBER_BYTES
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise SourceKitError("Source Kit manifest file record is invalid")
        paths.append(name)
        member = members.get(name)
        if member is None or len(member[1]) != size or _sha256(member[1]) != digest:
            raise SourceKitError("Source Kit member does not match its manifest")
    if (
        paths != sorted(paths)
        or len(paths) != len(set(paths))
        or set(paths) | {MANIFEST_NAME} != set(members)
        or _sha256(_canonical_bytes(records)) != fingerprint
    ):
        raise SourceKitError("Source Kit manifest does not match the archive")
    observed_counts = _category_counts({name: b"" for name in paths})
    if category_counts != observed_counts:
        raise SourceKitError("Source Kit category counts are invalid")
    required = {
        *TOP_LEVEL_FILES,
        GUIDE_NAME,
        "src/cli.py",
        "src/server.py",
        "src/source_kit_unpack.py",
        "src/starter_workspace.py",
        "src/trial_workspace.py",
        "tests/test_box_builder.py",
        "tests/test_source_kit_unpack.py",
        "tests/test_starter_workspace.py",
        "tests/test_trial_workspace.py",
        "packs/core/finance/manifest.json",
        "packs/industries/game_studio/manifest.json",
        "packs/industries/commerce/manifest.json",
        "examples/boxes/global_game_studio.json",
        "examples/boxes/cn_dtc_shopify_stripe_store.json",
        "examples/boxes/cn_marketplace_store.json",
        "examples/boxes/us_marketplace_amazon_seller_c_corp.json",
        "examples/pipelines/amazon_seller_marketplace_close_fixture.json",
        "deployment/Dockerfile",
        "public/index.html",
        "docs/OPC_FINANCE_BOX架构.md",
        "docs/AmazonMarketplace订单库存完整性设计.md",
        "docs/技术RC全矩阵审计.md",
        "docs/可Fork源码安全初始化.md",
        "docs/五分钟本地试用.md",
        ".github/workflows/tests.yml",
    }
    if not required.issubset(paths):
        raise SourceKitError("Source Kit is missing required fork-ready members")


def verify_source_kit_bundle(
    path: str | Path,
    *,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Verify archive integrity and reproduce its logical source tree locally."""
    body = _read_archive(path)
    verification, _, _ = _verify_source_kit_body(body, project_root=project_root)
    return verification


def _verify_source_kit_body(
    body: bytes,
    *,
    project_root: str | Path | None = None,
) -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    """Verify one already-read archive body and return those exact member bytes."""
    try:
        with zipfile.ZipFile(io.BytesIO(body), "r") as archive:
            members = _archive_members(body, archive)
    except zipfile.BadZipFile as exc:
        raise SourceKitError("Source Kit is not a valid ZIP archive") from exc
    manifest_member = members.get(MANIFEST_NAME)
    if manifest_member is None or len(manifest_member[1]) > MAX_MANIFEST_BYTES:
        raise SourceKitError("Source Kit manifest is missing or too large")
    try:
        manifest = json.loads(manifest_member[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceKitError("Source Kit manifest is not valid UTF-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise SourceKitError("Source Kit manifest must be a JSON object")
    _validate_manifest(manifest, members)
    try:
        expected_body, _, expected_manifest = build_source_kit_bundle(project_root)
        with zipfile.ZipFile(io.BytesIO(expected_body), "r") as expected_archive:
            expected_members = {
                info.filename: expected_archive.read(info)
                for info in expected_archive.infolist()
            }
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SourceKitError("Source Kit cannot be reproduced from installed source assets") from exc
    observed_members = {name: item[1] for name, item in members.items()}
    if expected_manifest != manifest or expected_members != observed_members:
        raise SourceKitError("Source Kit does not reproduce from installed source assets")
    verification = {
        "schema_version": 1,
        "valid": True,
        "sha256": _sha256(body),
        "size_bytes": len(body),
        "content_fingerprint": manifest["content_fingerprint"],
        "member_count": len(members),
        "manifest_file_count": manifest["file_count"],
        "category_counts": manifest["category_counts"],
        "reproducible_from_installed_source": True,
        "archive_bytes_match_current_builder": expected_body == body,
        "fork_ready_source_tree": True,
        "tests_included": True,
        "git_history_included": False,
        "vendored_dependencies_included": False,
        "runtime_data_included": False,
        "private_evidence_included": False,
        "credentials_included": False,
        "real_financial_source_files_included": False,
        "archive_extracted": False,
        "paths_returned": False,
        "external_actions_performed": False,
    }
    return verification, observed_members, manifest
