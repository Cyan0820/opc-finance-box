---
name: review-opc-month-close
description: Review an OPC monthly finance close by legal entity, period, currency, evidence, reconciliation, cash risk, tax workpaper, and human approval state. Use when Codex needs to inspect close readiness, explain blockers, draft a founder briefing, or prepare approval questions without posting, closing, paying, or filing.
---

# Review OPC Month Close

Review current facts, not an old Agent snapshot. Start with scope and evidence before explaining results.

## Procedure

1. Identify the target period and legal entities. Use statutory scope for books/tax/bank; use management scope only for reporting overlays.
2. Verify data coverage and evidence lineage for settlement, orders, purchases, invoices, payroll, bank, opening balances, FX, and tax registrations.
3. Reconcile in this order:
   - Channel/order facts to settlement.
   - Settlement/receivables/payables to bank activity.
   - Purchases to acceptance, invoice, approval, and payment.
   - Commerce refunds to return authorization, warehouse receipt, disposition, and reviewed inventory action.
   - Cross-border import batches to SKU/warehouse, declared value, freight, insurance, customs duty, import tax, brokerage, and source evidence. Keep import tax outside the inventory landed-cost candidate unless an approved policy determines otherwise; never infer customs classification, duty rate, recoverability, or posting.
   - Inventory movement to costing when Commerce is enabled.
4. Review draft vouchers, general-ledger detail, trial balance, statements, close tasks, and period state. Balance each external journal per currency, reconcile GL movements to Trial Balance by account, and verify explicit statement mappings. Then bind every bank account/currency to an explicit GL cash account, verify the current bank source fingerprint and evidence-backed reconciling items, and compare adjusted balances. Treat equality as a control only; never infer transaction matching, completeness, posting or close.
5. Review tax workpapers by each entity's selected jurisdiction Pack and maturity.
6. Refresh cash forecast and industry metrics; never mix currencies or treat projections as cash.
7. Separate hard blockers, anomalies, executable tasks, and decisions requiring a human gate.
8. Draft a founder briefing using the output structure below.

Read [references/close-review-contract.md](references/close-review-contract.md) for review gates and wording.

## Shadow Close handoff

When the user is validating a real, anonymized company close, keep the human baseline separate from the Agent result. The repository CLI provides this local workflow:

1. `shadow-close-template BOX.json --output BASELINE.xlsx`
2. `shadow-close-compare BOX.json BASELINE.xlsx FINANCE.json --output REPORT.json`
3. An independent finance reviewer runs `shadow-close-review` against that exact report fingerprint.
4. Use `shadow-close-verify` in CI or handoff checks; it returns only scope, counts and fingerprints.

The compare/review artifacts contain financial values and must remain private local files. Do not paste them into chat, logs, issues, or the promotion ledger. Do not supply an independent review actor or decision on the human's behalf. If values, scope, source, tolerance, or report fingerprint changes, require a fresh comparison and review.

## Output

Return:

- Scope: period, entity IDs, functional/reporting currencies, data mode.
- Readiness: completed/total, hard blockers, unresolved exceptions, evidence gaps.
- Money: cash, receivables, payables, revenue/cost/profit by entity and currency.
- Decisions: question, recommendation, business impact, required role, review gate.
- Deliverables: candidate/draft/approved/posted/filed status and source reference.
- Next action: one highest-priority factual action; never present a blocked item as approvable.

Never claim an action occurred unless the persistent audit state and actual external receipt say so.
