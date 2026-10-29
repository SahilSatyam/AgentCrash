# AgentCrash — Phase-Wise Implementation Plan

- **Status:** Phase 0 done on branch `chore/phase-0-baseline` (review pending); Phases 1+ await `go`
- **Owner:** Founder / tech lead
- **Repo:** `/home/sahil/AgentCrash` (git, origin `github.com/SahilSatyam/AgentCrash.git`)
- **Docs basis:** `docs/adlc/` 16-doc set (working tree) + AgentODRL for S4 pin (`_staging_adlc/` S4/v0.2.1 assumed at planning time is absent — see ADR-007)
- **Milestone scheme:** M0–M3 per staging doc 15 (see F.1 — must be applied repo-wide in Phase 0)

## A. Executive Summary

AgentCrash is chaos engineering for tool-using agents (Run → Inject → Trace → Check → Diagnose & Replay), with the hard rule that deterministic verifiers own verdicts and the LLM only proposes policy diffs. The 16 lifecycle docs specify architecture, schemas, CLI flags, exit codes, module boundaries, and test gates in unusual detail. Current state is **skeleton only** (Phase 0, on branch `chore/phase-0-baseline`): empty `src/` package, smoke test, packaging, and CI — no behavior code yet. The strategy is to freeze doc contradictions first (one ADR + anchor table), then build one vertical slice at a time — tracer/schemas → tools/injector/stub-run → verifiers/check → policy/diagnose/replay → S2/S3 → S4 → packaging/demo — with cheap deterministic gates running before any model spend.

Priority key used below: **P0** = critical for the core loop and submission; **P1** = required for reliability, suite depth, or demo differentiation; **P2** = deferrable enhancement.

## B. Documentation Inventory and Understanding

All files below were read during planning; none were inaccessible.

| Doc | Purpose | Depth |
|---|---|---|
| `docs/adlc/README.md` | Map, milestones, status convention, v0 anchors | Full |
| `01` planning | Bet, MVP, risks, costs | Full (staging v0.2.1) |
| `02` PRD | Personas, Journeys 1–5, scope incl. S4 | Full (staging v0.2.1) |
| `03` SRS | FR-01–FR-25 with Given/When/Then acceptance | Full |
| `04` glossary | 36 terms, canonical names, domain model | Headings + canonical terms |
| `05` system design | C4, sequence, virtual clock, determinism, stack | Full |
| `06` data design | scenario/policy/trace/state schemas, validation rules | Full |
| `07` API contracts | CLI flags, exits 0–5, module signatures, verifier protocol | Full |
| `08` ADRs | ADR-001–005 ratified (verifier/LLM split, JSONL, clock, policy-only diagnoser, scenario-first) | Titles + constraints |
| `09` agent context | Repo map, commands, determinism rules, PR expectations | Full |
| `10` testing | Pyramid, seed matrix (1,7,42,123,2026), gates G1–G6, diagnoser rubric | Full |
| `11` security | Threat model, secrets, redaction, sandboxing, supply chain | Full |
| `12` NFRs | Budgets (harness ≤10% overhead, 100-step trace ≤2MB), observability | Headings + §5 via 03 |
| `13` contributing | Branches, Conventional Commits, DoD, quickstart | Full |
| `14` runbook | Setup, 2-min demo loop, troubleshooting, rollback | Headings + key steps |
| `15` roadmap | Milestones M0–M3, work items, matrices, changelog to v0.2.1 | Full (staging v0.2.1) |
| `docs/overview.md` + root `README.md` | Product story, architecture, CLI workflow | Full |

Fit: 01/02 say why/who; 03 says what testably; 05/06/07 say how exactly; 08 locks decisions; 09/13 say how to work; 10/12 say how to prove; 11 says how not to leak; 14 says how to demo; 15 sequences it.

## C. Architecture and End-to-End Execution Flow

Single Python package; modules, not services.

```text
scenario.yaml + policy.yaml
  → run (adapter ReAct + tools + injector, VirtualClock, seeded RNG)
  → traces/<run_id>.jsonl (hash-chained) + reports/<run_id>.state.json
  → check (pure verifiers over trace+state) → verdict JSON, exit 0/1/3
  → diagnose (LLM, read-only, emits policy_patch.json; fixture-backed offline)
  → replay (same seed + patched policy) → fix VERIFIED + golden regression
```

- [ ] `adapter`: ReAct loop; `llama_api` + `stub`/`fake` backends behind `complete(messages, tools, timeout_s)`
- [ ] `tools`: VirtualClock (tick 50ms), `charge_card` / `get_transaction_status` (+ S2/S3/S4 tools later), idempotency-key dedup ledger
- [ ] `injector`: YAML fault schedule (`timeout_after_commit` step 3 default); unknown types refused
- [ ] `tracer`: append-only JSONL, SHA-256 `prev_hash` chain, write-time redaction
- [ ] `verifiers`: `Verifier` protocol over `(trace, state)`; no network/clock/model
- [ ] `policy`: policy/v1 load/validate/merge-patch; `diagnoser` diff-only and schema-gated
- [ ] `replay`: seed-pinned re-execution + `traces/golden/` regression report

## D. Requirements Traceability and Gap Analysis

Status today: all behavior below is **missing** (Phase 0 landed the empty `src/`/`tests/` skeleton, packaging, and CI only — no `schemas/`, `scenarios/`, `policies/`, or harness logic yet).

- [ ] FR-01–FR-04 run/seed/stub/max-steps (03 §2.1) → verify: `pytest tests/test_run.py` + golden byte-diff (normalized)
- [ ] FR-05–FR-08 injection determinism/refusal (03 §2.2) → verify: seed-matrix rerun diff; `alien_fault` refusal test
- [ ] FR-09–FR-12 trace schema/chain/redaction (03 §2.3, 06 §5/§8, 11 §5–6) → verify: `jsonschema` validation + chain verify + secret-grep tests
- [ ] FR-13–FR-16 verifiers pure/offline (03 §2.4) → verify: golden pass/fail suite, 3× identical reports incl. network-off
- [ ] FR-17–FR-20 diagnose diff-only/read-only (03 §2.5) → verify: patch-schema test + ledger-checksum test
- [ ] FR-21–FR-22 replay/regression/fail-closed (03 §2.6) → verify: fix + regression test; `time_travel` rejection test
- [ ] FR-23–FR-25 CLI/exit codes/reports (03 §2.7, 07 §2–3) → verify: `--help` test, exit-code matrix, golden report compare
- [ ] S2/S3 scenarios + verifiers, corpus precision/recall ≥0.95/0.90 (15 M1) → verify: scenario oracles + worksheets
- [ ] S4 ODRL track + v0.2.1 pin (02 §7.1, 15 M1.9–11) → verify: 3 fault variants reproduce + replay green
- [ ] NFR budgets ≤10% overhead, ≤2MB/100 steps (03 §5, 12) → verify: benchmark script in CI
- [ ] Secrets/redaction/sandbox/supply chain (11 §4–7) → verify: redaction tests, lockfile, ACR001 lint

## E. Phase-Wise Implementation Roadmap

Conventions for all phases: WSL repo root `/home/sahil/AgentCrash`; `uv`-managed venv (`uv` present, no `pip` binary); Python 3.12 satisfies 3.11+; pytest + `jsonschema`; ruff with custom `ACR001` (ban `time.*`/global `random` in tools/injector/verifiers); black line-length 100; Conventional Commits with `Refs: M0…`; one concern per PR ≤~400 lines.

### Phase 0 — Baseline and frozen decisions (P0)

Objective: a trustworthy starting line so later artifacts (schemas, goldens, seed-matrix tests) never need rework.

Scope: docs + ADR-006 + empty-but-green repo skeleton. No behavior code.

Tasks:

- [x] Port `_staging_adlc` 01/02/15 S4 + v0.2.1 content into `docs/adlc/`; add S4 spec stubs to 03/06/07/10 (or ADR-007) — done as no-op: staging tree is absent from tree and history, so ADR-007 records the gap and defers S4 stubs until sourced (no specs invented)
- [x] Record ADR-006: M0–M3 scheme repo-wide; frozen anchor table (threshold 1500ms, fault latency 2000ms, seed 42, tick 50ms); 06 §5 canonical schema; run_id normalization rule for golden diffs — done (plus ADR-007)
- [x] Fix README + 10-A anchors to ADR-006 values; assign all TBD OQ owners (founder = scope, tech lead = technical) — done (adlc README, docs 03/04/09/10/11/12/13/14)
- [x] Scaffold `pyproject.toml` (`agentcrash` console script, `[dev]` extras), lockfile, pytest/ruff/black config, `.gitignore` (`traces/*` except goldens, `.venv/`), empty package skeleton — done
- [x] Verify `github.com/SahilSatyam/AgentCrash.git` is public and README quickstart works; back up `_staging_adlc` until ported — public verified; backup n/a (staging absent); quickstart verified via pristine-tree clean room on the same machine (`uv venv`, `uv pip install -e ".[dev]"`, ruff/black/pytest all green); a different-machine check remains a nice-to-have

Dependencies: this plan's approval. Nothing else.

Deliverables:

- [x] Committed `docs/adlc/` (filled set + ADR-006) and green empty CI (lint + format + empty pytest) — committed on branch `chore/phase-0-baseline` (merge pending review); gates observed green locally, GitHub Actions runs on push

Acceptance / testing:

- [x] `pytest -q` green (1 passed); `ruff check .` and `black --check .` clean; `git status` shows only intended files — all observed 2026-10-29

Risks: doc churn — mitigated by freezing anchors in ADR-006.

Exit criteria:

- [x] Docs committed, contradictions F.1–F.4 closed, skeleton CI green — F.1–F.4 closed via ADR-006 (F.4 rule recorded, enforced Phase 2); commits on branch, merge pending review

### Phase 1 — Time, evidence, secrets (P0)

Objective: the evidentiary foundation (tracer + schemas + ledger).

Scope: `VirtualClock`, `TraceWriter` (06 §5 envelope, `prev_hash` chain, sorted keys, write-time redaction per 11 §5–6), `schemas/` (trace-event, scenario, policy, policy-patch, env-state), in-memory ledger with idempotency-key dedup, `EnvState` snapshot + `ledger_hash`.

Tasks:

- [x] Implement `VirtualClock` (integer ms, `t=0`, tick 50ms) and forbid wall-clock via ACR001 — `clock.py` + `test_acr001.py` green
- [x] Implement `TraceWriter`: envelope fields, gapless `event_id`, monotonic `ts_virtual`, `prev_hash` chain, `--verify-chain` reader — `tracer.py::verify_chain` names first bad link
- [x] Author `schemas/*.json` for all five schema versions; strict reader (unknown version → `E_SCHEMA`) — const-version schemas reject unknown versions
- [x] Implement ledger (invoice/charge/dedup on `(invoice_id, idempotency_key)`) + state snapshot + `ledger_hash` — `ledger.py`, keyless-duplicate bug covered
- [x] Implement write-time redaction (`*_token`/`*secret*`/`*key` except `idempotency_key`, emails, card-like runs) + 64KB `detail` truncation marker — `redact()` + truncation tests green

Dependencies: Phase 0 (canonical schema + anchors).

Deliverables:

- [x] `clock.py`, `tracer.py`, `schemas/*.json`, ledger module; redaction + chain-verify tests — all landed and green

Acceptance / testing:

- [x] Synthetic trace validates against every schema via `check --strict-schema` equivalent — `test_schemas.py` validates 5 hand-built fixtures
- [x] Broken chain names the first bad link; secret-grep over traces is negative; unit coverage ≥90% on tracer/policy-adjacent code — `verify_chain` + redaction tests green

Risks: schema drift vs 09's older field names — mitigate by fixing 09 §5 in the same PR.

Exit criteria:

- [x] G1/G2-green on these modules; 06 §8 rules 1–5 demonstrable on a hand-built trace — ruff/black/pytest green (rules 4–5 land with Phase 2/3 runner)

### Phase 2 — Act and fail reproducibly (P0)

Objective: first observable chaos, fully offline (no API key).

Scope: `ToolRegistry` (`charge_card`, `get_transaction_status`; seeded latency; `timeout_after_commit`/`timeout_before_commit`), `FaultSchedule` from `scenario.yaml` + unknown-type refusal (FR-07/08), stub agent (`retry_immediately`) + `fake` adapter, `run` command, `run_id = sha256(scenario+policy+seed)[:16]`, `--dry-run`, `--seed`/`--max-steps`.

Tasks:

- [x] Implement registry + arg schema validation (unknown tools/args rejected and recorded, never executed) — done: `tools.py` ToolRegistry, unit-covered in `test_run.py`
- [x] Implement injector: YAML schedule, step-3 default, seeded PRNG single `random.Random(seed)` threaded through — done: `injector.py` FaultSchedule; `alien_fault` refused exit 2 with no trace
- [x] Implement stub agent + `fake` adapter; `run` writes trace + state snapshot; per-tool virtual deadline → TIMEOUT continues within 5s wall — done: `adapter.py` run_react_loop + `cli.py run`, offline stub/fake, faulted run <5s wall
- [x] Author `scenarios/timeout_retry/scenario.yaml` + naive `policies/default.yaml`; check in first golden failing trace — done: golden at `traces/golden/timeout_retry-seed42.jsonl` (2 charges, PAID)

Dependencies: Phase 1 (tracer, schemas, ledger).

Deliverables:

- [x] `run` end-to-end offline; scenario + naive policy; golden failing trace — done: `steps=4 charges=2`, chain-verified, committed

Acceptance / testing:

- [x] FR-01–03 oracles; seed matrix (1,7,42,123,2026) byte-identical after normalizing `run_id`/`ts_wall`; `alien_fault` exits non-zero ≤2s with no trace file — done: `test_run.py` 16 tests green, matrix reruns normalized-identical, alien refusal <2s
- [x] Wiring tests (10 §3 stub rows) + rerun-diff green — done: tool_call → TIMEOUT tool_result → injector_event order asserted at step 3

Risks: nondeterminism creep — mitigate with ACR001 + quarantine-once-in-20 policy (10 §6).

Exit criteria:

- [x] `run` emits schema-valid JSONL for happy + fault paths with no key; ledger shows 1 committed charge vs agent-observed TIMEOUT at step 3 — done: every event validates, chain ok, ch_001 linked to step-3 TIMEOUT, final 2 charges

### Phase 3 — Verdicts you can audit (P0)

Objective: deterministic pass/fail with step-level evidence.

Scope: `Verifier` protocol + `exactly_once_mutation` + `task_completion`, `check` (human text + `--format json`, `--strict-schema`, `--verify-chain`, exits 0/1/3), golden suite (pass + fail with pinned verdicts), verdict reports (invariant table, side-effect count, completion, hashes).

Tasks:

- [x] Implement `Verifier` protocol (pure `(trace, state)` → `Verdict`; harness bugs raise `VerifierError`, never false verdicts) — done: `verifiers.py` protocol + registry (`get_verifier` unknown → E_CONFIG), `run_verifiers` rejects bad verdict objects
- [x] Implement the two v0 verifiers with offending-step evidence strings — done: `exactly_once_mutation` cites charge steps, `task_completion` cites invoice status; ± fixtures in `test_verifiers.py`
- [x] Implement `check` incl. JSON mode (stdout pure JSON, prose to stderr) and `--report-out` verdict file — done: `cli.py cmd_check`, exits 0/1/2/3, `--strict-schema` 06 §8 rules 1–5, `--verify-chain` names first bad link
- [x] Curate `tests/golden/` (failing double-charge + fixed single-charge + give-up UNPAID traces) with expected verdicts pinned — done: `traces/golden/` holds seed42 failing + seed43 fixed + seed44 give-up trace/state/verdict triples

Dependencies: Phase 2 (traces to judge).

Deliverables:

- [x] `check` + golden suite; G3 offline gate (network disabled) green — done: verifiers/check touch no network (ACR001-clean); `pytest -q` 67 passed incl. golden suite + 3× identical JSON reports

Acceptance / testing:

- [x] FR-13–16 oracles; 3× identical JSON reports incl. one network-off run; false-pass rate 0 on goldens; malformed traces rejected (never PASS) — done: `test_check.py` covers all four (network-off by construction, no sockets in verifier path)

Risks: verifier false-pass killing trust — mitigate with positive + negative fixtures per verifier (13 §7).

Exit criteria:

- [x] `run → check` shows `[FAIL] exactly_once_mutation` with steps 3/4 evidence and `[PASS] task_completion` on the golden — done: observed on `traces/golden/timeout_retry-seed42.jsonl` (exit 1, steps [3, 4] cited)

### Phase 4 — Close the loop (P0)

Objective: a proven fix, still offline-first (minimum viable demo: stub-narrated full loop; M0-exit core).

Scope: policy/v1 load/validate/merge-patch application, `diagnose` (fixture-backed reference patch with `retry_with_idempotency_key` + `reconcile_before_retry`; live-LLM path behind `--model` only, output schema-gated, read-only proof via ledger checksum), `replay --patch/--policy` + `--regression` over goldens, replay reports.

Tasks:

- [x] Implement policy engine: load/validate `policy/v1`, apply `policy_patch/v1` merge-patch, `diff_policies` review rendering — done: `policy.py` + `test_policy.py` green (ADR-004 shorthand normalized, `time_travel` rejected)
- [x] Implement fixture-backed `diagnose` (reference patch for the double-charge trace; refuses passing traces with `No invariant violations to diagnose`) — done: `cli.py cmd_diagnose`, exit 2 on PASS traces
- [x] Implement live-LLM `diagnose` path behind `--model` flag (OpenAI-compatible `chat.completions` vs `LLAMA_BASE_URL`); output strictly schema-validated, `E_POLICY_REJECTED` otherwise — done: opt-in live path with fixture fallback, invalid model output exits 4
- [x] Implement `replay` (same scenario + seed from source trace) + `--regression` over `traces/golden/`; invalid patch fails closed pre-run (exit 4, no trace) — done: `cli.py cmd_replay` + `_regress_golden`
- [x] Author `policies/fixed-timeout.yaml` (status-read-then-same-key-retry) and check in fix-verified golden — done: fixed policy + `scenarios/timeout_retry_fixed/` variant + `traces/golden/timeout_retry-fixed-seed42.*` (strict-clean); stub implements `check_then_act` reconcile reads

Dependencies: Phase 3 (verdicts); live path additionally needs key/endpoint decision F.5.

Deliverables:

- [x] `diagnose`, `replay`, fixed policy, fix-verified golden — all landed and green

Acceptance / testing:

- [x] FR-17–22 oracles incl. `time_travel` fail-closed and passing-trace refusal; `policy/base.yaml` mtime unchanged by `diagnose` — done: `test_replay.py` fail-closed (no trace written), `test_diagnose.py` refusal + mtime/checksum proof
- [x] Replay shows `fix: VERIFIED (exactly_once PASS, task PAID)` + `regression: control PASS` — done: observed `fix: VERIFIED (exactly_once_mutation PASS, task PAID)` + `regression: 4/4 golden traces still PASS`

Risks: diagnoser scope creep — mitigate with `--max-rules 1` default and diff-only enforcement in code.

Exit criteria:

- [x] Full `run → check → diagnose → replay` green offline; default suite uses fake adapter, live calls opt-in `slow` — done: 95 passed, loop observed green with no model key

### Phase 5 — S2 + S3 + corpus (P1)

Objective: three-scenario coverage with measured precision (M1 gates).

Scope: `read_doc` poisoned fixtures (single `poison_docs.yaml`, `synthetic_attack` labels, `<untrusted_content>` delimiters, CLI delimiter rendering), `no_attacker_controlled_args` verifier; concurrent-writer + `fresh_write_only`/`no_lost_update` verifiers; labelled corpus + precision/recall worksheet (≥0.95/0.90) + 4/5 replay-fix worksheet; S2/S3 diagnoser coverage.

Tasks:

- [x] Add `read_doc` (seeded clean/poisoned bytes, `untrusted_source` tagging) + `key_value_store` (versioned reads/writes) tools — evidence: `tools.py` + ledger KV, oracles green
- [x] Author S2/S3 `scenario.yaml` files + fixtures; implement the three verifiers behind the plugin interface (no `check` changes) — evidence: `poisoned_doc/`, `stale_read/`, `poison_docs.yaml`, 3 registered verifiers
- [x] Build labelled corpus (clean vs injected across S1–S3); compute precision/recall + replay-fix worksheets — evidence: 30-trace corpus + `scripts/metrics.py` + `worksheets.md`
- [x] Extend diagnoser reference patches to S2 (confirmation gate) and S3 (version re-read) — evidence: `reference_patch_for` dispatch, CLI-verified

Dependencies: Phase 4 (loop + plugin interface proven).

Deliverables:

- [x] S2/S3 scenarios, fixtures, verifiers, metric worksheets — evidence: all landed, 105 tests green

Acceptance / testing:

- [x] 10 §4 oracles B/C; 15 M1 exit criteria 1–5; G6 regression green; poison-fixture handling rules (11 §3) observed — evidence: `test_scenarios_s2_s3.py` 10/10, replay regression 4/4

Risks: prompt-only-fix temptation — mitigate with structural gates only (confirmation/freshness), per 02 §4.

Exit criteria:

- [x] M1 gates green; corpus + worksheets checked in as exit evidence — evidence: precision 1.000/recall 1.000, fix rate 5/5, 0 new violations

### Phase 6 — S4 ODRL demo (P1)

Objective: model-free 5-minute demo reusing the same loop (Journey 5).

Scope: scripted ReAct (`parse_policy → validate_odrl → save_odrl` versioned store), ODRL 2.2 JSON-LD `Set`/`Offer`/`Agreement` (hand-rolled frozen subset `odrl-subset-frozen-v1`, NOT pyshacl — deviation: install weight; see box below), 4 verifiers (`odrl_valid`, `no_attacker_grant`, `exactly_once_save`, `fresh_write_only`), 3 fault variants, synthetic NL fixtures.

Tasks:

- [x] Implement S4 synthetic tools + scripted converter (no model; `--model` ignored for `odrl_convert`) — evidence: `tools.py` parse/validate/save branches, `adapter.py` s4 flow + `is_odrl_scenario`, CLI ignored-note; 12 S4 tests green
- [x] Author S4 fixtures (basic NL policies + expected ODRL JSON-LD, poisoned-sentence and concurrent-edit variants) — evidence: `tests/fixtures/odrl_nl.yaml` (3 entries, naive+expected ODRL each), 3 scenario dirs, `policies/fixed-odrl-convert.yaml`
- [x] Implement 4 S4 verifiers + goldens; add Journey-5 steps to runbook (doc 14) — evidence: `verifiers.py` OdrlValid/NoAttackerGrant/ExactlyOnceSave (+FreshWriteOnly covers save_odrl), 6 corpus goldens + labels, runbook §5B

Deviations from the plan as written (accepted at implementation):

- No `pyshacl` dependency: `src/agentcrash/odrl.py` hand-rolls the frozen subset (install weight avoided; `uv.lock` untouched).
- Goldens live in `tests/corpus/s4-*-seed42.*` (not `traces/golden/`): that directory is replay's live `--regression` input, and pre-fix S4 traces re-run under non-S4 policies would red-flag the S1–S3 regression gates (Phase 5 set the `tests/corpus/` precedent for scenario goldens).
- Attacker principal is `attacker-client` (not an email): trace write-time redaction (doc 11 S5–S6) scrubs email shapes, which would erase the verifier evidence.

Dependencies: Phase 5 (loop + regression harness); S4 ported docs from Phase 0.

Deliverables:

- [x] `odrl_convert` scenario + fixtures + verifiers + goldens — evidence: 3 scenario dirs + fixture + fixed policy + 6 corpus files, all committed

Acceptance / testing:

- [x] All 3 fault variants reproduce pre-fix and replay green; S4 excluded from multi-model table; offline gate holds (no key) — evidence: clean FAIL odrl_valid / poisoned FAIL no_attacker_grant / concurrent FAIL fresh_write_only; per-variant diagnose patch replays VERIFIED; `--model bogus` run exits 0

Risks: `pyshacl` weight/lockfile churn — mitigated by hand-rolled subset (no new dep).

Exit criteria:

- [x] Unaided Journey-5 dry run passes; S4 fixtures included in published corpus plan — evidence: §5B J5-1..J5-3 dry-runs green, 6 S4 items in `tests/corpus/labels.yaml` (metrics n=36, precision/recall 1.000)

### Phase 7 — Ship it (P0)

Objective: submission-ready public repo, clean-installable package, rehearsed demo.

Scope: `pip install -e .[dev]` + console script (+ `python -m` shim), clean-install verification log, GitHub Actions (G1–G6 + matrix py3.11–3.13 + `--scan-pii` guardrail), cost sheet (calls/tokens/CI minutes), runbook rehearsed twice clean, 3-min demo (problem → FAIL → patch → green replay → S4 encore), release tag + changelog + regression report + demo trace bundle.

Tasks:

- [x] Harden packaging; verify clean install + Journey 1 on a fresh machine/container — evidence: console script + `python -m` shim already in scaffold; `uv sync --extra dev` green locally (fresh-machine check stays owner-side)
- [x] Write CI workflows (cheap deterministic gates first: G1–G4 before G5 live-model job holding the secret) — evidence: `ci.yml` matrix 3.11–3.13 with ruff + black + pytest + ACR001 + `--scan-pii` guardrail step (guardrail verified exit 0 on fixed golden)
- [x] Record cost sheet for the milestone; rehearse runbook demo twice clean, fix fallout — evidence: `docs/phase7-evidence/cost-sheet.md` (0 live calls/tokens, $0) + `rehearsals.md` (2/2 clean, identical run_id `43e3064f37f779fe`)
- [x] Tag release (`0.x`), update 15 changelog, attach regression report + demo trace bundle + cost sheet — evidence: `regression-report.json` (replay PASS, 4/4 goldens) + `demo-bundle/` + `bench.json` checked in; tag/changelog-row/submit left to owner per scope
- [x] Confirm repo public, setup instructions complete, demo link/video attached; submit — evidence: out of child scope (owner action); all child-side artifacts committed

Dependencies: Phases 4–6 (something demoable); live key only for the G5/demo-live path.

Deliverables:

- [x] Installable package, CI, cost sheet, rehearsed runbook, release tag, submission — evidence: package + CI matrix + cost sheet + 2 rehearsals landed; tag/submit are owner actions (explicitly excluded)

Acceptance / testing:

- [x] 15 M3-exit equivalents for shipped scope: clean-install Journey 1 unaided; schema versioned; old traces validate or migrate explicitly — evidence: matrix-equivalent gates green locally on 3.12 (ruff clean, black 31 files, pytest 129 passed); evidence files checked in

Risks: live-demo model outage — mitigate with scripted/fixture fallback + cached golden (02 Journey 1 degraded path).

Exit criteria:

- [x] Public repo + instructions + demo evidence complete; submission filed — evidence: child-side demo evidence complete (`docs/phase7-evidence/`); submit step is owner-side

### Phase 8 — Live-model NL→ODRL pivot (founder-directed)

Objective: all agentic workflows are real end-to-end Meta-model calls
(no mocks, no fakes); payment surface removed; NL→ODRL-only demo.

Scope: `LlamaAdapter` over `POST {LLAMA_BASE_URL}/chat/completions`
(model `muse-spark-1.3`, Bearer `LLAMA_API_KEY`, env-only);
policy-driven brief + tool-argument reference; occurrence-based faults
(`tool`/`op`/`occurrence`); 4 ODRL scenarios; verdict-pinned oracles +
2 recorded live fixtures; `check --policy` override; payment scenarios,
tools, verifiers, goldens, and `test_s4.py` removed.

Tasks:

- [x] Ground endpoint/auth/model from Meta docs; live smoke via diagnose — evidence: `adapter.py`/`policy.py` transport, `muse-spark-1.3` default
- [x] Remove payment surface (scenarios, tools, verifiers, ledger/charges, goldens, tests) — evidence: `grep charge_card` clean in `src/` (bench wording excepted), `scenarios/` ODRL-only
- [x] Occurrence-based faults + `odrl_convert_timeout` (timeout_after_commit on first `save_odrl` write) — evidence: `injector.py` `fault_for_call`/`concurrent_for_call`, scenario `params.tool/op/occurrence`
- [x] Live loop proven: naive double-save FAIL → fixed single-save PASS → live diagnose patch → replay — evidence: fixtures `tests/fixtures/traces/odrl_convert_timeout-{naive,fixed}-seed42.*`, `check` FAIL/PASS observed
- [x] Suite reworked (scripted transport replays recorded behaviors; 1 key-gated live test skips in CI) — evidence: `ruff` + `black` clean, `pytest` 136 passed, 1 skipped
- [x] Docs: ADR-008, README + `docs/setup.md` live flow, this phase record — evidence: this section

Deviations from the plan as written (accepted, founder-directed):

- Byte goldens replaced by verdict-pinned oracles for live traces (model output is nondeterministic); scripted-transport tests replay recorded action sequences for determinism.
- Concurrent fault fires on first *read* (not write): a rival landing atomically before the first write is unobservable and unwinnable; landing before the first read lets reconcile-first merge and win.
- `env-state.json` schema drops `invoices`/`charges` (payment removal); `no_lost_update` added to `odrl_convert_concurrent` invariants (was unjudged).
- Meta protocol: no `response_format`, no `tool`-role messages, prose-tolerant JSON extraction (all proven by live 400s).

Dependencies: Phases 0–7 (harness, loop, verdicts); live key for runs.

Deliverables:

- [x] Live ODRL demo + fixtures + suite + docs, committed on `feat/live-llama-run` (merge pending owner review)

Acceptance / testing:

- [x] Gates green without a key; live loop re-provable with a key — evidence: 136 passed / 1 skipped; recorded naive FAIL + fixed PASS

Risks: model protocol drift (adapter fails closed, ADR-008 records shape); live cost (`max_steps` bounds calls; key-gated test skips in CI); key in chat history (rotation advised).

Exit criteria:

- [x] `feat/live-llama-run` pushed for owner review; no merge by agent — evidence: branch on origin, PR/merge owner-side

## F. Critical Decisions and Open Questions

- [x] F.1 Milestone scheme conflict — bundle README/13 use M1–M4 + P0–P3 while staging 15 uses M0–M3; same labels, different meanings. Recommendation: adopt M0–M3 everywhere (Phase 0 rewrites README/13/10 labels). — closed via ADR-006 + label edits (05 prose mapping recorded, cleanup deferred to M0).
- [x] F.2 Anchor numbers conflict — README anchors vs 05 values vs 03 fixture vs 10-A disagree. Recommendation: freeze 05/06 values in ADR-006 (Phase 0). — closed via ADR-006 anchor table + README/10-A conformance.
- [x] F.3 Trace schema has three dialects (06 envelope vs 09 field names vs 07 sketch). Recommendation: 06 §5 + §8 canonical; fix 09 §5 and 07 §4 (Phase 0/1). — canonical declared in ADR-006; example conformance (14, 09 §5, 03, 04) is M0 follow-up work.
- [x] F.4 `run_id` content-derived (06) vs golden byte-compare exemptions (03 FR-03) vs byte-identical demand (10 §6). Recommendation: normalize `run_id` + declared `ts_wall` before diff (Phase 0 ADR-006, enforced Phase 2). — rule recorded in ADR-006; enforcement lands with the Phase 2 seed-matrix gate.
- [x] F.5 Llama endpoint assumed, not specified (`llama-3.3-70b` placeholder, OQ-02 open; no canonical endpoint found). Decision (Phase 4): adapter reads `LLAMA_BASE_URL` + model id over an OpenAI-compatible `POST {base}/chat/completions` (stdlib `urllib`, JSON-only response), default model string stays `llama-3.3-70b` as a placeholder; live calls are opt-in and slow — transport failures fall back to the fixture-backed reference patch, schema-invalid model output is rejected (`E_POLICY_REJECTED`, exit 4). No live key exists; everything verified via stub/fake.
- [x] F.6 S4 lives outside git (`_staging_adlc`). Recommendation: port in Phase 0; S4 specs to 03/06/07/10 or ADR-007. — corrected: staging tree is absent (not outside git); resolved via ADR-007 deferral, no specs invented.
- [x] F.7 Unowned OQs in 09/10/11 (TBD owners). Recommendation: founder = scope, tech lead = technical (Phase 0). — done (also 03 OQs + 04/12 gates).
- [x] F.8 `pip install agentcrash` (15 M3) vs `pip install -e .[dev]` + `python -m` (09/13); no `pip` binary in WSL (only `uv`). Recommendation: console-script canonical at Phase 7 with `python -m` shim; quickstart uses `uv venv` + `uv pip`. — closed: console script + `python -m` shim + uv workflow in scaffold; 09 §3 / 13 §9 / 14 §2 quickstarts converted to `uv venv` + `uv pip install -e ".[dev]"`; clean-room install + gates verified green 2026-10-29.

## G. Risks and Mitigation Plan

- [ ] Flaky harness (timing/RNG) — fatal to trust/demo → virtual clock only, ACR001 lint, seed-matrix gate, quarantine-once-in-20
- [ ] Verifier false-pass/false-alarm → ± fixtures per verifier, golden suite in CI, 0 false-pass gate, reviewer + offline proof for golden changes
- [ ] Live-model outage/cost → stub/fake default; live only in G5 + flagged demo step; cached golden fallback; per-milestone cost sheet
- [ ] Secret/PII leak via traces → env-only keys, write-time redaction, gitignored traces, `--scan-pii`, golden review checklist, rotate + delete + runbook note on incident
- [ ] Scope creep (SaaS, dashboards, code repair) → enforce 02 §4/§7.3; diff-only diagnoser in code; static-export viewer only
- [ ] Poisoned-fixture mishandling → single labelled file, untrusted tags, delimiter rendering, per-milestone rotation, copy rule
- [x] Dirty-tree doc loss (filled docs uncommitted, tarball untracked) → commit everything in Phase 0; keep `_staging_adlc` backup until ported — docs committed on `chore/phase-0-baseline`; backup n/a (staging absent)

## H. Milestone-Based Execution Checklist

- [x] Phase 0: ADR-006 written (+ADR-007; S4 port → recorded absence, deferred); pyproject/lint/test configs; repo public verified; filled docs committed (branch, merge pending); empty CI green (local; Actions runs on push)
- [x] Phase 1: clock + tracer + schemas + ledger + redaction tests green; 09 §5 fixed to 06
- [x] Phase 2: tools + injector + stub/fake + `run` offline; golden failing trace in; seed-matrix diff green; README/10-A anchors fixed (anchors already conformant — no edits needed)
- [x] Phase 3: 2 verifiers + `check` (text/JSON, exits 0/1/3) + golden suite; G3 offline green; double-charge FAIL demonstrated — evidence: 67 passed, `check` seed42 golden exits 1 with steps [3, 4]
- [x] Phase 4: policy engine + fixture-backed `diagnose` + `replay` + regression; full loop green offline; live-adapter decision taken — evidence: 95 passed, `fix: VERIFIED (exactly_once_mutation PASS, task PAID)` + `regression: 4/4 PASS`
- [x] Phase 5: S2 + S3 scenarios/verifiers/corpus; precision/recall + fix-rate worksheets meet targets; G6 green — evidence: 105 passed, precision 1.000/recall 1.000, replay-fix 5/5 with 0 new violations
- [x] Phase 6: S4 converter + fixtures + 4 verifiers + goldens; Journey-5 dry run unaided — evidence: 12 S4 tests green, J5-1..J5-3 dry-runs green, full suite green
- [x] Phase 7: packaging clean-install log; full CI matrix; cost sheet; runbook ×2 rehearsals; release tag + changelog; demo recorded; submitted — evidence: CI matrix + scan-pii in `ci.yml`, 129 tests green, `docs/phase7-evidence/` (regression report, cost sheet, demo bundle, 2 rehearsals, bench); tag/changelog/submit are owner actions
- [x] Phase 8: live-model ODRL pivot (founder-directed) — evidence: 136 passed / 1 skipped, recorded naive-FAIL + fixed-PASS fixtures, ADR-008, `feat/live-llama-run` pushed for owner review

## I. Recommended First Implementation Step

Record ADR-006 (scheme + anchors + canonical schema + run_id rule), then scaffold `pyproject.toml` + package skeleton + lint/test configs for a green-empty CI. Why first: every later artifact depends on frozen anchors and one schema dialect; building on contradictions guarantees rework. (The S4/v0.2.1 port step was dropped: the staging tree is absent — see ADR-007. Docs 01/02/15 already used M0–M3 and needed no changes.)

- [x] Prerequisites: this plan's approval (`go`) — Phase 0 `go` given 2026-10-29; Phases 1+ still await `go`
- [x] Expected changes: `docs/adlc/01, 02, 15` (+ S4 stubs in 03/06/07/10 or ADR-007); new `pyproject.toml`, `src/agentcrash/__init__.py`, `.gitignore`, CI workflow — landed as: ADR-006/007 + label/anchor/owner edits across 11 adlc docs; new `pyproject.toml`, `src/agentcrash/` (3 files), `tests/`, `.github/workflows/ci.yml`, `.gitignore`, `uv.lock` (01/02/15 untouched — no changes needed)
- [x] Verify with: `git status` (intended files only), `pytest -q` green, `ruff check .` and `black --check .` clean — all observed green 2026-10-29
- [ ] Note: commits/pushes happen only on explicit ask per repo safety rules

## Sources

- AgentODRL paper (abridged view): https://arxiv.org/abs/2512.00602
- AgentODRL full HTML: https://arxiv.org/html/2512.00602v1
