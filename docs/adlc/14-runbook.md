# 14 - Runbook (Demo and Operator Guide)

**Status:** Revised for 0.3.0 · **Owner:** Tech lead (demo captain) · **Milestone:** M0 Baseline
**Lens:** ADLC startup runbook: one page to prepare, one loop to demo, one table to
survive surprises. No day-wise timeline; readiness is gated by rehearsal count.

## 1. Service overview

| Item | Value |
|---|---|
| Product | `agentcrash-core` 0.3.0 (import `agentcrash`), chaos engineering for tool-using agents |
| CLI | `check` and `serve` only; run as `python -m agentcrash` |
| Runtime | Python 3.11+, Windows PowerShell, `uv`-managed venv at `.venv` |
| Model | None needed. The demo loop, `check`, and the test suite are offline |
| Evidence | Trace file written by the host script (`run.jsonl` in the guide); verdict reports under `reports\` |
| Control plane | `python -m agentcrash serve` (Lab UI + `/v1` API), loopback only by default |
| Demo scenario | Checkout example from `docs/setup.md` section 3: `timeout_after_commit` on `payments.charge`, then a blind retry |
| Logs | Console output, `reports\*.verdict.json`, and the trace file |
| Escalation | Tech lead (demo captain), then whoever owns the touched module (section 10) |

## 2. Setup (once per machine; rehearse twice before a demo)

```powershell
# 1. Repo and environment
cd C:\Dev\Projects\AgentCrash
uv venv
uv pip install -e ".[dev]"

# 2. Smoke test: expect every test to pass
.\.venv\Scripts\python -m pytest -q

# 3. Confirm the CLI
.\.venv\Scripts\python -m agentcrash --version      # agentcrash 0.3.0
```

No key or token is required. Use `.\.venv\Scripts\python -m agentcrash ...` for every
command (see the Smart App Control note in section 7).

## 3. The demo loop (about 5 minutes)

The story: an agent charges an order. The first `payments.charge` call commits, then
reports a timeout (`timeout_after_commit`). A naive agent retries blindly and charges
twice. The checker catches it; a fixed agent reads status first and passes.

1. Create `checkout.py` from `docs/setup.md` section 3, step 1. Run from an empty
   working directory so `reports\` and `run.jsonl` land there.
2. Run the naive agent and judge it:

```powershell
python checkout.py
python -m agentcrash check --trace run.jsonl `
  --invariant duplicate_mutating_effect --invariant required_step:charge
```

Expected (exit code 1):

```text
charges: 2
[FAIL] Invariant duplicate_mutating_effect violated! (duplicate effect(s): payments.charge at steps charge and charge)
[PASS] Invariant required_step:charge satisfied (step 'charge' present (4 event(s)))
verdict=FAIL report=reports\run_<id>.verdict.json
```

3. Fix the recovery logic (read status before acting again) and re-judge:

```powershell
python checkout.py --fixed
python -m agentcrash check --trace run.jsonl `
  --invariant duplicate_mutating_effect --invariant required_step:charge
```

Expected (exit code 0):

```text
charges: 1
[PASS] Invariant duplicate_mutating_effect satisfied (no duplicate mutating tool effects)
[PASS] Invariant required_step:charge satisfied (step 'charge' present (4 event(s)))
verdict=PASS report=reports\run_<id>.verdict.json
```

Check the exit code right after `check` with `$LASTEXITCODE`. Here `python` means
`<repo>\.venv\Scripts\python`.

Talking points: the final answer looks fine in both runs; the checker grades the path,
not the answer. A passing verdict comes from deterministic invariants, never from a model.

## 4. Interpret `check` output

| Line or code | Meaning | Action |
|---|---|---|
| `[PASS] Invariant <id> satisfied (...)` | Invariant held on this trace | None |
| `[FAIL] Invariant <id> violated! (...)` | Invariant broken; the parenthesis is the evidence | Open the trace, find the named steps |
| `verdict=PASS|FAIL report=...` | Overall verdict and the report path | Keep the report with the trace |
| Exit 0 | PASS | |
| Exit 1 | FAIL, or a PII leak under `--scan-pii` | Read the evidence |
| Exit 2 | Usage or config error (`E_CONFIG`): unknown invariant, missing trace, no `--invariant` given | Fix the command |
| Exit 3 | Corrupt trace (`E_TRACE_CORRUPT`) | Regenerate the trace |

Useful variants:

```powershell
# Machine-readable verdict (prose goes to stderr)
python -m agentcrash check --trace run.jsonl --invariant duplicate_mutating_effect --format json

# Host-defined invariants
python -m agentcrash check --trace run.jsonl --plugin my_agent.invariants --invariant my_check

# PII and secret-shape guardrail (non-zero exit on a hit)
python -m agentcrash check --trace run.jsonl --invariant required_step:charge --scan-pii
```

Built-in invariant ids: `duplicate_mutating_effect`, `fresh_write_only`,
`no_lost_update`, `required_step:<name>`.

## 5. Control plane (optional demo segment)

```powershell
python -m agentcrash serve --port 8901
# Lab UI: http://127.0.0.1:8901/
```

Bind stays on `127.0.0.1` by default. To require a token, set the allowlist before
starting, then pass the same value as `api_key` in the host's `WorkflowConfig`:

```powershell
$env:AGENTCRASH_API_TOKENS = "demo-token"
python -m agentcrash serve --port 8901
```

Retention: `POST /v1/admin/prune` with `project` and `keep_last_n` and/or
`older_than_days`. Hosts feed events to `/v1/events` through the SDK `endpoint` setting;
ingestion is idempotent on `event_id`.

## 6. Collect evidence

After any rehearsal or demo, keep for each run:

1. The trace file (for example `run.jsonl`) and the matching `reports\run_<id>.verdict.json`.
2. The exact commands and exit codes, copied from the console.
3. For perf claims, `.\.venv\Scripts\python bench/run_overhead.py` output compared with
   `docs/phase7-evidence/bench.json` (about 5.7% overhead, ~114 us per step, trace ~38 KB
   for 100 steps).

Do not commit traces or `reports\`; both are gitignored. Run `--scan-pii` before sharing a
trace outside the team.

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `agentcrash.exe` blocked, or Code Integrity event 3077 in Event Viewer | Windows Smart App Control or WDAC blocks the unsigned console-script launcher | Use `python -m agentcrash` (equivalent). Do not turn Smart App Control off for this: Windows cannot turn it back on without a reset |
| `E_CONFIG: unknown invariant '...'` (exit 2) | Id is not built in | Use a built-in id, or add `--plugin <module>` that registers it |
| `E_CONFIG: no invariants: pass at least one --invariant <id>` (exit 2) | `check` needs at least one `--invariant` | Add one |
| `E_CONFIG: trace file not found` (exit 2) | Wrong path or the host script did not write the trace | Run the host script first; check the working directory |
| `E_TRACE_CORRUPT` (exit 3) | Truncated or hand-edited trace | Regenerate it; for hash-chained files use `verify_chain` from `agentcrash.tracer` to find the first bad link |
| Fault never fires | `fault_injection` is off, or boundary names differ | Set `WorkflowConfig(fault_injection=True)`; make the `configure_faults` key and the `maybe_fault` argument identical |
| SDK records nothing | `AGENTCRASH_DISABLED=1` is set | Clear it: `Remove-Item Env:AGENTCRASH_DISABLED` |
| `401` from `/v1/*` | `AGENTCRASH_API_TOKENS` is set | Send the token as `api_key` in `WorkflowConfig`, or unset the variable on the server |
| Export seems lost | Bounded drop-oldest queue, or dead `endpoint` (best-effort flush, never raises) | Raise `max_queue`; check the `endpoint` URL and that `serve` is running |
| Port already in use | Another `serve` is running | `python -m agentcrash serve --port <other>` |
| `pytest` reports failures | Local edit, or a stale checkout | `git status`; `git diff`; rerun on a clean tree |
| `duplicate_mutating_effect` fails after a fix | Retry still calls the mutating tool without status read or idempotency key | Read status first, or reuse one idempotency key per intent |

## 8. Rollback (a change broke the demo or the suite)

```powershell
# 1. Freeze evidence: keep the broken trace and report
New-Item -ItemType Directory -Force demo-evidence | Out-Null
Copy-Item run.jsonl demo-evidence\broken-run.jsonl
Copy-Item reports -Destination demo-evidence\reports -Recurse

# 2. Find the last green commit; do not discard uncommitted work blindly
git status
git log --oneline -5

# 3. Revert the offending commit (creates a new commit; history stays intact)
git revert <bad-SHA>

# 4. Re-verify the gates
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m black --check .
.\.venv\Scripts\python -m pytest -q
```

To demo from a known-good commit without altering your branch, use a separate worktree:

```powershell
git worktree add ..\AgentCrash-green <last-green-SHA>
```

Data compatibility: events and policies are versioned (`v1`), so a code rollback inside
the same schema version does not invalidate collected traces. Rolling back a release:
re-tag the previous green commit and cut a patch release (doc 13, section 8).

## 9. After the demo (15 minutes, demo captain owns it)

1. Archive `demo-evidence\` with the traces, reports, and the commands used.
2. Record anything that surprised you as an issue with its trace attached.
3. Confirm the next milestone gate in doc 13 and the roadmap items in doc 15. Demos move
   M-gates, never calendar days.

## 10. Operator contacts and escalation

| Role | Owns | Escalate when |
|---|---|---|
| Demo captain (tech lead) | Narrative, go/no-go | Any failing gate in final rehearsal |
| Backend engineer | SDK, tracer, control plane | Non-deterministic verdicts, chain or schema errors |
| Product lead | Story, scope | Scope question from the audience |

Removed in 0.3.0 (ADR-009): the scenario runner, `diagnose` and `replay` commands, and
the live-model demo. Host-supplied diagnose and replay return as roadmap item R6.

**Open Question (owner: tech lead):** hosted shareable demo versus local-only. Until
decided, local-only is the supported path.
