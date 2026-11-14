"""Reference integration: a second app reuses the core with no host coupling."""

import ast
import sys
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _load(name):
    path = EXAMPLES / name / "app.py"
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_second_app_imports_only_stdlib_and_agentcrash():
    src = _load("minimal_agent")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names) - {"__future__", "agentcrash"}
    assert not third_party, third_party
    assert "agentcrash" in imported  # genuinely integrated, not standalone


def test_minimal_agent_end_to_end():
    sys.path.insert(0, str(EXAMPLES / "minimal_agent"))
    try:
        import app as mini

        out = mini.count_twice({})
        assert out["state"]["n"] >= 1
    finally:
        sys.path.remove(str(EXAMPLES / "minimal_agent"))
        sys.modules.pop("app", None)
