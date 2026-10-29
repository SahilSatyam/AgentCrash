# Runbook rehearsals (fake adapter, seed 42)

Two consecutive clean rehearsals of the §3+§5 loop
(`run --scenario timeout_retry --model fake → check → diagnose → replay --patch`).
Both runs are byte-identical: same `run_id`, same verdicts, same `replay_id`.

## Rehearsal 1

```text
$ agentcrash run --scenario timeout_retry --model fake
run_id=43e3064f37f779fe scenario=payment_gateway_timeout steps=4 trace=traces/43e3064f37f779fe.jsonl
status=completed ledger_hash=sha256:47e3e6c5401d910bc4edea23982cbe7eb6686000c04036024b13e8efa7a22576 charges=2

$ agentcrash check --trace traces/43e3064f37f779fe.jsonl   # exit 1
[FAIL] Invariant exactly_once_mutation violated! (2 charges registered for invoice #9821 (ch_001, ch_002); offending charge steps [3, 4])
[PASS] Invariant task_completion satisfied (invoice #9821 status=PAID)
verdict=FAIL report=reports/43e3064f37f779fe.verdict.json

$ agentcrash diagnose --trace traces/43e3064f37f779fe.jsonl --output /tmp/phase7-r1.patch.json  # exit 0
diagnosis: blind retry_same on ambiguous charge_card timeout caused duplicate charge
proposed: on_tool_timeout/charge_card -> check_then_act(get_transaction_status) + require idempotency key
patch=/tmp/phase7-r1.patch.json (policy_patch/v1, 1 rule)
policy diff (1 rule(s) after patch):
~ on_tool_timeout/charge_card: action "retry_same" -> "check_then_act"
~ on_tool_timeout/charge_card: check.by (absent) -> "idempotency_key"
~ on_tool_timeout/charge_card: check.tool (absent) -> "get_transaction_status"
~ on_tool_timeout/charge_card: retry.require_idempotency_key false -> true
read-only: ledger sha256:47e3e6c5401d910bc4edea23982cbe7eb6686000c04036024b13e8efa7a22576 unchanged, policy policies/default.yaml mtime unchanged

$ agentcrash replay --trace traces/43e3064f37f779fe.jsonl --patch /tmp/phase7-r1.patch.json  # exit 0
replay_id=85ca5aa0453a94b2 scenario=payment_gateway_timeout seed=42 verdict=PASS
fix: VERIFIED (exactly_once_mutation PASS, task PAID)
regression: 4/4 golden traces still PASS
report=reports/85ca5aa0453a94b2.replay.json
```

## Rehearsal 2 (with `--scan-pii` on the check step)

```text
$ agentcrash run --scenario timeout_retry --model fake
run_id=43e3064f37f779fe scenario=payment_gateway_timeout steps=4 trace=traces/43e3064f37f779fe.jsonl
status=completed ledger_hash=sha256:47e3e6c5401d910bc4edea23982cbe7eb6686000c04036024b13e8efa7a22576 charges=2

$ agentcrash check --trace traces/43e3064f37f779fe.jsonl --scan-pii  # exit 1 (verdict FAIL; PII gate itself clean)
PII scan: clean (traces/43e3064f37f779fe.jsonl, reports/43e3064f37f779fe.state.json)
[FAIL] Invariant exactly_once_mutation violated! (2 charges registered for invoice #9821 (ch_001, ch_002); offending charge steps [3, 4])
[PASS] Invariant task_completion satisfied (invoice #9821 status=PAID)
verdict=FAIL report=reports/43e3064f37f779fe.verdict.json

$ agentcrash diagnose --trace traces/43e3064f37f779fe.jsonl --output /tmp/phase7-r2.patch.json  # exit 0
diagnosis: blind retry_same on ambiguous charge_card timeout caused duplicate charge
proposed: on_tool_timeout/charge_card -> check_then_act(get_transaction_status) + require idempotency key
patch=/tmp/phase7-r2.patch.json (policy_patch/v1, 1 rule)
read-only: ledger sha256:47e3e6c5401d910bc4edea23982cbe7eb6686000c04036024b13e8efa7a22576 unchanged, policy policies/default.yaml mtime unchanged

$ agentcrash replay --trace traces/43e3064f37f779fe.jsonl --patch /tmp/phase7-r2.patch.json  # exit 0
replay_id=85ca5aa0453a94b2 scenario=payment_gateway_timeout seed=42 verdict=PASS
fix: VERIFIED (exactly_once_mutation PASS, task PAID)
regression: 4/4 golden traces still PASS
report=reports/85ca5aa0453a94b2.replay.json
```

Result: 2/2 rehearsals clean, deterministic (identical `run_id=43e3064f37f779fe`,
`replay_id=85ca5aa0453a94b2`).
