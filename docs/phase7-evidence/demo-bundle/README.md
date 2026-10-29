# Demo bundle (offline, fake adapter)

Pre-recorded golden traces so the demo story survives a dead venue network
(runbook §7 fallback). All files are copies of committed goldens/policies.

| File | Source | Meaning |
|---|---|---|
| `pre-fix-trace.jsonl` + `pre-fix-state.json` | `traces/golden/timeout_retry-seed42.*` | Double charge (steps 3–4), verdict FAIL |
| `pre-fix-verdict.json` | `traces/golden/timeout_retry-seed42.verdict.json` | Pinned FAIL verdict |
| `post-fix-trace.jsonl` + `post-fix-state.json` | `traces/golden/timeout_retry-fixed-seed42.*` | Single charge, verdict PASS |
| `post-fix-verdict.json` | `traces/golden/timeout_retry-fixed-seed42.verdict.json` | Pinned PASS verdict |
| `fixed-policy.yaml` | `policies/fixed-timeout.yaml` | Canonical fix (status-read-then-same-key-retry) |

## Expected outputs

```text
$ agentcrash check --trace pre-fix-trace.jsonl --state pre-fix-state.json  # exit 1
[FAIL] Invariant exactly_once_mutation violated! (2 charges registered for invoice #9821 (ch_001, ch_002); offending charge steps [3, 4])
[PASS] Invariant task_completion satisfied (invoice #9821 status=PAID)
verdict=FAIL

$ agentcrash check --trace post-fix-trace.jsonl --state post-fix-state.json  # exit 0
[PASS] Invariant exactly_once_mutation satisfied (exactly 1 committed charge_card effect for invoice #9821 (ch_001); charge steps [3])
[PASS] Invariant task_completion satisfied (invoice #9821 status=PAID)
verdict=PASS

$ agentcrash replay --trace <pre-fix> --policy fixed-policy.yaml  # exit 0
verdict=PASS
fix: VERIFIED (exactly_once_mutation PASS, task PAID)
regression: 4/4 golden traces still PASS
```

(The `check` lines above are the observed golden outputs; the `replay` line is
the observed rehearsal shape with `replay_id=85ca5aa0453a94b2`.)
