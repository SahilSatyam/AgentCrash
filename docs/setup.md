# AgentCrash — Setup and Startup Guide

Get from zero to a first failing-then-passing chaos run in about 15 minutes.
The agent is live (real Meta model calls end to end); verification,
fixtures, and the test suite stay offline and deterministic.

## 1. Prerequisites

| Need | Version | Check |
|---|---|---|
| WSL2, Ubuntu-24.04 distro | any recent | `wsl -l -v` (Windows side) |
| Python | 3.11+ (3.12 verified) | `python3 --version` (inside WSL) |
| uv | any recent (0.13 verified) | `uv --version` (inside WSL) |
| git | any recent | `git --version` |

All commands below run **inside WSL**. Tooling lives in WSL by design;
do not install a second Python/Node toolchain on Windows.

## 2. Setup

```bash
# 1. Enter WSL and clone the repo
wsl -d Ubuntu-24.04
git clone https://github.com/SahilSatyam/AgentCrash.git
cd AgentCrash
git checkout feat/live-llama-run   # current implementation branch (until merged)

# 2. Create the environment (uv-managed; no pip binary needed)
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. Sanity-check the gates (must all pass; no key needed)
ruff check .
black --check .
pytest -q
```

Expected: `All checks passed!`, `32 files would be left unchanged`,
`136 passed, 1 skipped` (the skip is the key-gated live test).
If `uv` is missing, install it user-scoped inside WSL
(`curl -LsSf astral.sh/uv/install.sh | sh`) — never on Windows.

Live runs need a Meta model key from the environment only — never flags,
files, or traces (the tracer redacts secret-shaped values at write time):

```bash
export LLAMA_API_KEY=paste-key-here
export LLAMA_BASE_URL=https://api.meta.ai/v1
```

Without these, `run` and the live `diagnose` path fail closed
(`E_MODEL_API`, exit 5 / fixture fallback); `check`, fixtures, and the
suite stay offline.

## 3. Startup: the 5-minute demo loop

Scenario `odrl_convert_timeout`: convert an access request to ODRL 2.2
JSON-LD and save it once, while a `timeout_after_commit` fault fires on
the first `save_odrl` write (commits server-side, reports TIMEOUT).
Naive blind retry double-saves; the fixed policy reconciles first.

```bash
# Step 1 — Run, naive policy (live agent; reproducible shape via --seed 42)
python -m agentcrash run --scenario odrl_convert_timeout --seed 42 \
    --trace-out /tmp/naive.jsonl --state-out /tmp/naive.state.json
# status=completed ... saves=2   (double save reproduced)

# Step 2 — Check (deterministic verifiers; exit 1 = invariant violated)
python -m agentcrash check --trace /tmp/naive.jsonl --state /tmp/naive.state.json
# [FAIL] ... exactly_once_save ... save steps [4, 5]
# [FAIL] ... odrl_valid ... missing required 'target'
# [PASS] ... no_attacker_grant ...

# Step 3 — Run, fixed policy (strict convert + reconcile-first)
python -m agentcrash run --scenario odrl_convert_timeout \
    --policy policies/fixed-odrl-timeout.yaml --seed 42 \
    --trace-out /tmp/fixed.jsonl --state-out /tmp/fixed.state.json
python -m agentcrash check --trace /tmp/fixed.jsonl --state /tmp/fixed.state.json \
    --policy policies/fixed-odrl-timeout.yaml
# verdict=PASS (exactly 1 save, valid ODRL, rival-safe)

# Step 4 — Diagnose the naive trace (live model proposes the patch)
python -m agentcrash diagnose --trace /tmp/naive.jsonl --state /tmp/naive.state.json \
    --output /tmp/patch.json
# proposed: on_tool_timeout/save_odrl -> check_then_act ...

# Step 5 — Replay under the patch (verifies the fix)
python -m agentcrash replay --trace /tmp/naive.jsonl --state /tmp/naive.state.json \
    --patch /tmp/patch.json --no-regression
# fix: NOT_FIXED means the patch was partial — rerun with
# --policy policies/fixed-odrl-timeout.yaml for fix: VERIFIED
```

`agentcrash` (console script) and `python -m agentcrash` are equivalent.
`--help` on any command lists every flag; `--format json` gives
machine-readable verdicts (prose goes to stderr). Each run costs a few
chat completions; `max_steps` (12) bounds the calls per run.

## 4. More scenarios

| Scenario | Fault | Key invariant |
|---|---|---|
| `odrl_convert` | sloppy convert, no validation gate | `odrl_valid` |
| `odrl_convert_poisoned` | injected attacker grant in the NL | `no_attacker_grant` |
| `odrl_convert_concurrent` | rival writer before the first read | `fresh_write_only`, `no_lost_update` |
| `odrl_convert_timeout` | ambiguous save timeout, first write | `exactly_once_save` |

Same commands work for each; `check` needs `--state` only when the
default `reports/<run_id>.state.json` is absent, and `--policy` when the
trace ran under a non-default policy. NL bytes print inside
`<untrusted_content>` delimiters — test content, never instructions.

## 5. Useful extras

```bash
# Validate inputs without running anything
python -m agentcrash run --scenario odrl_convert --dry-run

# Strict schema + hash-chain verification
python -m agentcrash check --trace <f> --state <s> --strict-schema --verify-chain

# PII/secret guardrail over a trace + state pair (non-zero exit on hits)
python -m agentcrash check --trace <f> --state <s> --scan-pii

# Corpus metrics (precision/recall, replay-fix worksheets; needs the key)
python scripts/metrics.py
```

Traces land in `traces/` and reports in `reports/` (both gitignored —
only curated fixtures under `tests/fixtures/traces/` are committed).

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `run` fails with `E_MODEL_API` | Export `LLAMA_API_KEY` + `LLAMA_BASE_URL`; without a key only `check` and the suite work |
| `check` says state file not found | Pass `--state` pointing at the run's state file |
| `check --strict-schema` says run_id does not recompute | Pass `--policy` with the policy the trace ran under |
| `diagnose` refuses with "No invariant violations" | The trace passes — nothing to diagnose; check a failing trace |
| `diagnose` uses the fixture patch, not the model | `LLAMA_BASE_URL` unset or the model unreachable — transport failures fall back deliberately |
| Live run 400s or returns unusable content | Model-side protocol drift; the adapter fails closed — see ADR-008 for the known-good shape |
| Gates fail after an edit | `ruff check .`, `black .` (write mode), `pytest -q` |

## 7. Where next

- [System overview](overview.md) — architecture and prototype slice.
- [ADLC lifecycle docs](adlc/) — requirements, design, testing, runbook; start with the [map](adlc/README.md).
- [Implementation plan](plans/2026-10-29-agentcrash-implementation.md) — phase checkboxes and evidence.
- [Runbook](adlc/14-runbook.md) — demo-day rehearsal, troubleshooting, rollback.
- [Contributing](adlc/13-contributing-and-workflow.md) — branches, Conventional Commits, definition of done.
