# 14 — Runbook (Demo-Day + Operator Guide)

**Status:** Approved · **Owner:** Tech lead (demo captain) · **Milestone:** M0 Prototype
**Lens:** ADLC startup runbook — one page to prepare, one loop to demo, one table to
survive surprises. No day-wise timeline; readiness is gated by rehearsal count.

## 1. Service overview

| Item | Value |
|---|---|
| Service | AgentCrash prototype CLI (`agentcrash run / check / diagnose / replay`) |
| Runtime | Python 3.11+, WSL Ubuntu-24.04, repo at `/home/sahil/AgentCrash` |
| Model | Meta Llama via API adapter; offline fallback `--model fake` for rehearsals |
| Evidence | JSONL traces in `traces/` (one file per run, `latest.jsonl` symlinked) |
| Policy file | `policy/base.yaml` (+ diagnoser patches as `policy_patch.json`) |
| Scenario (v0) | `timeout_retry`: ambiguous timeout on `charge_card`, invoice #9821, $150 |
| Escalation | Tech lead (demo captain) → backend engineer (harness) → product lead (narrative) |
| Logs | Console output + `traces/*.jsonl` + `traces/run-manifest.log` |

## 2. Setup (do once per machine, rehearse twice before demo day)

```bash
# 1. Enter WSL and the repo
wsl -d Ubuntu-24.04 -- bash -lc 'cd /home/sahil/AgentCrash && pwd'

# 2. Environment (inside WSL)
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 3. Model credentials (live demo only; fakes need nothing)
export LLAMA_API_KEY="..." LLAMA_BASE_URL="https://api.llama.example/v1"

# 4. Smoke test — must print PASS lines and a trace file
python -m agentcrash run --scenario timeout_retry --model fake
python -m agentcrash check --trace traces/latest.jsonl
ls -la traces/   # expect <timestamp>.jsonl + latest.jsonl
```

Readiness gate: two consecutive clean rehearsals (one `fake`, one live model if the
venue has network). If the live rehearsal fails, demo on `fake` — decide before doors
open, not on stage.

## 3. Run the first scenario (the 2-minute demo loop)

Narrate while typing. The story: the agent looks successful but double-charged.

```bash
# Step 1 — Run: agent attempts the payment under injected chaos
python -m agentcrash run --scenario timeout_retry --model llama-3.3-70b
# Expected: run completes, agent reports PAID. Trace lands in traces/<ts>.jsonl.

# Step 2 — Check: deterministic verifiers grade the path, not the answer
python -m agentcrash check --trace traces/latest.jsonl
```

Expected `check` output for the unpatched policy (the money moment):

```text
[FAIL] Invariant 'exactly_once_mutation' violated!
  - Step 3: charge_card(invoice_id=9821, amount=150) -> TIMEOUT (outcome ambiguous)
  - Step 4: charge_card(invoice_id=9821, amount=150) -> SUCCESS
  - Error: 2 charges registered for invoice #9821 (double charge detected)
[PASS] Invariant 'task_completion': invoice #9821 status == PAID
Verdict: FAIL (1 passed, 1 failed) — see traces/latest.jsonl seq 3-4
```

## 4. Interpret `check` output

| Output | Meaning | Next step |
|---|---|---|
| All `[PASS]`, `Verdict: PASS` | Path respected every invariant | Collect the trace (§6), move to regression or close |
| `[FAIL] exactly_once_mutation` | Retry after ambiguous timeout duplicated the side effect | Diagnose (§5) — this is the v0 demo failure |
| `[FAIL] task_completion` | Agent never reached PAID (over-conservative policy or agent bug) | Inspect trace `tool_result` outcomes; check policy `max_retries` is not 0 |
| `ERROR: trace validation failed` | Trace does not match schema v1 | Do not diagnose; fix the tracer first (see troubleshooting) |
| `WARN: unknown policy version` | Trace policy differs from local `policy/base.yaml` | Note the version; replay with the matching policy file |

Reading a trace directly (when the audience asks "prove it"):

```bash
python -c "import json; [print(json.loads(l)['seq'], json.loads(l)['event_type'], json.loads(l).get('tool'), json.loads(l).get('outcome')) for l in open('traces/latest.jsonl')]"
# Expect: seq 3 tool_result charge_card TIMEOUT_AMBIGUOUS, seq 4 tool_result charge_card SUCCESS
```

## 5. Diagnose, then replay (close the loop on stage)

```bash
# Step 3 — Diagnose: LLM proposes a POLICY-ONLY patch (never touches verdicts)
python -m agentcrash diagnose --trace traces/latest.jsonl --output policy_patch.json
cat policy_patch.json
# Expect: {"rule": "on_tool_timeout", "tool": "charge_card",
#          "action": "check_transaction_status", "retry_with_idempotency_key": true}

# Step 4 — Review the exact diff before applying (never skip on stage)
python -m agentcrash policy diff --base policy/base.yaml --patch policy_patch.json

# Step 5 — Replay: re-run with the patched policy, plus the regression suite
python -m agentcrash replay --trace traces/latest.jsonl --policy policy_patch.json
```

Expected `replay` output:

```text
[PASS] exactly_once_mutation: charge_card executed once (idempotency key inv-9821-a3f)
[PASS] task_completion: invoice #9821 PAID
Regression: 1/1 prior scenarios still green
Verdict: FIX CONFIRMED — patch resolves the violation with no regressions
```

If replay reports `FIX REJECTED` (still failing) or a regression, say so plainly, keep
the trace, and fall back to the pre-recorded golden trace (§7). Never re-run diagnose
in a loop on stage — one attempt, then the golden backup.

## 6. Collect traces (evidence discipline)

```bash
mkdir -p demo-evidence && cp traces/latest.jsonl demo-evidence/demo-<n>.jsonl
python -m agentcrash check --trace demo-evidence/demo-<n>.jsonl | tee demo-evidence/demo-<n>. verdict.txt
```

Keep per run: the trace, the verdict text, the policy file + patch, and the model name.
Name files `demo-01-pre-fix.jsonl`, `demo-02-post-fix.jsonl`, … so the story reads in
order. Traces are append-only: never edit a collected trace; annotate beside it.

## 7. Troubleshooting table (when the demo fights back)

| Symptom | Likely cause | First response (under 60 seconds) |
|---|---|---|
| `run` hangs with no output | Live model unreachable (no network / bad key) | Abort; re-run with `--model fake`; continue the story on the fake adapter |
| `TIMEOUT` never fires; check PASSES pre-fix | Wrong scenario flag or injector seed overridden | Verify `--scenario timeout_retry`; confirm seed line in `run_start` event |
| `check` always FAILs even post-fix | Stale policy: replay used `base.yaml` not the patch | Re-run replay with `--policy policy_patch.json`; show the `policy diff` |
| `diagnose` proposes retries without idempotency key | Model ignored schema hint | Reject the patch (schema validation will); re-run diagnose once, else use golden patch |
| `trace validation failed` on a fresh run | Code changed event fields without a schema bump | Stop; this is a real bug — switch to golden traces, file an issue with the trace |
| Double charge shows 3+ charges, not 2 | `max_retries` > 1 with no idempotency gate | Point it out as campaign evidence (retries multiply harm); use golden trace |
| Venue has no network at all | Credentials or connectivity dead | Full `fake`-adapter demo; state this openly — determinism makes it honest |
| Llama gives a different agent path each run | Model sampling nondeterminism | Expected: verdicts are deterministic over whatever path occurred; re-run `check` live to prove it |

Golden backups (prepare before demo day, store in `demo-evidence/golden/`):

- `golden-pre-fix.jsonl` — known FAIL trace (double charge, seq 3–4).
- `golden-post-fix.jsonl` — known PASS replay trace with the canonical patch.
- `golden-patch.json` — the canonical `policy_patch.json` above.
- Rehearse presenting from goldens once, so the fallback feels practiced, not panicked.

## 8. Rollback (when a change breaks the demo or the suite)

Rollback trigger: any rehearsal where `check`/`replay` error (not FAIL, but ERROR), or
where two consecutive runs with the same seed give different verdicts.

```bash
# 1. Freeze: keep the broken trace, do not overwrite it
cp traces/latest.jsonl demo-evidence/broken-<label>.jsonl
# 2. Roll code back to the last green commit (policy files are compatible in-milestone)
git stash -u && git log --oneline -5   # identify last green SHA
git checkout <last-green-SHA>
# 3. Re-verify the gate
python -m agentcrash run --scenario timeout_retry --model fake
python -m agentcrash check --trace traces/latest.jsonl
# 4. If green, demo from this commit; file the regression with the broken trace attached
```

Data compatibility: traces are versioned (`schema_version`); a code rollback never
invalidates already-collected traces within the same schema version. Policy patches
reference `base_policy` explicitly, so a rolled-back policy file is detected, not
silently misapplied.

## 9. After the demo (15 minutes, demo captain owns it)

1. Archive `demo-evidence/` with that session's traces, verdicts, policy files, and the
   model name(s) used.
2. Log costs: model calls, approximate tokens, and any live-model spend vs. budget.
3. File follow-ups: every surprise from §7 becomes an issue with its trace attached.
4. Confirm the next milestone gate in doc 15 — demos move M-gates, never calendar days.

## 10. Operator contacts and escalation

| Role | Owns | Escalate when |
|---|---|---|
| Demo captain (tech lead) | Narrative, go/no-go on live vs. fake | Any ERROR-class failure in final rehearsal |
| Backend engineer | Harness, tracer, replay correctness | Nondeterministic verdicts or schema errors |
| Product lead | Story, costs, follow-up issues | Budget overrun or scope question from the audience |

**Open Question (owner: tech lead, resolves by M3):** hosted shareable demo vs. local
laptop demo — if hosted, this runbook gains a deploy/rollback section for the demo
instance; until then, local-only is the supported path.
