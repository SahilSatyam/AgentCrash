# 13 - Contributing and ADLC Workflow

**Status:** Revised for 0.3.0 · **Owner:** Tech lead · **Milestone scheme:** M0-M3 (ADR-006)
**Lens:** ADLC (AI/Agentic Development Lifecycle) for a startup project: problem, users,
MVP, risks, costs. No corporate bureaucracy, no day-wise timelines.

## 1. Principles

1. Evidence over opinion. A claim about agent behavior ships with a trace, a test, or a
   benchmark number, never a bare assertion.
2. Determinism first. Anything that affects a verdict (invariants, clock, seed) must be
   reproducible from checked-in inputs.
3. Small slices, gated by milestones. Work lands per milestone (M0-M3, ADR-006) and maps
   to roadmap items R1-R7; nothing is scheduled by calendar day.
4. The model proposes, code disposes. A model may only propose a schema-valid
   `policy_patch/v1`; deterministic invariants own every verdict (ADR-001, ADR-004).
5. Domain-free core (ADR-009). Host- or domain-specific code lives in the host, not in
   `src/agentcrash/`.
6. Frugality. Prefer the smallest change that closes the loop. The suite and the demo loop
   are offline and free; keep them that way.

## 2. Branch and PR conventions

### Branch naming

| Prefix | Use | Example |
|---|---|---|
| `feat/` | New capability | `feat/fault-catalog-v2` |
| `fix/` | Bug fix | `fix/prune-age-cutoff` |
| `docs/` | Docs only (including ADLC) | `docs/runbook-refresh` |
| `chore/` | Tooling, CI, deps | `chore/ci-windows-runner` |
| `perf/` | Performance work with bench evidence | `perf/tracer-redaction` |
| `exp/` | Throwaway spike (never merges directly) | `exp/otel-export-probe` |

Rules: one concern per branch; branch from `develop` (the repo's default branch);
rebase onto `develop` before review; delete after merge. Release tags come from `main`
(section 8). Never push directly to `main`.

### Commit conventions (Conventional Commits)

Format: `type(scope): short imperative summary`, for example
`fix(server): prune runs by age on coarse clocks`. Types: `feat`, `fix`, `docs`, `test`,
`refactor`, `perf`, `chore`. Breaking changes use `!` (as in `refactor!:`) plus a
migration note in `CHANGELOG.md`. Behavior-changing commits may add a trailer such as
`Refs: M1` or `Refs: R2`.

### Pull request process

1. Open a PR with the template below; keep it near 400 changed lines or fewer.
2. Required checks: the four gates in section 4 are green; docs touched if
   user-visible behavior changed.
3. Required reviewer: one human owner (tech lead or delegate). Agent review is advisory
   and never satisfies the requirement.
4. Agent-generated work: the PR description states which parts are agent-generated and
   what the human verified (commands run, output read).
5. Merge method: squash-and-merge; the squash message follows the commit conventions.

PR template:

```markdown
## Goal (one paragraph)

## Milestone: M0 | M1 | M2 | M3   (and roadmap item R1-R7 if any)

## Changes

## Evidence (commands and output: pytest, ruff, black, bench if relevant)

## Agent-generated sections (if any) + human verification performed

## Risks and rollback
```

## 3. Milestone-gated ADLC flow (no dates)

Milestones are exit-gated: the next one starts only when the current gate is met. The
M0-M3 scheme stays as defined in ADR-006; the old per-milestone scenario and `diagnose`
gates were retired with ADR-009. Current mapping:

| Milestone | Goal | Exit gate (all must hold) | Roadmap items |
|---|---|---|---|
| M0 Baseline | Domain-free core, offline loop, docs honest | Four gates green on py3.11-3.13; setup-guide demo loop gives FAIL then PASS; NFR-PERF-01 met (`bench.json`) | 0.3.0 scope, R7 hygiene |
| M1 Coverage | Framework adapters, richer faults and invariants | Adapter examples run end to end offline; new faults and invariants have positive and negative tests | R1, R2, R3 |
| M2 Measurement | Reliability metrics and standard traces | Metrics reproducible from stored runs; trace export validated | R4, R5 |
| M3 Closing the loop | Host-supplied diagnose and replay; public release | Runner contract documented and tested; release checklist done | R6, R7 |

The roadmap (`docs/plans/2026-11-14-sota-roadmap.md`) is a draft: implement a roadmap
item only after the owner replies `go`.

Change control inside a milestone: scope changes need owner approval in the PR or the
roadmap doc (doc 15); ADR changes follow section 6.

## 4. Definition of Done

A task is done only when all of these hold:

- [ ] Acceptance criteria from the issue or milestone plan are met.
- [ ] Tests added or updated; the gates below pass.
- [ ] Deterministic behavior: same input and seed give the same trace content and verdict.
- [ ] Domain-free: `tests/test_generic_core.py` passes without edits to its banned list.
- [ ] Docs updated: README, `docs/setup.md`, or the ADLC doc touched if behavior,
      contracts, flags, or ops changed; `CHANGELOG.md` updated for user-visible change.
- [ ] Security and failure cases considered (section 5).
- [ ] Perf-sensitive change (`sdk.py`, `tracer.py`, `events.py`): `bench/run_overhead.py`
      re-run and compared with `docs/phase7-evidence/bench.json`.
- [ ] Human review completed for any agent-generated code.

Gates (these are the CI steps in `.github/workflows/ci.yml`):

```powershell
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m black --check .
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pytest tests/test_acr001.py -q
```

## 5. Review checklist (paste into PRs)

- [ ] Change meets its acceptance criteria and milestone scope.
- [ ] Public surface (SDK names, CLI flags, `/v1` routes, exit codes, schema versions)
      is consistent across code, README, `docs/setup.md`, and docs 06-07.
- [ ] Commands and outputs in the PR are real and were run.
- [ ] Security and privacy: redaction still matches `tracer.py` and `scan_pii.py`; no
      secrets in code, traces, or logs; auth behavior of `/v1/*` unchanged unless
      intended (doc 11).
- [ ] Failure modes: ambiguous timeout, stale read, and malformed input paths have a
      test or an explicit non-applicability note. Bad input fails closed with the
      documented exit code (1 FAIL or PII leak, 2 usage/config, 3 corrupt trace).
- [ ] The SDK still never raises into the host.
- [ ] No dates or day-wise promises in code, docs, or comments.

## 6. Change management

| Change type | Approval | How |
|---|---|---|
| Requirement wording (doc 03) | Product lead | PR against the doc; link the motivating trace or evidence |
| New or superseding ADR | Tech lead | New ADR section in doc 08; old ADR marked Superseded, never edited |
| Policy or patch schema change | Tech lead | Schema PR in `src/agentcrash/schemas/` with a migration note and updated tests |
| Event or trace schema bump | Tech lead | New schema version; reader accepts old plus new for one milestone |
| Removing public API | Tech lead | `CHANGELOG.md` migration table, as done for 0.3.0 |
| Release / version tag | Owner | See section 8; `CHANGELOG.md` entry |

## 7. Coding standards

- **Runtime:** Python 3.11+. Type hints on public functions. Docstrings on modules and
  public classes.
- **Style:** Black and Ruff, line length 100 (`pyproject.toml`). CI enforces both.
- **Determinism:** deterministic modules (clock, tracer, ledger, injector, verifiers, ...)
  must not use wall-clock time or global `random`. `tests/test_acr001.py` (ACR001) fails
  on banned imports. Time comes from `VirtualClock`.
- **Domain-free:** no domain vocabulary or default tool targets in `src/agentcrash/`.
  Use neutral names in docs and tests (`payments.charge`, `crm.update`, `kv`).
- **Dependencies:** core runtime deps are `pyyaml` and `jsonschema`. Anything else needs
  a PR note naming the capability it buys, and usually belongs in an extra.
- **Secrets:** environment only (doc 11). Tests use no real keys.
- **Tests:** pytest, offline, built in code with `tmp_path`; no committed golden traces
  (doc 10).
- **SDK safety:** the SDK must never raise into the host and must stay a no-op under
  `AGENTCRASH_DISABLED=1`.

## 8. Release and versioning

- SemVer. The package is `0.x`, so a minor release may break the API; every break gets a
  migration row in `CHANGELOG.md`. Current version: 0.3.0 (verify with
  `python -m agentcrash --version`).
- Release steps:
  1. Bump `version` in `pyproject.toml` and `__version__` in `src/agentcrash/__init__.py`
     (the release job refuses a tag that does not match both).
  2. Add the `CHANGELOG.md` entry; check `bench/run_overhead.py` is within targets.
  3. Merge to `main`, then create tag `v<version>` on that commit and push the tag to
     `origin`.
  4. `.github/workflows/release.yml` runs the gates on py3.11-3.13, builds the sdist and
     wheel, runs `twine check --strict`, smoke-installs the wheel in a clean venv, and
     publishes to PyPI with trusted publishing (no token stored in GitHub).
- One-time trusted-publishing setup (owner):
  1. pypi.org > `agentcrash-core` > Manage > Publishing > add a GitHub publisher:
     owner `SahilSatyam`, repository `AgentCrash`, workflow `release.yml`,
     environment `pypi`.
  2. GitHub > repo Settings > Environments > create `pypi`. Optional: add yourself as a
     required reviewer so every upload waits for your approval.
- PyPI uploads are permanent: a version number can never be re-used. A bad release is
  yanked on pypi.org and fixed with a new patch version.
- Rollback of a release: yank it on pypi.org, then publish a fixed patch version.
  Event and policy schemas are versioned (`v1`), so a code rollback inside a major
  schema version needs no data migration.

## 9. Local development quickstart (Windows PowerShell)

```powershell
git clone https://github.com/SahilSatyam/AgentCrash.git
cd AgentCrash
uv venv
uv pip install -e ".[dev]"

# Gates
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m black --check .
.\.venv\Scripts\python -m pytest -q

# Verify the CLI
.\.venv\Scripts\python -m agentcrash --version
.\.venv\Scripts\python -m agentcrash check --help

# Demo loop: see docs/setup.md section 3 and doc 14
```

Always use `python -m agentcrash`: Windows Smart App Control can block the unsigned
`agentcrash.exe` launcher (doc 14, section 7).

## 10. Cost discipline

The core needs no model and no network, so the suite and demo loop cost nothing beyond
CI minutes. Live-model patch proposals (`fetch_live_patch`) are opt-in and happen in the
host. Any change that adds a recurring model call, a new CI job, or a new dependency
states the cost in the PR evidence section.
