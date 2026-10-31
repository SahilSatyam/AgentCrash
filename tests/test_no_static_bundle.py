"""The UI ships no static content: everything renders from the backend."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_no_baked_demo_bundle() -> None:
    assert not (REPO / "ui" / "demo-data.js").exists()


def test_index_has_no_bundle_script_tag() -> None:
    html = (REPO / "ui" / "index.html").read_text(encoding="utf-8")
    assert "demo-data" not in html


def test_no_demo_generator_script() -> None:
    assert not (REPO / "scripts" / "build_ui_demo.py").exists()
