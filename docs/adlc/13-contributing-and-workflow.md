# 13 — Contributing and ADLC Workflow

**Status:** Approved · **Owner:** Tech lead · **Milestone:** M1 Foundations
**Lens:** ADLC (AI/Agentic Development Lifecycle) for a startup idea — problem, users,
MVP, risks, costs; no corporate bureaucracy, no day-wise timelines.

## 1. Principles

1. Evidence over opinion. A claim about agent behavior ships with a trace, a test, or a
   benchmark number — never a bare assertion.
2. Determinism first. Anything that affects a verdict (verifiers, clocks, seeds) must be
   reproducible from checked-in inputs.
3. Small slices, gated by milestones. Work lands per milestone (M1–M4); nothing is
   scheduled by calendar day.
4. The model proposes, code disposes. LLM-generated diagnoses and patches are always
   validated by deterministic checks before they count.
5. Startup frugality. Prefer the smallest change that closes the loop; track compute
   cost (model calls, CI minutes) on every milestone review.

## 2. Branch and PR conventions

### Branch naming

| Prefix | Use | Example |
|---|---|---|
| `feat/` | New capability or scenario | `feat/stale-read-verifier` |
| `fix/` | Bug fix | `fix/idempotency-key-reuse` |
| `docs/` | ADLC docs only | `docs/runbook-demo-script` |
| `chore/` | Tooling, CI, deps | `chore/ruff-config` |
| `exp/` | Throwaway spike (never merges directly) | `exp/llama-latency-probe` |

Rules: one concern per branch; branch from `main`; rebase onto `main` before review;
delete after merge.

### Commit conventions (Conventional Commits)

Format: `type(scope): short imperative summary`, e.g. `feat(tracer): validate event
schema at write time`. Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.
Every commit that changes behavior must reference the milestone: `Refs: M2`.

### Pull request process

1. Open a PR with the template below; keep it under ~400 changed lines where possible.
2. Required checks: test suite green, linters clean, trace fixtures updated if behavior
   changed, docs touched if user-visible behavior changed.
3. Required reviewers: one human owner (tech lead or delegate). Agent review is
   advisory only and never satisfies the reviewer requirement.
4. Agent-generated work: the PR description must state which parts are agent-generated
   and what evidence the human verified (tests run, traces inspected).
5. Merge method: squash-and-merge; the squash message follows commit conventions.

PR template:

```markdown
## Goal (one paragraph)

## Milestone: M1 | M2 | M3 | M4

## Changes

## Evidence (tests, traces, numbers)

## Agent-generated sections (if any) + human verification performed

## Risks and rollback
```

## 3. Milestone-gated ADLC flow (no dates)

Milestones are exit-gated: the next milestone starts only when the current gate is met.
There are no day-wise estimates anywhere in planning or reporting.

| Milestone | Goal | Exit gate (all must hold) |
|---|---|---|
| M1 Foundations | Repo, harness skeleton, ADR-001–003, trace schema v1 | `run` emits valid JSONL for the happy path; schema validator passes; docs 01–08 approved |
| M2 Prototype Slice | Full timeout exactly-once loop | `run → check → diagnose → replay` green on invoice #9821; double-charge detected pre-fix; no double-charge post-fix; docs 09–14 approved |
| M3 Scenario Expansion | Poisoned-doc + stale-read scenarios | New verifiers with positive/negative fixtures; regression suite green; ADR-006+ recorded |
| M4 Hardening + Demo | Reliable public demo | Wall-clock soak parity report; runbook demo executed twice without intervention; cost sheet published |

Change control inside a milestone: scope changes need owner approval in the PR or the
roadmap doc (15); ADR changes follow Section 6.

## 4. Definition of Done

A task is done only when all of these hold:

- [ ] Acceptance criteria from the issue or milestone plan are met.
- [ ] Tests added or updated; full suite green (`pytest -q`, see doc 10).
- [ ] Deterministic behavior: same seed gives same result across two consecutive runs.
- [ ] Trace fixtures updated if tool-call behavior changed; `trace validate` passes.
- [ ] Linters clean (Ruff + Black, see Section 7); no new warnings.
- [ ] Docs updated: ADLC doc touched if the change alters behavior, contracts, or ops.
- [ ] Security and failure cases considered (Section 5 checklist).
- [ ] Cost noted: model calls and approximate tokens added to the PR evidence section.
- [ ] Human review completed for any agent-generated code or diagnosis.

## 5. Review checklist (paste into PRs)

- [ ] Change meets its task acceptance criteria and milestone scope.
- [ ] Requirements and interfaces remain consistent (no silent contract drift).
- [ ] Relevant checks and manual verification are documented with commands and outputs.
- [ ] Security, privacy, and failure cases considered: prompt-injection surface (tool
  outputs treated as untrusted data), least-privilege tool access, confirmation gates
  on mutating tools, no secrets in traces or logs.
- [ ] Failure-mode coverage: timeout/ambiguity, poisoned content, concurrent update —
  whichever apply — have a test or an explicit non-applicability note.
- [ ] User-facing behavior and operational implications described (CLI output changes,
  new flags, runbook impact).
- [ ] No dates or day-wise promises introduced in code, docs, or comments.

## 6. Change management

| Change type | Approval | How |
|---|---|---|
| Requirement wording (doc 03) | Product lead | PR against the doc; link the motivating trace or user evidence |
| New or superseding ADR | Tech lead | New ADR file section; old ADR marked Superseded, never edited |
| Policy schema change | Tech lead + backend engineer | Schema PR with migration note and updated `policy diff` golden files |
| Trace schema version bump | Tech lead | New `schema_version`; validator accepts old + new for one milestone |
| Release / version tag | Founder | See Section 8; changelog entry in doc 15 |

## 7. Coding standards

- **Runtime:** Python 3.11+. Type hints required on all public functions; `mypy --strict`
  clean on `agentcrash/` package.
- **Style:** Black (line length 100) + Ruff. Run `ruff check .` and `black --check .`
  before pushing. Docstrings on every module and public class (Google style).
- **Testing:** pytest. Every verifier and injector needs positive and negative fixture
  tests under `tests/fixtures/`. Property: same seed → byte-identical trace (assert on
  `trace_id`-normalized JSONL). See doc 10 for the full strategy.
- **Clocks:** Production code must use the injected `VirtualClock`; direct calls to
  `time.sleep()`, `time.time()`, or `asyncio.sleep()` in tools/injectors are lint
  errors (custom Ruff rule `ACR001`). Wall-clock allowed only in explicitly marked
  soak-mode modules.
- **Secrets:** No API keys in code, traces, or fixtures. Model access via environment
  variables (`LLAMA_API_KEY`, `LLAMA_BASE_URL`); tests use recorded fakes.
- **Traces as test data:** Fixture traces live in `tests/fixtures/traces/*.jsonl` and are
  reviewed like code. Never commit a trace containing real credentials or PII.
- **Costs:** Keep the suite cheap: default test run uses the fake model adapter; live
  Llama calls are opt-in (`--model llama-3.3-70b`) and labeled `slow`.

## 8. Release and versioning

- Versioning: SemVer (`v0.1.0`, `v0.2.0`, …). Prototype stays `0.x` until M4 exit.
- Each release tags `main`, updates doc 15 changelog, and attaches: (a) the regression
  suite report, (b) the demo trace bundle, (c) the cost sheet (model calls, tokens,
  CI minutes for the milestone).
- Rollback of a release: re-tag the previous green commit; policy files are
  backward-compatible within a major version, so a code rollback never needs a trace
  migration inside the same milestone.

## 9. Local development quickstart

```bash
# inside WSL, repo root /home/sahil/AgentCrash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && black --check . && pytest -q
python -m agentcrash run --scenario timeout_retry --model fake
python -m agentcrash check --trace traces/latest.jsonl
```

## 10. Startup cost discipline

Every milestone review answers: model calls used, tokens spent, CI minutes consumed,
and whether the fake adapter could have covered more. If live-model spend for a
milestone exceeds the budget set in doc 01 (planning), the overrun needs a one-line
justification in the changelog. Frugality is a review criterion, not an afterthought.
