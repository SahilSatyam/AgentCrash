"""Allow `python -m agentcrash ...` (see docs 13 §9 and 14 §2 quickstarts)."""

from agentcrash.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
