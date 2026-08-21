# Tax Pack contract

Create a Pack:

```bash
opc-finance-box jurisdiction-init \
  --output-root packs/jurisdictions \
  --slug <slug> --country-code <CODE> --display-name <name> \
  --source-authority <authority> --source-title <title> \
  --source-url <official-https-url> \
  --verified-at YYYY-MM-DD --rules-effective-at YYYY-MM-DD
```

Maturity:

- `design`: registration/evidence contract only; no tax amount or filing output.
- `workpaper`: deterministic candidate workpapers with sources, blockers, and human review.
- `filing_assist`: limited to named registration/form/external workflow after professional validation; not unattended filing.

Never upgrade maturity based only on tests using invented data. Require representative shadow runs, professional signoff, form/version coverage, authorization, receipt handling, recovery, and documented exclusions.

Calendar schedules may use `days_after_date`, `months_after_date` or `annual_fixed_after_date` only when an official national rule and an explicit, evidence-backed anchor determine a candidate. Use `manual_configuration` when registration, entity facts, local authority, agent status or account-specific dates can change the result.
