# Service Request Examples

这些请求均使用虚构数据，通过与 HTTP API 相同的校验边界执行：

```bash
python -m src.cli dispatch examples/boxes/cn_dtc_store.json \
  examples/service_requests/dtc_margin.json

python -m src.cli dispatch examples/boxes/cn_marketplace_store.json \
  examples/service_requests/marketplace_inventory.json

python -m src.cli dispatch examples/boxes/global_game_studio.json \
  examples/service_requests/game_management_consolidation.json

python -m src.cli dispatch examples/boxes/cn_dtc_stripe_store.json \
  examples/service_requests/stripe_payout_reconciliation.json
```

`marketplace_inventory.json` 故意保留 1 件差异，预期结果为 `ready=false`，且不会自动调整库存。`cn_vat_workpaper.json` 使用空事实，预期生成带 blocker 的候选底稿，不能被解释为零申报或已申报。`stripe_payout_reconciliation.json` 会得到高置信银行候选，但仍保持 `candidate_only`，不会自动核销或过账。
