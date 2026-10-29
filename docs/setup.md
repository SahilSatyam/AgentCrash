# AgentCrash — Setup and Startup Guide

Get from zero to a first failing-then-passing chaos run in about 10 minutes.
Everything below runs offline with the stub/fake adapters — no API key needed.

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
git checkout feat/all-phases   # current implementation branch (until merged)

# 2. Create the environment (uv-managed; no pip binary needed)
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. Sanity-check the gates (must all pass)
ruff check .
black --check .
pytest -q
```

Expected: `All checks passed!`, `N files would be left unchanged`,
`129 passed`. If `uv` is missing, install it user-scoped inside WSL
(`curl -LsSf astral.sh/uv/install.sh | sh`) — never on Windows.

Live-model runs (optional, not needed for the loop below) use
`LLAMA_API_KEY` and optionally `LLAMA_BASE_URL`:

```bash
export LLAMA_API_KEY=paste-key-here
export LLAMA_BASE_URL=https://your-llama-endpoint/v1   # only for live runs
```

Keys come from the environment only — never flags, files, or traces.

## 3. Startup: the 2-minute demo loop

Scenario `timeout_retry`: pay vendor $150 for invoice #9821 while a
`timeout_after_commit` fault fires at step 3. Naive retry double-charges;
the fixed policy reconciles first.

```bash
# Step 1 — Run (offline stub agent; reproducible via --seed 42)
python -m agentcrash run --scenario timeout_retry --model fake --seed 42
# run_id=43e3064f37f779fe ... charges=2   (double charge reproduced)

# Step 2 — Check (deterministic verifiers; exit 1 = invariant violated)
python -m agentcrash check --trace traces/43e3064f37f779fe.jsonl
# [FAIL] ... exactly_once_mutation ... steps [3, 4]
# [PASS] ... task_completion ... status=PAID

# Step 3 — Diagnose (proposes a policy patch only; changes no verdict)
python -m agentcrash diagnose --trace traces/43e3064f37f779fe.jsonl \
    --output policy_patch.json
# proposed: on_tool_timeout/charge_card -> check_then_act ...

# Step 4 — Replay (verifies the fix + runs the regression suite)
python -m agentcrash replay --trace traces/43e3064f37f779fe.jsonl \
    --patch policy_patch.json
# fix: VERIFIED (exactly_once_mutation PASS, task PAID)
# regression: 4/4 golden traces still PASS
```

`agentcrash` (console script) and `python -m agentcrash` are equivalent.
`--help` on any command lists every flag; `--format json` gives
machine-readable verdicts (prose goes to stderr).

## 4. More scenarios

| Scenario | Fault | Key invariant |
|---|---|---|
| `timeout_retry` | ambiguous timeout at step 3 | `exactly_once_mutation` |
| `poisoned_doc` | injected refund instruction in a doc | `no_attacker_controlled_args` |
| `stale_read` | concurrent writer between read and write | `fresh_write_only`, `no_lost_update` |
| `odrl_convert` (+ `_poisoned`, `_concurrent`) | S4 policy-convert faults, model-free | `odrl_valid`, `no_attacker_grant` |

Same four commands work for each; `check` needs `--state` only when the
default `reports/<run_id>.state.json` is absent. Poisoned-doc bytes print
inside `<untrusted_content>` delimiters — test content, never instructions.

## 5. Useful extras

```bash
# Validate inputs without running anything
python -m agentcrash run --scenario timeout_retry --dry-run

# Strict schema + hash-chain verification
python -m agentcrash check --trace <f> --strict-schema --verify-chain

# PII/secret guardrail over a trace + state pair (non-zero exit on hits)
python -m agentcrash check --trace <f> --state <s> --scan-pii

# Corpus metrics (precision/recall, replay-fix worksheets)
python scripts/metrics.py
```

Traces land in `traces/` and reports in `reports/` (both gitignored —
only curated goldens under `traces/golden/` are committed).

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `run` hangs / model errors | Use `--model fake`; live models need network + `LLAMA_API_KEY` |
| `check` says state file not found | Pass `--state` pointing at the run's `reports/<run_id>.state.json` |
| `diagnose` refuses with "No invariant violations" | The trace passes — nothing to diagnose; check a failing trace |
| `replay` reports a regression | Read which golden failed and which rule caused it; do not force the patch |
| Fresh-run trace differs from golden | Inputs differ (e.g. `--model` string is part of the run config); same inputs are byte-identical |
| Gates fail after an edit | `ruff check .`, `black .` (write mode), `pytest -q`; seeds 1, 7, 42, 123, 2026 must agree |

## 7. Where next

- [System overview](overview.md) — architecture and prototype slice.
- [ADLC lifecycle docs](adlc/) — requirements, design, testing, runbook; start with the [map](adlc/README.md).
- [Implementation plan](plans/2026-10-29-agentcrash-implementation.md) — phase checkboxes and evidence.
- [Runbook](adlc/14-runbook.md) — demo-day rehearsal, troubleshooting, rollback.
- [Contributing](adlc/13-contributing-and-workflow.md) — branches, Conventional Commits, definition of done.
