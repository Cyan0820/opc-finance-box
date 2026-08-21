from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from typing import Any, Iterable

from .box_runtime import BoxRuntime
from .pilot_shadow_observation import (
    validate_pilot_shadow_observation_receipt,
    verify_pilot_shadow_observation,
)


MAX_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 50 * 1024 * 1024
MINIMUM_CONSECUTIVE_PERIODS = 2
MAXIMUM_PERIODS = 24
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ID24_PATTERN = re.compile(r"^[0-9a-f]{24}$")
PERIOD_PATTERN = re.compile(r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{1,127}$")
REFERENCE_PATTERN = re.compile(
    r"^(?:evidence|document|workpaper|registry|advisor|authority|audit|review)://"
    r"[^\s]{2,500}$"
)
SERIES_DECISIONS = {"approved-for-promotion-evidence", "needs-correction"}
OBSERVATION_DECISIONS = {"passed", "accepted-differences", "needs-correction"}


class PilotShadowSeriesError(ValueError):
    """Raised when consecutive Pilot Shadow evidence is incomplete or unsafe."""


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise PilotShadowSeriesError(
            "pilot Shadow series evidence must be JSON-serializable"
        ) from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _actor(value: Any, field: str) -> str:
    actor = str(value or "").strip()
    if not ACTOR_PATTERN.fullmatch(actor):
        raise PilotShadowSeriesError(
            f"{field} must be a 2-128 character stable actor identifier"
        )
    return actor


def _text(value: Any, field: str, *, minimum: int, maximum: int = 1000) -> str:
    text = str(value or "").strip()
    if not minimum <= len(text) <= maximum or any(
        ord(char) < 32 and char not in "\n\t" for char in text
    ):
        raise PilotShadowSeriesError(
            f"{field} must be {minimum}-{maximum} printable characters"
        )
    return text


def _references(value: Iterable[str], field: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise PilotShadowSeriesError(f"{field} must be a list")
    references = list(value)
    if not references or len(references) > 20 or len(references) != len(set(references)):
        raise PilotShadowSeriesError(f"{field} requires 1-20 unique references")
    if any(
        not isinstance(item, str) or not REFERENCE_PATTERN.fullmatch(item)
        for item in references
    ):
        raise PilotShadowSeriesError(
            f"{field} must contain bounded opaque evidence references"
        )
    return references


def _strict_fields(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise PilotShadowSeriesError(f"{label} fields do not match the strict contract")


def _read_private_json(
    path_value: str | Path,
    *,
    label: str,
    maximum_bytes: int = MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    path = Path(path_value)
    if path.is_symlink():
        raise PilotShadowSeriesError(f"{label} must not be a symbolic link")
    if not path.is_file():
        raise PilotShadowSeriesError(f"{label} does not exist")
    file_stat = path.stat()
    if file_stat.st_size > maximum_bytes:
        raise PilotShadowSeriesError(f"{label} exceeds the maximum size")
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise PilotShadowSeriesError(f"{label} must use private 0600 permissions")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PilotShadowSeriesError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PilotShadowSeriesError(f"{label} must be a JSON object")
    return value


def _write_private(path_value: str | Path, value: dict[str, Any]) -> Path:
    path = Path(path_value)
    if path.exists() or path.is_symlink():
        raise PilotShadowSeriesError(
            "pilot Shadow series output already exists; refusing to overwrite"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    if os.name != "nt":
        path.chmod(0o600)
    return path


def _write_private_bytes(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _private_archive_root(root_value: str | Path) -> Path:
    root = Path(root_value).expanduser()
    if not root.is_absolute():
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must be absolute"
        )
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must be an existing real directory"
        )
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must use mode 0700"
        )
    entries = list(root.iterdir())
    if len(entries) >= MAXIMUM_PERIODS:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive already contains 24 periods"
        )
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            raise PilotShadowSeriesError(
                "pilot Shadow period archive root may contain only period directories"
            )
        _period_number(entry.name)
        if os.name != "nt" and stat.S_IMODE(entry.stat().st_mode) != 0o700:
            raise PilotShadowSeriesError(
                "pilot Shadow archived period directories must use mode 0700"
            )
    return root


def _private_json_bytes(path_value: str | Path, *, label: str) -> bytes:
    path = Path(path_value)
    _read_private_json(path, label=label)
    return path.read_bytes()


def archive_pilot_shadow_period(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path,
    registration_json: str | Path,
    handoff_review: str | Path,
    pilot_readiness_review: str | Path,
    runs_root: str | Path,
    entity_report_paths: Iterable[str | Path],
    evidence_root: str | Path,
    *,
    portfolio_review_path: str | Path | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Verify and transactionally archive one period for a future series review."""
    reports = list(entity_report_paths)
    verified = verify_pilot_shadow_observation(
        runtime,
        reviewed_receipt_json,
        registration_json,
        handoff_review,
        pilot_readiness_review,
        runs_root,
        reports,
        portfolio_review_path=portfolio_review_path,
        as_of=as_of,
    )
    root = _private_archive_root(evidence_root)
    period = str(verified["period"])
    _period_number(period)
    destination = root / period
    if destination.exists() or destination.is_symlink():
        raise PilotShadowSeriesError(
            "pilot Shadow period archive already exists; refusing to overwrite"
        )

    expected_entities = sorted(runtime.entities.ids())
    report_bodies: dict[str, bytes] = {}
    for path_value in reports:
        path = Path(path_value)
        report = _read_private_json(path, label="entity Shadow Close report")
        entity_id = str(report.get("entity_id") or "")
        if entity_id not in expected_entities or entity_id in report_bodies:
            raise PilotShadowSeriesError(
                "pilot Shadow period archive reports must cover every entity exactly"
            )
        report_bodies[entity_id] = path.read_bytes()
    if sorted(report_bodies) != expected_entities:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive reports must cover every entity exactly"
        )

    multi_entity = len(expected_entities) > 1
    if multi_entity != (portfolio_review_path is not None):
        raise PilotShadowSeriesError(
            "pilot Shadow period archive portfolio scope does not match the Box"
        )
    sources = {
        "reviewed-observation.json": _private_json_bytes(
            reviewed_receipt_json, label="reviewed pilot Shadow observation",
        ),
        "shadow-run-registration.json": _private_json_bytes(
            registration_json, label="pilot Shadow Run registration",
        ),
        "data-handoff-review.json": _private_json_bytes(
            handoff_review, label="pilot data handoff review",
        ),
        "pilot-readiness-review.json": _private_json_bytes(
            pilot_readiness_review, label="pilot readiness review",
        ),
    }
    if portfolio_review_path is not None:
        sources["portfolio-review.json"] = _private_json_bytes(
            portfolio_review_path, label="reviewed portfolio Shadow manifest",
        )

    created = False
    try:
        destination.mkdir(mode=0o700)
        created = True
        report_root = destination / "entity-reports"
        report_root.mkdir(mode=0o700)
        if os.name != "nt":
            destination.chmod(0o700)
            report_root.chmod(0o700)
        for name, body in sources.items():
            _write_private_bytes(destination / name, body)
        for entity_id, body in report_bodies.items():
            _write_private_bytes(report_root / f"{entity_id}.json", body)

        archived = _source_paths(runtime, destination)
        reverified = verify_pilot_shadow_observation(
            runtime,
            archived["observation"],
            archived["registration"],
            archived["handoff"],
            archived["readiness"],
            runs_root,
            archived["reports"],
            portfolio_review_path=archived["portfolio"],
            as_of=as_of,
        )
        if reverified != verified:
            raise PilotShadowSeriesError(
                "archived pilot Shadow period does not reproduce source verification"
            )
    except Exception:
        if created and destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        raise

    return {
        "schema_version": 1,
        "archived": True,
        "archive_verified": True,
        "runtime_fingerprint": verified["runtime_fingerprint"],
        "period": period,
        "entity_count": len(expected_entities),
        "portfolio_archived": multi_entity,
        "private_file_count": len(sources) + len(report_bodies),
        "ready_for_next_shadow_period": verified["ready_for_next_shadow_period"],
        "source_paths_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "private_financial_evidence_copied": True,
        "statutory_books_modified": False,
        "posting_performed": False,
        "payment_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "external_actions_performed": False,
    }


def _period_number(period: str) -> int:
    if not PERIOD_PATTERN.fullmatch(period):
        raise PilotShadowSeriesError("period must use YYYY-MM")
    year, month = (int(item) for item in period.split("-"))
    return year * 12 + month - 1


def _source_paths(
    runtime: BoxRuntime,
    period_root: Path,
) -> dict[str, Any]:
    if period_root.is_symlink() or not period_root.is_dir():
        raise PilotShadowSeriesError(
            "each pilot Shadow period evidence entry must be a non-symbolic directory"
        )
    period = period_root.name
    _period_number(period)
    multi_entity = len(runtime.entities.ids()) > 1
    expected = {
        "reviewed-observation.json",
        "shadow-run-registration.json",
        "data-handoff-review.json",
        "pilot-readiness-review.json",
        "entity-reports",
    }
    if multi_entity:
        expected.add("portfolio-review.json")
    actual = {item.name for item in period_root.iterdir()}
    if actual != expected:
        raise PilotShadowSeriesError(
            f"pilot Shadow period {period} must contain only the exact evidence layout"
        )
    report_root = period_root / "entity-reports"
    if report_root.is_symlink() or not report_root.is_dir():
        raise PilotShadowSeriesError(
            f"pilot Shadow period {period} entity-reports must be a non-symbolic directory"
        )
    expected_reports = {f"{entity_id}.json" for entity_id in runtime.entities.ids()}
    actual_reports = {item.name for item in report_root.iterdir()}
    if actual_reports != expected_reports:
        raise PilotShadowSeriesError(
            f"pilot Shadow period {period} entity reports must cover every entity exactly"
        )
    reports = [
        report_root / f"{entity_id}.json"
        for entity_id in sorted(runtime.entities.ids())
    ]
    if any(path.is_symlink() or not path.is_file() for path in reports):
        raise PilotShadowSeriesError(
            f"pilot Shadow period {period} entity reports must be regular files"
        )
    return {
        "period": period,
        "observation": period_root / "reviewed-observation.json",
        "registration": period_root / "shadow-run-registration.json",
        "handoff": period_root / "data-handoff-review.json",
        "readiness": period_root / "pilot-readiness-review.json",
        "reports": reports,
        "portfolio": period_root / "portfolio-review.json" if multi_entity else None,
    }


def _period_roots(root_value: str | Path) -> list[Path]:
    root = Path(root_value)
    if root.is_symlink() or not root.is_dir():
        raise PilotShadowSeriesError(
            "pilot Shadow series evidence root must be a non-symbolic directory"
        )
    entries = list(root.iterdir())
    if (
        len(entries) < MINIMUM_CONSECUTIVE_PERIODS
        or len(entries) > MAXIMUM_PERIODS
        or any(item.is_symlink() or not item.is_dir() for item in entries)
    ):
        raise PilotShadowSeriesError(
            "pilot Shadow series requires 2-24 non-symbolic period directories"
        )
    periods = [item.name for item in entries]
    if any(not PERIOD_PATTERN.fullmatch(period) for period in periods):
        raise PilotShadowSeriesError(
            "pilot Shadow series evidence root may contain only YYYY-MM directories"
        )
    ordered = sorted(entries, key=lambda item: _period_number(item.name))
    numbers = [_period_number(item.name) for item in ordered]
    if any(current != previous + 1 for previous, current in zip(numbers, numbers[1:])):
        raise PilotShadowSeriesError(
            "pilot Shadow series periods must be consecutive calendar months"
        )
    return ordered


def _content_sha256(path: Path, label: str) -> str:
    value = _read_private_json(path, label=label)
    return _hash(value)


def _collect_period(
    runtime: BoxRuntime,
    period_root: Path,
    runs_root: str | Path,
    *,
    as_of: str | None,
) -> dict[str, Any]:
    paths = _source_paths(runtime, period_root)
    receipt = _read_private_json(
        paths["observation"],
        label=f"pilot Shadow {paths['period']} reviewed observation",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    integrity = validate_pilot_shadow_observation_receipt(
        runtime, receipt, require_review=True,
    )
    try:
        verified = verify_pilot_shadow_observation(
            runtime,
            paths["observation"],
            paths["registration"],
            paths["handoff"],
            paths["readiness"],
            runs_root,
            paths["reports"],
            portfolio_review_path=paths["portfolio"],
            as_of=as_of,
        )
    except (ValueError, OSError) as exc:
        raise PilotShadowSeriesError(
            f"pilot Shadow period {paths['period']} source verification failed: {exc}"
        ) from exc
    if verified["period"] != paths["period"] or receipt.get("period") != paths["period"]:
        raise PilotShadowSeriesError(
            "pilot Shadow evidence directory name must match its reviewed observation period"
        )
    review = receipt["review"]
    decision = str(review.get("decision") or "")
    if decision not in OBSERVATION_DECISIONS:
        raise PilotShadowSeriesError("pilot Shadow observation decision is invalid")
    principal_values = {
        integrity["registration_actor"],
        *integrity["entity_reviewers"],
        _actor(review.get("actor"), "observation reviewer"),
    }
    if integrity["portfolio_reviewer"]:
        principal_values.add(integrity["portfolio_reviewer"])
    content_hashes = {
        "observation": _hash(receipt),
        "registration": _content_sha256(
            paths["registration"], f"pilot Shadow {paths['period']} registration",
        ),
        "handoff": _content_sha256(
            paths["handoff"], f"pilot Shadow {paths['period']} handoff review",
        ),
        "readiness": _content_sha256(
            paths["readiness"], f"pilot Shadow {paths['period']} readiness review",
        ),
        "entity_reports": [{
            "entity_id": path.stem,
            "content_sha256": _content_sha256(
                path, f"pilot Shadow {paths['period']} entity report",
            ),
        } for path in paths["reports"]],
        "portfolio": (
            _content_sha256(
                paths["portfolio"], f"pilot Shadow {paths['period']} portfolio review",
            )
            if paths["portfolio"] else None
        ),
    }
    return {
        "period": paths["period"],
        "observation_receipt_fingerprint": integrity["receipt_fingerprint"],
        "observation_review_id": str(review.get("review_id") or ""),
        "observation_review_decision": decision,
        "observation_reviewer": _actor(review.get("actor"), "observation reviewer"),
        "source_bundle_fingerprint": _hash(content_hashes),
        "comparison_count": verified["comparison_count"],
        "matched_count": verified["matched_count"],
        "exception_count": verified["exception_count"],
        "system_defect_count": verified["system_defect_count"],
        "ready_for_next_shadow_period": verified["ready_for_next_shadow_period"],
        "separation_principals": sorted(principal_values),
    }


def _load_pilot_shadow_period_archive(
    runtime: BoxRuntime,
    evidence_root: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None,
    period_count: int | None = None,
) -> list[dict[str, Any]]:
    """Load a private one-to-twenty-four-period archive for internal binding."""
    root = Path(evidence_root).expanduser()
    if not root.is_absolute():
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must be absolute"
        )
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must be an existing real directory"
        )
    if os.name != "nt" and stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root must use mode 0700"
        )
    entries = list(root.iterdir())
    if not 1 <= len(entries) <= MAXIMUM_PERIODS:
        raise PilotShadowSeriesError(
            "pilot Shadow period archive requires 1-24 archived periods"
        )
    if any(item.is_symlink() or not item.is_dir() for item in entries):
        raise PilotShadowSeriesError(
            "pilot Shadow period archive root may contain only period directories"
        )
    ordered = sorted(entries, key=lambda item: _period_number(item.name))
    numbers = [_period_number(item.name) for item in ordered]
    if any(current != previous + 1 for previous, current in zip(numbers, numbers[1:])):
        raise PilotShadowSeriesError(
            "pilot Shadow period archive periods must be consecutive calendar months"
        )
    for period_root in ordered:
        if os.name != "nt" and stat.S_IMODE(period_root.stat().st_mode) != 0o700:
            raise PilotShadowSeriesError(
                "pilot Shadow archived period directories must use mode 0700"
            )
        paths = _source_paths(runtime, period_root)
        report_root = period_root / "entity-reports"
        if os.name != "nt" and stat.S_IMODE(report_root.stat().st_mode) != 0o700:
            raise PilotShadowSeriesError(
                "pilot Shadow archived entity report directories must use mode 0700"
            )
        private_files = [
            paths["observation"], paths["registration"], paths["handoff"],
            paths["readiness"], *paths["reports"],
        ]
        if paths["portfolio"] is not None:
            private_files.append(paths["portfolio"])
        if os.name != "nt" and any(
            stat.S_IMODE(path.stat().st_mode) != 0o600 for path in private_files
        ):
            raise PilotShadowSeriesError(
                "pilot Shadow archived evidence files must use mode 0600"
            )
    if period_count is not None:
        if not isinstance(period_count, int) or not 1 <= period_count <= len(ordered):
            raise PilotShadowSeriesError(
                "pilot Shadow archive prefix count is invalid"
            )
        ordered = ordered[:period_count]
    return [
        _collect_period(runtime, period_root, runs_root, as_of=as_of)
        for period_root in ordered
    ]


def inspect_pilot_shadow_period_archive(
    runtime: BoxRuntime,
    evidence_root: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Re-verify an archived period chain without returning private bindings."""
    periods = _load_pilot_shadow_period_archive(
        runtime, evidence_root, runs_root, as_of=as_of,
    )
    latest = periods[-1]
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "period_count": len(periods),
        "first_period": periods[0]["period"],
        "latest_period": latest["period"],
        "entity_count": len(runtime.entities.ids()),
        "archive_consecutive": True,
        "latest_ready_for_next_shadow_period": latest[
            "ready_for_next_shadow_period"
        ],
        "archive_capacity_remaining": MAXIMUM_PERIODS - len(periods),
        "ready_to_initialize_next_period": (
            latest["ready_for_next_shadow_period"]
            and len(periods) < MAXIMUM_PERIODS
        ),
        "source_paths_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "statutory_books_modified": False,
        "posting_performed": False,
        "payment_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "external_actions_performed": False,
    }


def _transitions(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for previous, current in zip(periods, periods[1:]):
        exception_delta = current["exception_count"] - previous["exception_count"]
        output.append({
            "from_period": previous["period"],
            "to_period": current["period"],
            "consecutive": _period_number(current["period"]) == (
                _period_number(previous["period"]) + 1
            ),
            "comparison_count_delta": (
                current["comparison_count"] - previous["comparison_count"]
            ),
            "matched_count_delta": current["matched_count"] - previous["matched_count"],
            "exception_count_delta": exception_delta,
            "system_defect_count_delta": (
                current["system_defect_count"] - previous["system_defect_count"]
            ),
            "exception_trend": (
                "improved" if exception_delta < 0 else
                "worsened" if exception_delta > 0 else "unchanged"
            ),
        })
    return output


def _receipt_fingerprint(receipt: dict[str, Any]) -> str:
    return _hash({
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_fingerprint", "review", "review_current"}
    })


def _review_fingerprint(review: dict[str, Any]) -> str:
    return _hash({
        key: value for key, value in review.items() if key != "review_fingerprint"
    })


def validate_pilot_shadow_series_receipt(
    runtime: BoxRuntime,
    receipt: dict[str, Any],
    *,
    require_review: bool = False,
) -> dict[str, Any]:
    expected = {
        "schema_version", "artifact_type", "runtime_fingerprint", "entity_ids",
        "minimum_consecutive_periods", "periods", "period_observations",
        "transitions", "series_result_candidate", "status",
        "source_artifacts_input_only", "raw_financial_values_persisted",
        "statutory_books_modified", "posting_performed", "payment_performed",
        "period_close_performed", "external_filing_performed",
        "external_actions_performed", "guardrail", "receipt_fingerprint",
        "review", "review_current",
    }
    _strict_fields(receipt, expected, "pilot Shadow series receipt")
    snapshot = runtime.snapshot()
    if receipt.get("schema_version") != 1 or receipt.get("artifact_type") != (
        "pilot_shadow_series_receipt"
    ):
        raise PilotShadowSeriesError("pilot Shadow series contract is invalid")
    if receipt.get("runtime_fingerprint") != snapshot["fingerprint"]:
        raise PilotShadowSeriesError("pilot Shadow series belongs to a different Box")
    entity_ids = sorted(runtime.entities.ids())
    if receipt.get("entity_ids") != entity_ids:
        raise PilotShadowSeriesError("pilot Shadow series entity scope is invalid")
    if receipt.get("minimum_consecutive_periods") != MINIMUM_CONSECUTIVE_PERIODS:
        raise PilotShadowSeriesError("pilot Shadow series minimum period control is invalid")
    periods = receipt.get("periods")
    observations = receipt.get("period_observations")
    if (
        not isinstance(periods, list)
        or not MINIMUM_CONSECUTIVE_PERIODS <= len(periods) <= MAXIMUM_PERIODS
        or periods != sorted(periods, key=_period_number)
        or len(set(periods)) != len(periods)
        or not isinstance(observations, list)
        or len(observations) != len(periods)
    ):
        raise PilotShadowSeriesError("pilot Shadow series period coverage is invalid")
    if any(
        _period_number(current) != _period_number(previous) + 1
        for previous, current in zip(periods, periods[1:])
    ):
        raise PilotShadowSeriesError("pilot Shadow series periods are not consecutive")
    observation_fields = {
        "period", "observation_receipt_fingerprint", "observation_review_id",
        "observation_review_decision", "observation_reviewer",
        "source_bundle_fingerprint", "comparison_count", "matched_count",
        "exception_count", "system_defect_count", "ready_for_next_shadow_period",
        "separation_principals",
    }
    principals: set[str] = set()
    for index, item in enumerate(observations):
        _strict_fields(item, observation_fields, "pilot Shadow period observation")
        if item.get("period") != periods[index]:
            raise PilotShadowSeriesError("pilot Shadow series observation order is invalid")
        for field in ("observation_receipt_fingerprint", "source_bundle_fingerprint"):
            if not HEX64_PATTERN.fullmatch(str(item.get(field) or "")):
                raise PilotShadowSeriesError(f"pilot Shadow series {field} is invalid")
        if not ID24_PATTERN.fullmatch(str(item.get("observation_review_id") or "")):
            raise PilotShadowSeriesError("pilot Shadow observation review id is invalid")
        if item.get("observation_review_decision") not in OBSERVATION_DECISIONS:
            raise PilotShadowSeriesError("pilot Shadow series observation decision is invalid")
        reviewer = _actor(item.get("observation_reviewer"), "observation reviewer")
        period_principals = item.get("separation_principals")
        if (
            not isinstance(period_principals, list)
            or not period_principals
            or period_principals != sorted(set(period_principals))
            or reviewer not in period_principals
        ):
            raise PilotShadowSeriesError("pilot Shadow series role evidence is invalid")
        for principal in period_principals:
            principals.add(_actor(principal, "period separation principal"))
        counts = [
            item.get(field) for field in (
                "comparison_count", "matched_count", "exception_count",
                "system_defect_count",
            )
        ]
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise PilotShadowSeriesError("pilot Shadow series counts are invalid")
        if counts[0] != counts[1] + counts[2] or counts[3] > counts[2]:
            raise PilotShadowSeriesError("pilot Shadow series counts are inconsistent")
        if not isinstance(item.get("ready_for_next_shadow_period"), bool):
            raise PilotShadowSeriesError("pilot Shadow period readiness is invalid")
        if index < len(observations) - 1 and not item["ready_for_next_shadow_period"]:
            raise PilotShadowSeriesError(
                "a blocked Shadow observation cannot be followed by another period"
            )
    expected_transitions = _transitions(observations)
    if receipt.get("transitions") != expected_transitions:
        raise PilotShadowSeriesError("pilot Shadow series transition summary is invalid")
    candidate = (
        "ready_for_promotion_evidence"
        if all(item["ready_for_next_shadow_period"] for item in observations)
        else "needs_correction"
    )
    if receipt.get("series_result_candidate") != candidate:
        raise PilotShadowSeriesError("pilot Shadow series result candidate is invalid")
    if receipt.get("status") != "ready_for_independent_review":
        raise PilotShadowSeriesError("pilot Shadow series status is invalid")
    if receipt.get("source_artifacts_input_only") is not True:
        raise PilotShadowSeriesError("pilot Shadow series sources must remain input-only")
    for field in (
        "raw_financial_values_persisted", "statutory_books_modified",
        "posting_performed", "payment_performed", "period_close_performed",
        "external_filing_performed", "external_actions_performed",
    ):
        if receipt.get(field) is not False:
            raise PilotShadowSeriesError(f"pilot Shadow series {field} must be false")
    _text(receipt.get("guardrail"), "pilot Shadow series guardrail", minimum=40)
    fingerprint = _receipt_fingerprint(receipt)
    if receipt.get("receipt_fingerprint") != fingerprint:
        raise PilotShadowSeriesError("pilot Shadow series fingerprint mismatch")
    review = receipt.get("review")
    current = receipt.get("review_current") is True
    if require_review and not current:
        raise PilotShadowSeriesError("pilot Shadow series is not independently reviewed")
    review_actor = None
    if current:
        review_fields = {
            "review_id", "receipt_fingerprint", "decision", "actor", "rationale",
            "evidence_references", "reviewed_at", "scope_note", "review_fingerprint",
        }
        _strict_fields(review, review_fields, "pilot Shadow series review")
        if review.get("receipt_fingerprint") != fingerprint:
            raise PilotShadowSeriesError("pilot Shadow series review binding is invalid")
        decision = review.get("decision")
        if decision not in SERIES_DECISIONS:
            raise PilotShadowSeriesError("pilot Shadow series review decision is invalid")
        review_actor = _actor(review.get("actor"), "pilot Shadow series reviewer")
        if review_actor in principals:
            raise PilotShadowSeriesError(
                "pilot Shadow series reviewer must differ from every period principal"
            )
        _text(review.get("rationale"), "pilot Shadow series review rationale", minimum=12)
        _references(review.get("evidence_references") or [], "series review evidence")
        _text(review.get("scope_note"), "pilot Shadow series review scope note", minimum=40)
        reviewed_at = str(review.get("reviewed_at") or "")
        try:
            parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PilotShadowSeriesError("pilot Shadow series reviewed_at is invalid") from exc
        if parsed.tzinfo is None:
            raise PilotShadowSeriesError("pilot Shadow series reviewed_at requires timezone")
        expected_id = _hash({
            "receipt_fingerprint": fingerprint,
            "actor": review_actor,
            "reviewed_at": reviewed_at,
        })[:24]
        if review.get("review_id") != expected_id:
            raise PilotShadowSeriesError("pilot Shadow series review id is invalid")
        if review.get("review_fingerprint") != _review_fingerprint(review):
            raise PilotShadowSeriesError("pilot Shadow series review fingerprint is invalid")
        if decision == "approved-for-promotion-evidence" and candidate != (
            "ready_for_promotion_evidence"
        ):
            raise PilotShadowSeriesError(
                "only a complete consecutive series can be approved for promotion evidence"
            )
        if decision == "needs-correction" and candidate != "needs_correction":
            raise PilotShadowSeriesError(
                "needs-correction review requires a blocked series candidate"
            )
    elif review is not None:
        raise PilotShadowSeriesError("non-current pilot Shadow series review cannot be attached")
    return {
        "valid": True,
        "receipt_fingerprint": fingerprint,
        "period_count": len(periods),
        "first_period": periods[0],
        "latest_period": periods[-1],
        "comparison_count": sum(item["comparison_count"] for item in observations),
        "matched_count": sum(item["matched_count"] for item in observations),
        "exception_count": sum(item["exception_count"] for item in observations),
        "system_defect_count": sum(item["system_defect_count"] for item in observations),
        "candidate": candidate,
        "period_principals": sorted(principals),
        "review_actor": review_actor,
        "review_current": current,
    }


def assemble_pilot_shadow_series(
    runtime: BoxRuntime,
    evidence_root: str | Path,
    runs_root: str | Path,
    output: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    period_observations = [
        _collect_period(runtime, period_root, runs_root, as_of=as_of)
        for period_root in _period_roots(evidence_root)
    ]
    receipt = {
        "schema_version": 1,
        "artifact_type": "pilot_shadow_series_receipt",
        "runtime_fingerprint": runtime.snapshot()["fingerprint"],
        "entity_ids": sorted(runtime.entities.ids()),
        "minimum_consecutive_periods": MINIMUM_CONSECUTIVE_PERIODS,
        "periods": [item["period"] for item in period_observations],
        "period_observations": period_observations,
        "transitions": _transitions(period_observations),
        "series_result_candidate": (
            "ready_for_promotion_evidence"
            if all(item["ready_for_next_shadow_period"] for item in period_observations)
            else "needs_correction"
        ),
        "status": "ready_for_independent_review",
        "source_artifacts_input_only": True,
        "raw_financial_values_persisted": False,
        "statutory_books_modified": False,
        "posting_performed": False,
        "payment_performed": False,
        "period_close_performed": False,
        "external_filing_performed": False,
        "external_actions_performed": False,
        "guardrail": (
            "This private receipt proves consecutive reviewed Pilot Shadow observations only. "
            "It permits preparation of stable-promotion evidence after independent review, "
            "but does not promote a Pack or authorize posting, payment, close or filing."
        ),
        "review": None,
        "review_current": False,
    }
    receipt["receipt_fingerprint"] = _receipt_fingerprint(receipt)
    integrity = validate_pilot_shadow_series_receipt(runtime, receipt)
    destination = _write_private(output, receipt)
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "period_count": integrity["period_count"],
        "first_period": integrity["first_period"],
        "latest_period": integrity["latest_period"],
        "comparison_count": integrity["comparison_count"],
        "exception_count": integrity["exception_count"],
        "series_result_candidate": integrity["candidate"],
        "review_current": False,
        "output_written": destination.is_file(),
        "source_paths_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "raw_financial_values_returned": False,
        "external_actions_performed": False,
    }


def review_pilot_shadow_series(
    runtime: BoxRuntime,
    receipt_json: str | Path,
    output: str | Path,
    *,
    decision: str,
    actor: str,
    rationale: str,
    evidence_references: Iterable[str],
) -> dict[str, Any]:
    receipt = _read_private_json(
        receipt_json, label="pilot Shadow series receipt", maximum_bytes=MAX_RECEIPT_BYTES,
    )
    integrity = validate_pilot_shadow_series_receipt(runtime, receipt)
    if receipt.get("review_current") is True:
        raise PilotShadowSeriesError(
            "pilot Shadow series is already reviewed; assemble a new receipt"
        )
    if decision not in SERIES_DECISIONS:
        raise PilotShadowSeriesError(
            "decision must be approved-for-promotion-evidence or needs-correction"
        )
    reviewer = _actor(actor, "pilot Shadow series reviewer")
    if reviewer in set(integrity["period_principals"]):
        raise PilotShadowSeriesError(
            "pilot Shadow series reviewer must differ from every period principal"
        )
    if decision == "approved-for-promotion-evidence" and integrity["candidate"] != (
        "ready_for_promotion_evidence"
    ):
        raise PilotShadowSeriesError(
            "only a complete consecutive series can be approved for promotion evidence"
        )
    if decision == "needs-correction" and integrity["candidate"] != "needs_correction":
        raise PilotShadowSeriesError(
            "needs-correction review requires a blocked series candidate"
        )
    reviewed_at = datetime.now().astimezone().isoformat()
    review_id = _hash({
        "receipt_fingerprint": integrity["receipt_fingerprint"],
        "actor": reviewer,
        "reviewed_at": reviewed_at,
    })[:24]
    review = {
        "review_id": review_id,
        "receipt_fingerprint": integrity["receipt_fingerprint"],
        "decision": decision,
        "actor": reviewer,
        "rationale": _text(
            rationale, "pilot Shadow series review rationale", minimum=12,
        ),
        "evidence_references": _references(
            evidence_references, "pilot Shadow series review evidence",
        ),
        "reviewed_at": reviewed_at,
        "scope_note": (
            "This review applies only to the exact consecutive period evidence fingerprints "
            "in this receipt. It permits evidence preparation but grants no financial authority."
        ),
    }
    review["review_fingerprint"] = _review_fingerprint(review)
    reviewed = dict(receipt)
    reviewed["review"] = review
    reviewed["review_current"] = True
    validate_pilot_shadow_series_receipt(runtime, reviewed, require_review=True)
    destination = _write_private(output, reviewed)
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": reviewed["runtime_fingerprint"],
        "period_count": integrity["period_count"],
        "first_period": integrity["first_period"],
        "latest_period": integrity["latest_period"],
        "decision": decision,
        "review_id": review_id,
        "review_current": True,
        "eligible_to_prepare_stable_promotion_evidence": (
            decision == "approved-for-promotion-evidence"
        ),
        "ready_for_stable_promotion": False,
        "output_written": destination.is_file(),
        "source_paths_returned": False,
        "actors_returned": False,
        "raw_financial_values_returned": False,
        "external_actions_performed": False,
    }


def _verify_current_pilot_shadow_series(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path,
    evidence_root: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    receipt = _read_private_json(
        reviewed_receipt_json,
        label="reviewed pilot Shadow series receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    integrity = validate_pilot_shadow_series_receipt(
        runtime, receipt, require_review=True,
    )
    period_roots = _period_roots(evidence_root)
    current = [
        _collect_period(runtime, period_root, runs_root, as_of=as_of)
        for period_root in period_roots
    ]
    if current != receipt["period_observations"]:
        raise PilotShadowSeriesError(
            "source period evidence no longer matches the reviewed pilot Shadow series"
        )
    return receipt, integrity, current, period_roots


def _safe_pilot_shadow_series_verification(
    receipt: dict[str, Any],
    integrity: dict[str, Any],
    current: list[dict[str, Any]],
) -> dict[str, Any]:
    decision = receipt["review"]["decision"]
    eligible = decision == "approved-for-promotion-evidence"
    return {
        "schema_version": 1,
        "valid": True,
        "runtime_fingerprint": receipt["runtime_fingerprint"],
        "period_count": integrity["period_count"],
        "first_period": integrity["first_period"],
        "latest_period": integrity["latest_period"],
        "comparison_count": integrity["comparison_count"],
        "matched_count": integrity["matched_count"],
        "exception_count": integrity["exception_count"],
        "system_defect_count": integrity["system_defect_count"],
        "decision": decision,
        "review_id": receipt["review"]["review_id"],
        "review_current": True,
        "consecutive_periods_verified": True,
        "eligible_to_prepare_stable_promotion_evidence": eligible,
        "ready_for_next_shadow_period": current[-1]["ready_for_next_shadow_period"],
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "source_paths_returned": False,
        "source_attempt_ids_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }


def verify_pilot_shadow_series(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path,
    evidence_root: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    receipt, integrity, current, _ = _verify_current_pilot_shadow_series(
        runtime,
        reviewed_receipt_json,
        evidence_root,
        runs_root,
        as_of=as_of,
    )
    return _safe_pilot_shadow_series_verification(receipt, integrity, current)


def verify_pilot_shadow_series_for_promotion(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path,
    evidence_root: str | Path,
    runs_root: str | Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Reverify a reviewed series and expose private binding data to promotion code only.

    Callers must never persist or return the private binding fields. They exist so a
    stable-promotion assessment can prove that its raw reports and portfolio manifests
    are the exact artifacts covered by the independently reviewed consecutive series.
    """
    receipt, integrity, current, period_roots = _verify_current_pilot_shadow_series(
        runtime,
        reviewed_receipt_json,
        evidence_root,
        runs_root,
        as_of=as_of,
    )
    safe = _safe_pilot_shadow_series_verification(receipt, integrity, current)
    report_bindings: list[dict[str, str]] = []
    portfolio_bindings: list[dict[str, str]] = []
    for period_root in period_roots:
        paths = _source_paths(runtime, period_root)
        for report_path in paths["reports"]:
            report_bindings.append({
                "entity_id": report_path.stem,
                "period": paths["period"],
                "content_sha256": _content_sha256(
                    report_path,
                    f"pilot Shadow {paths['period']} entity report",
                ),
            })
        if paths["portfolio"] is not None:
            portfolio_bindings.append({
                "period": paths["period"],
                "content_sha256": _content_sha256(
                    paths["portfolio"],
                    f"pilot Shadow {paths['period']} portfolio review",
                ),
            })
    return {
        **safe,
        "periods": list(receipt["periods"]),
        "entity_ids": list(receipt["entity_ids"]),
        "series_review_actor": integrity["review_actor"],
        "period_principals": list(integrity["period_principals"]),
        "report_content_bindings": report_bindings,
        "portfolio_content_bindings": portfolio_bindings,
    }


def build_pilot_shadow_series_status(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path | None = None,
    evidence_root: str | Path | None = None,
    runs_root: str | Path | None = None,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    configured = reviewed_receipt_json is not None
    base = {
        "schema_version": 1,
        "configured": configured,
        "evidence_root_configured": evidence_root is not None,
        "pipeline_ledger_configured": runs_root is not None,
        "valid": False,
        "status": "missing" if not configured else "invalid",
        "eligible_to_prepare_stable_promotion_evidence": False,
        "ready_for_stable_promotion": False,
        "ready_for_statutory_release": False,
        "ready_for_external_filing": False,
        "paths_returned": False,
        "source_attempt_ids_returned": False,
        "source_artifact_hashes_returned": False,
        "actors_returned": False,
        "review_rationales_returned": False,
        "evidence_references_returned": False,
        "raw_financial_values_returned": False,
        "posting_authorized": False,
        "payment_authorized": False,
        "period_close_authorized": False,
        "external_filing_authorized": False,
        "external_actions_performed": False,
    }
    if not configured:
        return base
    if evidence_root is None or runs_root is None:
        base["error_sha256"] = _hash(
            "reviewed pilot Shadow series requires evidence root and pipeline ledger"
        )
        return base
    try:
        verified = verify_pilot_shadow_series(
            runtime, reviewed_receipt_json, evidence_root, runs_root, as_of=as_of,
        )
    except (ValueError, OSError) as exc:
        base["error_sha256"] = _hash(str(exc))
        return base
    return {
        **base,
        "valid": True,
        "status": "current",
        "runtime_fingerprint": verified["runtime_fingerprint"],
        "period_count": verified["period_count"],
        "first_period": verified["first_period"],
        "latest_period": verified["latest_period"],
        "comparison_count": verified["comparison_count"],
        "matched_count": verified["matched_count"],
        "exception_count": verified["exception_count"],
        "system_defect_count": verified["system_defect_count"],
        "decision": verified["decision"],
        "review_id": verified["review_id"],
        "consecutive_periods_verified": True,
        "eligible_to_prepare_stable_promotion_evidence": verified[
            "eligible_to_prepare_stable_promotion_evidence"
        ],
    }


def build_pilot_shadow_series_workspace(
    runtime: BoxRuntime,
    reviewed_receipt_json: str | Path | None = None,
    evidence_root: str | Path | None = None,
    runs_root: str | Path | None = None,
    *,
    as_of: str | None = None,
) -> dict[str, Any]:
    status = build_pilot_shadow_series_status(
        runtime, reviewed_receipt_json, evidence_root, runs_root, as_of=as_of,
    )
    return {
        "schema_version": 1,
        "summary": {
            "activation_status": status["status"],
            "entity_count": len(runtime.entities.ids()),
            "period_count": status.get("period_count", 0),
            "first_period": status.get("first_period"),
            "latest_period": status.get("latest_period"),
            "comparison_count": status.get("comparison_count", 0),
            "matched_count": status.get("matched_count", 0),
            "exception_count": status.get("exception_count", 0),
            "system_defect_count": status.get("system_defect_count", 0),
            "decision": status.get("decision"),
            "consecutive_periods_verified": status.get(
                "consecutive_periods_verified", False,
            ),
            "eligible_to_prepare_stable_promotion_evidence": status[
                "eligible_to_prepare_stable_promotion_evidence"
            ],
            "ready_for_stable_promotion": False,
        },
        "control_boundary": {
            "private_mounts_server_configured_only": True,
            "paths_returned": False,
            "source_attempt_ids_returned": False,
            "source_artifact_hashes_returned": False,
            "actors_returned": False,
            "review_rationales_returned": False,
            "evidence_references_returned": False,
            "raw_financial_values_returned": False,
            "stable_promotion_performed": False,
            "posting_authorized": False,
            "payment_authorized": False,
            "period_close_authorized": False,
            "external_filing_authorized": False,
            "external_actions_performed": False,
        },
    }
