"""`agentcrash serve` is a loopback static viewer with a clear offline error."""

from __future__ import annotations

import pytest

from agentcrash.cli import EXIT_OK, EXIT_USAGE, main


def test_serve_help_documents_flags(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--help"])
    assert exc.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "--port" in out and "--bind" in out


def test_serve_missing_ui_exits_usage(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["serve", "--port", "8901"]) == EXIT_USAGE
    assert "ui/index.html" in capsys.readouterr().err
