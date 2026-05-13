# Ultra-short: ISIS LSDB check (crpd*), artifacts

Goal: collect `show isis database` from all routers starting with `crpd` without flooding the LLM context.

## 1) Collect once (store full payload, return only a pointer)

Prompt:

- "Using JMCP: call get_router_list. Filter the list to only names starting with 'crpd'. Then call execute_junos_command_batch with router_names=<filtered>, command='show isis database', timeout=120, response_mode='artifact', artifact_label='isis-lsdb-crpd', barrier_sync=true, barrier_policy='proceed', preconnect_timeout=30, preconnect_retries=2, preconnect_backoff_seconds=1. Return only the summary + run_id."

## 2) Inspect safely (diff-first, then drill down)

Prompt (triage):

- "Using JMCP: call read_artifact with run_id=<run_id>, mode='diff'. If there are multiple output groups, summarize which routers differ from the baseline and why (ignore pure lifetime/counter churn)."

Prompt (failures):

- "Using JMCP: call read_artifact with run_id=<run_id>, mode='failures', max_output_chars=2000."

Prompt (drill down on a subset):

- "Using JMCP: call read_artifact with run_id=<run_id>, mode='full', router_names=['crpd1','crpd2']. Then extract a compact representation: for each router, list (lsp-id -> sequence-number)."

Notes:
- `response_mode='artifact'` is the knob that *stores* the full results (Redis/disk/dual) and returns only `run_id`.
- `mode='diff'` is usually the best first read for fleet-wide LSDB sanity checks.
