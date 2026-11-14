# AgentCrash — Setup and Startup Guide

From zero to a failing-then-passing chaos run in about 10 minutes.
Everything here runs offline; no model key is needed.

## 1. Prerequisites

| Need | Version | Check |
|---|---|---|
| Python | 3.11+ | `python --version` |
| uv (recommended) | any recent | `uv --version` |
| git | any recent | `git --version` |

Works on Windows (PowerShell), macOS, and Linux.

## 2. Install

As a user:

```bash
pip install agentcrash-core
```

As a contributor:

```bash
git clone https://github.com/SahilSatyam/AgentCrash.git
cd AgentCrash
uv venv
uv pip install -e ".[dev]"
pytest -q && ruff check . && black --check .
```

On Windows, Smart App Control or WDAC may block unsigned console-script
launchers such as `.venv\Scripts\agentcrash.exe` (Code Integrity event 3077).
This guide uses `python -m agentcrash`, which is equivalent and always works.
Do not turn Smart App Control off for this: Windows cannot turn it back on
without a reset.

## 3. The 5-minute loop

The scenario: an agent charges an order. The first `payments.charge`
call commits, then reports a timeout (`timeout_after_commit`). A naive
agent retries blindly and charges twice.

### Step 1 — Instrument and break it

```python
# checkout.py
import json
import sys

from agentcrash import AgentCrash, WorkflowConfig

ac = AgentCrash(WorkflowConfig(project="checkout-agent", fault_injection=True, seed=42))
ac.configure_faults({"payments.charge": {"kind": "timeout_after_commit", "occurrence": 1}})
ledger = []

def charge(order_id, key=None):
    if key and any(row["key"] == key for row in ledger):
        return {"charged": order_id, "deduped": True}
    ledger.append({"order": order_id, "key": key})
    return {"charged": order_id}

fixed = "--fixed" in sys.argv
with ac.run(name="checkout") as run, run.step("charge") as step:
    key = "o-1-attempt" if fixed else None
    step.record_tool("payments.charge", inputs={"order": "o-1"}, outputs=charge("o-1", key))
    if run.maybe_fault("payments.charge"):  # agent saw TIMEOUT
        if fixed:
            step.record_tool("payments.status", inputs={"order": "o-1"},
                             outputs={"charged": len(ledger)})
        else:
            step.record_tool("payments.charge", inputs={"order": "o-1"}, outputs=charge("o-1"))

with open("run.jsonl", "w", encoding="utf-8") as fh:
    fh.write("\n".join(json.dumps(e) for e in ac.trace(run.run_id)))
print("charges:", len(ledger))
```

### Step 2 — Judge the naive run

```bash
python checkout.py
python -m agentcrash check --trace run.jsonl \
  --invariant duplicate_mutating_effect --invariant required_step:charge
# [FAIL] Invariant duplicate_mutating_effect violated! (duplicate effect(s): payments.charge ...)
# [PASS] Invariant required_step:charge satisfied
# exit code 1
```

### Step 3 — Fix the recovery logic and re-judge

```bash
python checkout.py --fixed
python -m agentcrash check --trace run.jsonl \
  --invariant duplicate_mutating_effect --invariant required_step:charge
# verdict=PASS, exit code 0
```

The fix reads status before acting again. Sending an idempotency key on
the retry works too.

## 4. Useful extras

```bash
# JSON verdict on stdout (prose goes to stderr)
python -m agentcrash check --trace run.jsonl --invariant duplicate_mutating_effect --format json

# Your own invariants from a module (see README "Your own invariants")
python -m agentcrash check --trace run.jsonl --plugin my_agent.invariants --invariant my_check

# Verify the run's hash chain first (a break exits 3)
python -m agentcrash check --trace run.jsonl --invariant required_step:charge --verify-chain

# PII/secret guardrail over a trace (non-zero exit on hits)
python -m agentcrash check --trace run.jsonl --invariant required_step:charge --scan-pii

# Control plane: Lab UI + /v1 API on loopback
python -m agentcrash serve --port 8901
```

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `unknown invariant '...'` | Built-ins are `duplicate_mutating_effect`, `fresh_write_only[:<tool>,...]`, `no_lost_update`, `required_step:<name>`; anything else needs `--plugin` |
| Fault never fires | Pass `fault_injection=True` and make the boundary name in `configure_faults` match `maybe_fault` |
| SDK seems to do nothing | `AGENTCRASH_DISABLED=1` is set |
| `401 unauthorized` from `/v1/*` | `AGENTCRASH_API_TOKENS` is set; send `api_key` in `WorkflowConfig` |

## 6. Where next

- [Overview](overview.md) — problem and architecture.
- [Architecture](architecture-decoupling.md) — core vs host boundary.
- [Roadmap](plans/2026-11-14-sota-roadmap.md) — adapters, fault catalog v2, metrics.
- [Examples](../examples/README.md) — reference integrations.
