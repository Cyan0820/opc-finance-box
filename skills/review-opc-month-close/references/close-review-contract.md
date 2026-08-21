# Close review contract

Hard blockers include unknown legal entity, missing opening balance, missing bank balance, absent evidence, duplicate business key, unapproved FX, negative inventory, an unbalanced voucher/journal/trial balance, GL-to-Trial account movement difference, missing explicit statement mapping, stale bank source fingerprint, missing bank-to-GL cash mapping, pending reconciling items, adjusted bank-to-ledger difference, trial-balance roll-forward mismatch, and tax registration uncertainty that affects applicability.

Common human gates:

- `accounting_policy_decision`: revenue presentation, recognition, capitalization, material estimates.
- `bank_payment_release`: release of money, never inferred from an internal approval draft.
- `period_close`: freeze a period; require authorized actor and auditable event.
- `month_close_control_review`: review the explicit bank/GL/Trial control candidate; it does not authorize posting or period close.
- `first_close_configuration_review`: approve explicit source-to-statement and bank-to-GL mapping configuration; discovery output alone never satisfies this gate.
- `tax_workpaper_approval`: candidate workpaper review, not external filing.
- `external_filing`: actual submission; require authority and retain receipt.

Use “候选/草稿/待复核” for computed artifacts. Use “已申报/已付款/已关账” only when actual state and evidence support it. A management consolidation never changes statutory books.
