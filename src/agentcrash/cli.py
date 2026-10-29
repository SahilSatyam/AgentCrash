"""AgentCrash command-line entry point.

Phase 0 placeholder: the `run` / `check` / `diagnose` / `replay` commands
land with the M0 prototype slice (see docs/adlc/15-roadmap-and-changelog.md).
"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    """Print a stub notice; real commands land in milestone M0."""
    del argv
    print("agentcrash: prototype commands (run/check/diagnose/replay) land in milestone M0.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
