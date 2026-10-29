# 12 — Non-Functional Requirements (NFRs)

**Status:** Draft · **Owner:** Startup founder (Open Question OQ-01 in [03](03-requirements-and-srs.md): confirm owner) · **Phase gate:** Phase 1 (Prototype v0) entry

> ADLC lens: these targets keep a laptop-run startup prototype honest — reproducibility first
> (a chaos harness that flakes is worthless), tiny budgets (no infra spend before traction),
> and a demo anyone can follow. Every NFR names a metric, a concrete target, the workload it
> holds under, and how to verify it. Traceability back to functional requirements in
> [03](03-requirements-and-srs.md) is explicit. No day-wise timeline is used; NFRs gate
> phases, not dates.

## 1. Determinism & reproducibility (must-meet for Phase 1)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-DET-01 | Fault-schedule equality across reruns | 100% identical fault steps, types, latencies for same (scenario, seed, policy) | `timeout_retry`, seed 42, 5 consecutive runs, stub agent | `diff <(normalize run1.jsonl) <(normalize run2.jsonl)` empty ignoring `run_id` + wall-clock fields; `pytest tests/test_determinism.py` | P0 |
| NFR-DET-02 | Trace byte-equality (modulo IDs) | Same as DET-01 extended to all event payloads + virtual timestamps | Same workload; plus seed sweep 1…20 | Rerun-diff over the seed sweep; CI fails on any mismatch | P0 |
| NFR-DET-03 | Verifier output stability | Byte-identical JSON report for 3 checks of the same trace, incl. one with network disabled | Golden failing + golden passing traces | `check --format json` × 3, `sha256sum` equal; covers FR-16 | P0 |
| NFR-DET-04 | Virtual-clock purity | Zero wall-clock reads in `tools/`, `injector/`, `tracer/` (static) + virtual timestamps monotonic | Full v0 codebase | `grep -rn 'time.time\|Date.now\|monotonic' tools injector tracer` empty in review; `seq`/`virtual_ts_ms` monotonicity asserted per trace | P0 |

Grounding: deterministic-simulation practice (single seeded PRNG as the only entropy source,
one virtual clock as the only time source — the same discipline FoundationDB-style DST
harnesses use) applied at startup scale: one seed reproduces one universe.

## 2. Performance budgets (laptop-grade)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-PERF-01 | Harness overhead vs bare agent loop | ≤ 10% added wall-clock per run | 12-step stub run, timeout_retry, measured over 10 runs on a 2-core laptop | `bench/run_overhead.py` reports mean overhead; Phase 1 gate | P0 |
| NFR-PERF-02 | Trace size | ≤ 2 MB for a 100-step run; ≤ 20 KB per event average | Synthetic 100-step stress scenario (stub agent, max_steps 100) | `wc -c` on produced trace; gate fails above budget | P0 |
| NFR-PERF-03 | `check` latency | ≤ 2 s wall-clock on the largest golden trace | Golden suite on reference laptop, cold cache | `time agentcrash check --trace <largest>`; CI perf smoke | P1 |
| NFR-PERF-04 | `replay` bounded cost | ≤ 2× single-run cost (patched run + control run, shared setup) | timeout_retry seed 42 + control scenario | Timed replay script; reported in demo notes | P1 |

Rationale (startup costs): no budget for runners bigger than a laptop or a free CI tier, so
the harness must be near-free next to the model bill. Model latency is excluded from every
budget — only harness time counts.

## 3. Reliability (correctness of the harness itself)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-REL-01 | Verifier false-pass rate | 0 false passes on the golden suite (all seeded double-charge, give-up, and clean traces) | `tests/golden/` (≥ 6 traces: 2 failing each invariant, 2 passing) | Golden suite must be 100% green; any false pass blocks Phase 1 exit | P0 |
| NFR-REL-02 | Verifier false-fail rate | 0 false fails on golden passing traces | Same suite | Same run; a fix that breaks the control scenario must FAIL (proves the check is live) | P0 |
| NFR-REL-03 | Fail-closed behavior | 100% of invalid inputs (unknown fault type, bad patch schema, missing trace) exit non-zero with no partial artifacts | Fault-injection matrix incl. FR-07/08/22 cases | `pytest tests/test_fail_closed.py`; assert no trace/patch file created | P0 |
| NFR-REL-04 | No secret leakage | 0 occurrences of planted canary secrets in any emitted trace/report across the suite | Canary `canary_token` value injected into tool args in a test run | Grep traces + reports for canary; covers FR-12 | P0 |

## 4. Portability & compatibility

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-PORT-01 | OS support | Linux, macOS, Windows-via-WSL: full FR-01…FR-25 acceptance on each | CI matrix (3 runners) or documented manual run per OS | Matrix green for Phase 1 exit; manual log otherwise | P0 |
| NFR-PORT-02 | Python support | 3.11, 3.12, 3.13 with no code changes | Same matrix, `python --version` variants | CI matrix; `pyproject.toml` `requires-python = ">=3.11"` | P0 |
| NFR-PORT-03 | Dependency footprint | ≤ 5 runtime deps (PyYAML, jsonschema, llama client, click/argparse stdlib, pytest for dev) | Fresh `pip install` from lockfile | `pip freeze` audit in review; offline stub path needs zero third-party calls | P1 |
| NFR-PORT-04 | Offline reproducibility | Stub-agent golden rerun passes with loopback networking disabled | `timeout_retry` stub plan, seed 42 | Unplugged/namespace-blocked CI job green | P0 |

## 5. Observability (every verdict explainable)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-OBS-01 | Verdict-to-evidence linkage | 100% of FAIL verdicts cite ≥ 1 offending step (`call_id` + `seq`) | All golden failing traces | Audit script over JSON reports; covers FR-13/25 | P0 |
| NFR-OBS-02 | Decision coverage | 100% of retries/denials backed by a `policy_decision` event naming rule + policy hash | timeout_retry naive + fixed runs | Trace query: every 2nd+ `tool_call` for a mutating tool has a preceding decision | P0 |
| NFR-OBS-03 | Report completeness | Every report (text + JSON) carries invariant table, side-effect count, task completion, run/seed/policy hashes | `check` + `replay` outputs | Schema-validated report fixtures; covers FR-25 | P0 |
| NFR-OBS-04 | Human skim time | Founder can state what broke, where, and the proposed fix in ≤ 2 min from the text report | Timed dry-run with a fresh reader | Stopwatch + checklist; part of demo rehearsal | P1 |

## 6. Usability (5-minute fundable demo)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-USE-01 | End-to-end demo time | run (FAIL) → check → diagnose → replay (PASS) completes in ≤ 5 min wall-clock | timeout_retry, stub or cached-LLM diagnose, reference laptop | Timed `demo.sh` rehearsal; Phase 1 gate | P0 |
| NFR-USE-02 | CLI self-description | 100% of commands/flags used in the demo appear in `--help` with an example | `run`, `check`, `diagnose`, `replay` | Help-text review checklist; covers FR-23 | P0 |
| NFR-USE-03 | First-run success | New clone → first green control run in ≤ 15 min following README only | Clean checkout, Python 3.11+, stub agent (no API key) | Fresh-reader trial; gaps filed as doc bugs | P1 |
| NFR-USE-04 | Error actionability | 100% of fail-closed errors name the bad value + valid options | Unknown fault, bad patch, missing trace/seed | Error-message audit against FR-07/08/22 outputs | P1 |

## 7. Maintainability & security posture (startup-scale)

| ID | Metric | Target | Workload / conditions | Verification method | Priority |
|---|---|---|---|---|---|
| NFR-MNT-01 | Verifier isolation | Each invariant is one file + one golden pair; adding an invariant touches no existing verifier | Phase 2 scenario authoring (poisoned-doc dry run) | Review: new `verifiers/<id>.py` + 2 goldens, zero edits elsewhere | P1 |
| NFR-MNT-02 | Diagnoser blast radius | Diagnoser output validates against `policy-patch.schema.json`; non-conforming output rejected before display | Malformed-patch fixture battery | `diagnose` fixture tests; covers FR-17/22 | P0 |
| NFR-SEC-01 | Secret hygiene | No API keys in traces, reports, goldens, or committed fixtures (canary-tested) | Full repo + generated artifacts | Canary grep in CI (see NFR-REL-04); `.env`-only credentials | P0 |
| NFR-SEC-02 | Untrusted-content discipline (forward) | Poisoned-doc fixtures, when added, are trust-tagged + delimited and never executed as instructions | Phase 2 authoring | Design review against prompt-injection guidance (spotlighting/delimiters, confirmation gates) | P1 |

Security note: the LLM is never a trust boundary in this design — verification is
deterministic (Tracer + Verifiers) and the Diagnoser can only propose diffs. Layered
defenses for untrusted content (delimiters, trust tags, confirmation gates, least-privilege
tools) follow current indirect-prompt-injection practice and land with Phase 2 scenarios.

## 8. Prioritization, tradeoffs, and cost ceiling

Must meet before Phase 1 exit (demo/fundraising gate): NFR-DET-01…04, NFR-PERF-01/02,
NFR-REL-01…04, NFR-PORT-01/02/04, NFR-OBS-01…03, NFR-USE-01/02, NFR-MNT-02, NFR-SEC-01.

Accepted temporary limitations: PERF-03/04 and USE-03/04 are rehearsed manually, not
CI-enforced; PORT-03 allows one extra dep on justification; MNT-01/SEC-02 are Phase 2
design constraints recorded here, not v0 code.

Cost ceiling (startup lens): $0 infra — harness runs on a laptop + free CI tier; the only
spend is Llama API calls for live-agent demos (bounded: ≤ 12 steps × ≤ 3 runs per rehearsal).
Owner approving exceptions: founder (OQ-01).

## 9. Worked verification plan (how Phase 1 exit is proven)

Concrete commands the founder runs to show every P0 NFR green. Expected values are exact —
any deviation fails the gate.

1. Determinism drill (DET-01/02/03): run `agentcrash run --scenario timeout_retry --seed 42`
   five times with the stub agent; `normalize()` strips `run_id` and wall-clock fields; all
   five diffs empty. Then `agentcrash check --format json` on the golden failing trace three
   times (third run with loopback disabled); `sha256sum` identical thrice. Purity grep over
   `tools/`, `injector/`, `tracer/` returns empty (DET-04).
2. Budget drill (PERF-01/02): `bench/run_overhead.py` reports mean harness overhead 4–8%
   (target ≤ 10%) on 10 stub runs; the 100-step stress trace weighs ~1.1 MB (target ≤ 2 MB).
   Both numbers print into the Phase 1 exit note.
3. Harness-correctness drill (REL-01…04): `pytest tests/golden/` — 6 traces minimum (double
   charge ×2 seeds, give-up ×1, clean pass ×2, truncated ×1): 0 false passes, 0 false fails.
   Fail-closed matrix: unknown fault, future fault type, malformed patch, missing trace —
   all exit 2 with no artifacts. Canary sweep: planted `canary_token` value absent from every
   trace and report.
4. Portability drill (PORT-01/02/04): CI matrix (Linux + macOS + WSL, Python 3.11–3.13)
   runs the stub golden suite; one job repeats it with networking disabled. Fresh-install
   check: `pip install -e .` on each runner pulls ≤ 5 runtime deps (PORT-03, P1).
5. Demo drill (OBS-01…03, USE-01/02): `demo.sh` executes the four-command narrative in
   ≤ 5 min: run (FAIL with steps 3–4 cited) → check (JSON validates) → diagnose (patch
   contains `retry_with_idempotency_key` + reconcile) → replay (`fix: VERIFIED, regression:
   PASS`). A fresh reader states breakage, location, and fix from the text report in ≤ 2 min
   (OBS-04, P1). Every demo flag exists in `--help` output (USE-02).
6. Guardrail drill (MNT-02, SEC-01): feed `diagnose` three malformed patch fixtures —
   all rejected before display; canary grep across the repo (committed files) clean.

Exit rule: all six drills green ⇒ Phase 1 exits. Any red drill ⇒ the phase stays open; the
founder records the exception approver (self) and the compensating milestone in the exit
note — no silent waivers.

## 9a. Sizing note: why the trace budget holds (PERF-02 arithmetic)

A generous v0 event averages ~1.1 KB (args, timestamps, hashes — measured on the golden
timeout trace at ~900 bytes/event). Budget check:

| Run length | Bytes at 1.1 KB/event | Headroom under 2 MB |
|---|---|---|
| 12 steps (~40 events) | ~44 KB | 45× |
| 100 steps (~330 events) | ~360 KB | 5.5× |
| 500 steps (~1,650 events) | ~1.8 MB | still inside |

Even a 5× event-size regression (verbose thoughts, long args) keeps the 100-step stress
run under budget. If a future phase adds token-level thought logging, this table — not the
2 MB target — gets revised first, with the founder approving the new ceiling (OQ-01).

## 10. Change history

| Milestone | Change | Author |
|---|---|---|
| Phase 0 draft | NFR skeleton with placeholder targets | Pre-Phase-1 skeleton (author unrecorded; OQ-01) |
| Phase 1 draft (this file) | Full NFR set DET/PERF/REL/PORT/OBS/USE/MNT/SEC with metric + target + verification; all TBDs replaced except owner | Group B |

