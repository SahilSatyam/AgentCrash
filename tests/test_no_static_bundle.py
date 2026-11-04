"""The Lab ships no static demo content: everything renders from the backend."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAB = REPO / "src" / "agentcrash" / "lab"


def test_no_baked_demo_bundle() -> None:
    assert not (LAB / "demo-data.js").exists()


def test_index_has_no_bundle_script_tag() -> None:
    html = (LAB / "index.html").read_text(encoding="utf-8")
    assert "demo-data" not in html


def test_no_demo_generator_script() -> None:
    assert not (REPO / "scripts" / "build_ui_demo.py").exists()
