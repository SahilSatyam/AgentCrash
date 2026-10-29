"""ACR001: deterministic modules must not use wall-clock time or global random.

Applies to clock/tracer/ledger plus tools/injector/verifiers when they land.
Only files present in the tree are checked; missing future modules are skipped.
"""

import ast
from pathlib import Path

CANDIDATES = [
    "src/agentcrash/clock.py",
    "src/agentcrash/tracer.py",
    "src/agentcrash/ledger.py",
    "src/agentcrash/tools.py",
    "src/agentcrash/injector.py",
    "src/agentcrash/verifiers.py",
]

BANNED_MODULES = {"time", "datetime", "random"}


def _banned_imports(tree: ast.AST) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        else:
            continue
        hits.extend(f"import {root}" for root in roots if root in BANNED_MODULES)
    return hits


def test_no_wall_clock_or_global_random() -> None:
    root = Path(__file__).resolve().parent.parent
    checked: list[str] = []
    for rel in CANDIDATES:
        path = root / rel
        if not path.exists():
            continue
        checked.append(rel)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        assert _banned_imports(tree) == [], f"{rel} uses banned time/random imports"
    assert checked, "expected at least one deterministic module to exist"
