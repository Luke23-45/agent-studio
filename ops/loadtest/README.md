# Load testing (P6-9)

k6 scripts asserting the §15 L1 budget: **p95 < 1.5 s end-to-end**,
error rate < 1%.

| Script | Purpose | Run |
|---|---|---|
| `load.js` | L1 ramp: 25 → 100 VUs peak, sustain, cool-down | every release to staging (manual) |
| `soak.js` | 60 min steady 20 VUs — degradation/leak detection | nightly on staging |

## Run locally

```bash
k6 run -e BASE_URL=http://localhost:8000 ops/loadtest/load.js
k6 run -e BASE_URL=https://staging.neryva.dev ops/loadtest/soak.js
```

## Run in CI

`.github/workflows/loadtest.yml` — `workflow_dispatch` against staging
(`BASE_URL` from the `STAGING_URL` secret; requires the `EVAL_OPENAI_API_KEY`
style provider key configured on staging). A failing threshold (p95 ≥ 1.5 s
or ≥ 1% errors) makes the workflow fail — the gate before promote.

## Notes

- The endpoint under test is the same one garak probes
  (`POST /api/v1/threads/completions`), so the numbers include the full
  guardrail pipeline (input pipeline, policy gate, PII layer, RAG, LLM,
  persistence) — the true end-to-end budget.
- Streamed responses (`stream: true`) are covered separately by
  `test_non_streaming_parity.py` + `test_streaming.py`; latency budgets
  there are asserted in-process.
- Alerts: `ops/monitoring/alert_rules.yml` watches the same p95 signal from
  Prometheus (`neryva_api_llm_latency_seconds`), so a CI pass plus green
  dashboards means the budget holds in production too.
