# Box workflow reference

```bash
python -m src.cli options
python -m src.cli box-starters
python -m src.cli starter-init /absolute/new/my-box --profile dtc --country NL --integration shopify_stripe --actor RECIPIENT
python -m src.cli starter-compose /absolute/new/global-box --profile dtc --entity CN=cn_ops --entity NL=nl_sales --entity-integration nl_sales=shopify_stripe --entity-integration cn_ops=xero --reporting-currency EUR --actor RECIPIENT
python -m src.cli handoff-unpack-verify /absolute/new/my-box
python -m src.cli handoff-bundle examples/box_specs/my-box.json --output outputs/my-box-handoff.zip
python -m src.cli handoff-verify outputs/my-box-handoff.zip
python -m src.cli handoff-receipt-verify outputs/my-box-handoff.zip outputs/my-box-handoff.browser-receipt.json
python -m src.cli handoff-unpack outputs/my-box-handoff.zip /absolute/new/handoff-workspace --actor HANDOFF_RECIPIENT
python -m src.cli handoff-unpack-verify /absolute/new/handoff-workspace
python -m src.cli source-kit-bundle --output outputs/opc-finance-box-source-kit.zip
python -m src.cli source-kit-verify outputs/opc-finance-box-source-kit.zip
python -m src.cli source-kit-unpack outputs/opc-finance-box-source-kit.zip /absolute/new/fork --actor RECIPIENT
python -m src.cli source-kit-unpack-verify /absolute/new/fork
python -m src.cli create examples/box_specs/my-box.json --output outputs/my-box.json
python -m src.cli validate outputs/my-box.json
python -m src.cli compile outputs/my-box.json --output outputs/my-box-build
python -m src.cli doctor outputs/my-box.json
python -m src.cli connector-shadow-status outputs/my-box.json --review-dir /absolute/private/connector-shadow-reviews --as-of YYYY-MM-DD
python -m src.cli production-readiness outputs/my-box.json --as-of YYYY-MM-DD
python -m src.cli activation-status outputs/my-box.json --as-of YYYY-MM-DD
python -m src.cli activation-init outputs/my-box.json /absolute/new/private-root --period YYYY-MM --facts-as-of YYYY-MM-DD --prepared-by PREPARER
python -m src.cli activation-workspace-verify outputs/my-box.json /absolute/new/private-root
python -m src.cli activation-runbook-status outputs/my-box.json /absolute/new/private-root
python -m src.cli activation-runbook-verify outputs/my-box.json /absolute/new/private-root
python -m src.cli activation-workspace-status outputs/my-box.json /absolute/new/private-root --as-of YYYY-MM-DD
python -m src.cli pack-audit
python -m src.cli eval evals/core_packs.json
python -m src.cli upgrade-check outputs/my-box.json outputs/previous-build/box.lock.json
```

The browser Builder uses the same installed Starter Catalog and connector binding
policy. Assign every external integration preset to the legal entity it actually
serves, confirm the live `Connector Pack -> entity_id` draft, then copy the
generated `starter-init` or `starter-compose --entity-integration` command if a
repeatable CLI handoff is preferred. Before a browser download is saved, require
the actual response Blob SHA-256 and length to match the fixed integrity headers.
Download or copy the resulting non-signing receipt and use the generated private
permission, `handoff-verify`, `handoff-receipt-verify`, `handoff-unpack`, and
`handoff-unpack-verify` commands. Set the downloaded ZIP and JSON to mode `0600`
first. Receipt verification reruns formal Pack reproducibility before binding the
safe fields; it cannot attest browser execution or identity. The browser does not
execute commands, extract archive members, persist credentials, or change the
active runtime.

After `activation-workspace-verify` passes, execute the private root's generated
`commands.json` in listed order. Replace every `REPLACE_WITH_...` value, but do
not change a fail-closed review decision until the named independent reviewer
has the real supporting evidence. The manifest is an operator template, not an
approval or an executable script.

Runbook progress is append-only operator continuity metadata. It must never be
used as a substitute for `activation-workspace-status` or a stage verifier.

Exit codes: `2` invalid request/config, `3` doctor blocker, `4` blocking upgrade change, `5` failed eval.

Compiled Bundle contains lock, setup checklist, data model, Agent contracts/prompts, Service/Connector catalogs, workflow/job plans, dashboard layout, jurisdiction rules, Pilot readiness/data-handoff/Shadow-registration/first-observation/consecutive-series schemas, upgrade policy, release gates, and README.

Read `docs/OPC_FINANCE_BOX架构.md` only when architecture details are needed. Read `docs/产品成熟度与路线图.md` before changing a Pack maturity label.
