# AgentCrash — Next Steps Plan

- **Status:** Open (prior session todos complete; CI green on `bfae51d`)
- **Owner:** Founder / tech lead
- **Repo:** `/home/sahil/AgentCrash` (branch `develop`, origin `github.com/SahilSatyam/AgentCrash.git`)
- **Prior plan:** `docs/plans/2026-10-29-agentcrash-implementation.md`

## Goal

Close the known Agent Lab evidence gap, settle the DSPy prompt pack decision, make the harness portable to a second agentic workflow, rotate the exposed API key, and record accepted hygiene items — without breaking the green baseline (197 passed + 1 skipped, ruff + black clean, ACR001 + PII guardrail green).

## Success Criteria

- Agent Lab shows the model's closing reason for every run; tabs 1–2 stay technical-free.
- The DSPy pack is either adopted behind a flag with live-smoke evidence or explicitly kept as a candidate with the decision recorded.
- A second workflow onboards via scenario YAML + tool handlers + verifiers only; loop, tracer, DB, and CLI untouched.
- Live server runs on a fresh key; the exposed key is revoked.
- No regression: full suite, ruff, black, and all CI gates stay green.

## Approach

Do the small certain fix first (done-reason capture + key rotation need no decisions), then resolve the two P1 decisions (pack fate; which second workflow drives the refactor), and only then generalize — the refactor follows a real second workflow, not an abstract one. Hygiene items are recorded, not scheduled.

## Steps

### Step 1 (P0) — Capture the model's `done` reason in the trace

Surfaces: `run_react_loop` in `src/agentcrash/adapter.py`, `redact()` in `src/agentcrash/tracer.py`, `record_run()` path, Agent Lab in `ui/app.js`, new `tests/test_done_reason.py`.

1. After the loop breaks on `done`, emit one `writer.write()` event carrying the verbatim `reason` (extend `run_end` detail or add an `agent_done` phase).
2. Confirm `redact()` preserves it and it flows through `events.detail_json` into `GET /api/runs/<run_key>`.
3. Render it as the closing timeline entry in Agent Lab only.
4. Test with a scripted run using a canned `done` action asserting trace → DB → API round-trip.

### Step 2 (P0) — Rotate the Meta API key

1. Issue a new key; update the server env (`LLAMA_API_KEY`, `LLAMA_BASE_URL=https://api.meta.ai/v1`, `LLAMA_TIMEOUT_S=180`).
2. Restart `agentcrash serve --port 8901`, run one live `POST /api/convert` smoke, revoke the old key. Never paste the new key in chat.

### Step 3 (P1) — Decide the DSPy prompt pack's fate

Context: `prompts/pack-probe-v1.json` (SIMBA, 19/24 live calls) scores 0.7 vs frozen baseline 0.47 on the held-out split but overfits one `uid` detail. Production prompts are untouched; packs adopt explicitly, never automatically.

- Option A (adopt): put the pack instruction behind a flag in the converter path, run a full-loop live smoke, keep the frozen metric as the CI gate.
- Option B (search bigger): rerun with `max_steps` 2–3, more candidates, 12+ train rows, higher `--max-live-calls`; adopt only a pack that wins on both the probe split and a fresh val split.
- Record the outcome in `docs/phase7-evidence/` either way.

### Step 4 (P1) — Plug-and-play refactor, driven by a real second workflow

1. `src/agentcrash/tools.py`: replace hardcoded per-tool branches in `ToolRegistry.call()` with a handler registry mirroring `register_verifier` in `src/agentcrash/verifiers.py`.
2. `build_agent_brief` (`src/agentcrash/adapter.py`): derive protocol sentences from scenario + policy instead of ODRL prose.
3. `reference_patch_for` (`src/agentcrash/policy.py`): replace the S4-hardcoded map with a per-workflow patch provider.
4. Relabel Studio tabs per workflow; Agent Lab event table and `runs`/`events`/`verdicts` schema stay unchanged.
5. Prove it: onboard the second workflow (e.g. support-triage agent) with scenario YAML + handlers + `Verifier` subclasses only.

### Step 5 (P2) — Hygiene (recorded, non-blocking)

- Pre-DB live traces were never backfilled (`seed` covers fixtures only): backfill or accept the loss.
- Multi-fault attribution ambiguity stays accepted (per-step injector events disambiguate).
- Exec-bit discipline: files staged via the Windows path land `+x` and trip ruff EXE002 — run `chmod -x` on non-scripts before committing.

## Validation Plan

- Step 1: `pytest tests/test_done_reason.py -q`, then full `pytest -q`, `ruff check .`, `black --check .`; manual check of one run in Agent Lab.
- Step 2: one live `POST /api/convert` returns `done`; old key revoked at the provider.
- Step 3: pack report JSON + live smoke trace saved under `docs/phase7-evidence/`.
- Step 4: second workflow runs convert → trace → verdict → UI with zero diffs to loop/tracer/DB/CLI; new-workflow setup documented.
- Whole plan: CI `gates` matrix (3.11/3.12/3.13) green on the final push.

## Risks / Open Questions

- Which second workflow drives Step 4? Default: support-triage agent; the refactor shape follows the choice, so confirm before starting it.
- If no second workflow is imminent, defer Step 4 rather than generalizing abstractly.
- Assumption: the SIMBA-optimized instruction transfers to the multi-step agentic loop; Step 3's smoke test is the check that falsifies it.
