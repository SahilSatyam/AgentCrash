# Agent instructions pointer

Canonical docs live in `docs/`. New agent sessions: start with `docs/overview.md`, then `docs/architecture-decoupling.md` (core vs host boundary), then the roadmap in `docs/plans/2026-11-14-sota-roadmap.md` (draft status — read it for direction, but do not implement until the owner replies `go`). `docs/adlc/` and `docs/plans/2026-10-29-agentcrash-implementation.md` are the historical lifecycle record.

The core package must stay domain-free: `tests/test_generic_core.py` enforces it.
