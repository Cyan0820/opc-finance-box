from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from .box_config import BoxConfigError, load_pack_manifest


class JurisdictionScaffoldError(ValueError):
    """Raised when a jurisdiction Pack scaffold would be unsafe or invalid."""


def _iso_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise JurisdictionScaffoldError(f"{field} must use YYYY-MM-DD") from exc


def scaffold_jurisdiction_pack(
    output_root: str | Path,
    *,
    slug: str,
    country_code: str,
    display_name: str,
    source_authority: str,
    source_title: str,
    source_url: str,
    verified_at: str,
    rules_effective_at: str,
) -> dict[str, Any]:
    """Create a source-backed design Pack without implying tax calculation or filing support."""
    normalized_slug = slug.strip().lower()
    normalized_code = country_code.strip().upper()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", normalized_slug):
        raise JurisdictionScaffoldError("slug must use lowercase letters, digits and underscores")
    if not re.fullmatch(r"[A-Z]{2}(?:-[A-Z0-9]{1,3})?", normalized_code):
        raise JurisdictionScaffoldError("country_code must use ISO-style uppercase code")
    if not display_name.strip() or not source_authority.strip() or not source_title.strip():
        raise JurisdictionScaffoldError("display_name and official source metadata are required")
    if not source_url.startswith("https://"):
        raise JurisdictionScaffoldError("source_url must be an official HTTPS URL")
    checked_at = _iso_date(verified_at, "verified_at")
    effective_at = _iso_date(rules_effective_at, "rules_effective_at")

    destination = Path(output_root) / normalized_slug
    if destination.exists():
        raise JurisdictionScaffoldError(f"destination already exists: {destination}")
    destination.mkdir(parents=True)
    source_id = f"{normalized_code.lower().replace('-', '_')}_official_initial"
    rule_id = f"{normalized_code.lower().replace('-', '.')}.tax.evidence.initial"
    manifest = {
        "id": f"jurisdiction.{normalized_slug}",
        "kind": "jurisdiction",
        "display_name": display_name.strip(),
        "version": "0.1.0",
        "status": "experimental",
        "jurisdiction": {
            "code": normalized_code,
            "tax_readiness": "design",
            "rules_effective_at": effective_at,
            "authority_scope": "初始证据与登记设计；未实现税额计算、申报工作底稿或外部提交",
        },
        "rules_file": "rules.json",
        "capabilities": [
            f"tax.{normalized_slug}.registration_profile",
            f"tax.{normalized_slug}.evidence_checklist",
        ],
        "requires": ["core.finance"],
        "conflicts": [],
        "manual_review_gates": [
            "tax_registration_confirmation",
            "tax_advisor_review",
            "tax_filing_release",
        ],
    }
    rules = {
        "schema_version": 1,
        "jurisdiction": normalized_code,
        "verified_at": checked_at,
        "review_policy": {
            "max_age_days": 180,
            "warning_days_before_expiry": 30,
            "expiry_effect": "block_external_filing_and_calendar_release",
            "reverification_triggers": [
                "authority_source_change",
                "rule_effective_date_change",
                "pack_upgrade",
                "entity_applicability_change",
                "tax_registration_change",
            ],
        },
        "applicability_review_policy": {
            "max_age_days": 365,
            "warning_days_before_expiry": 30,
            "expiry_effect": "block_calendar_and_external_filing_release",
            "reverification_triggers": [
                "pack_upgrade",
                "entity_applicability_change",
                "tax_registration_change",
            ],
        },
        "scope_note": "初始官方来源索引；所有登记、税务处理、期限和申报结论均需当地专业人士复核。",
        "sources": [{
            "id": source_id,
            "authority": source_authority.strip(),
            "title": source_title.strip(),
            "url": source_url,
            "effective_from": effective_at,
            "status": f"official source checked {checked_at}",
        }],
        "rules": [{
            "id": rule_id,
            "summary": "保存主体税务登记、经营事实和官方来源，供当地税务专业人士确认后再扩展自动化。",
            "effective_from": effective_at,
            "automation_level": "evidence",
            "human_review_required": True,
            "source_ids": [source_id],
        }],
    }
    manifest_path = destination / "manifest.json"
    rules_path = destination / "rules.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rules_path.write_text(json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        loaded = load_pack_manifest(manifest_path)
    except BoxConfigError as exc:
        raise JurisdictionScaffoldError(f"generated Pack failed validation: {exc}") from exc
    return {
        "pack_id": loaded.pack_id,
        "destination": str(destination),
        "manifest": str(manifest_path),
        "rules": str(rules_path),
        "tax_readiness": loaded.jurisdiction["tax_readiness"] if loaded.jurisdiction else None,
        "next_steps": [
            "补充登记、税种和日历规则；每条规则引用官方来源。",
            "为每条规则建立确定性测试和人工复核门。",
            "完成专业复核前保持 experimental/design，不启用外部申报。",
        ],
    }
