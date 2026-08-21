---
name: add-opc-tax-pack
description: Create, source, implement, validate, and mature a versioned OPC Finance jurisdiction Pack for a new tax country or region. Use when Codex needs to add tax registration facts, official sources, candidate calendars, evidence checklists, deterministic workpapers, review gates, or country/region choices without inventing tax rules or enabling filing.
---

# Add OPC Tax Pack

Start at `experimental/design`. Do not generate rates, deadlines, applicability, or filing claims from general model memory.

## Procedure

1. Define the exact jurisdiction scope: country, state/province/city, registration type, tax type, entity type, fiscal year, and effective period.
2. Browse current official authority sources. Record authority, title, HTTPS URL, effective date, verification date, and status. Prefer legislation, authority guidance, official form instructions, and official filing calendars.
3. Scaffold the Pack with `opc-finance-box jurisdiction-init`; never overwrite an existing Pack.
4. Add structured rules where each rule references official source IDs, has an effective date, automation level, and `human_review_required=true`.
5. Express dates as supported schedules. Use `manual_configuration` when national rules do not determine local deadlines.
6. Implement deterministic Service handlers for registration profiles, evidence, calendars, or workpapers. Bind them in the registry and keep statutory `entity_id` scope.
7. Add normal, boundary, missing-fact, non-applicable, duplicate, expiry, wrong-entity, and review-gate tests.
8. Run validation, Pack audit, doctor, eval, and upgrade check.
9. Keep external filing disabled. Change maturity only after the criteria in [references/tax-pack-contract.md](references/tax-pack-contract.md) are satisfied.

## Non-negotiable boundaries

- A destination country, currency, payment processor field, or collected-tax amount is evidence, not a final tax conclusion.
- Missing registration or local facts block applicability; they are not false or zero.
- Workpapers must include entity, Pack version, rules verification date, official sources, blockers, reviewer role, and filing flags.
- `filing_assist` still requires authorized approval and actual submission receipt.
