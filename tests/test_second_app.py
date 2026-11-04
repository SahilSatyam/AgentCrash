"""Reference integration: a second app reuses the core with zero ODRL imports.

The ODRL host sample (``examples/odrl_host``) lives in the
``nl2odrl-responsible-ai`` repo and is exercised by its own suite there;
core must not depend on it.
"""

import ast
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name):
    path = EXAMPLES / name / "app.py"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_second_app_has_zero_odrl_imports():
    src = _load("minimal_agent")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any("odrl" in name.lower() for name in imported), imported
    assert "agentcrash" in src  # genuinely integrated, not standalone


def test_minimal_agent_end_to_end():
    sys.path.insert(0, str(EXAMPLES / "minimal_agent"))
    try:
        import app as mini

        out = mini.count_twice({})
        assert out["state"]["n"] >= 1
    finally:
        sys.path.remove(str(EXAMPLES / "minimal_agent"))
        sys.modules.pop("app", None)
