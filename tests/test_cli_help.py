"""Every subcommand documents every flag in --help; bogus commands fail."""

from __future__ import annotations

import pytest

from agentcrash.cli import EXIT_OK, EXIT_USAGE, main

COMMAND_FLAGS = {
    "run": [
        "--scenario",
        "--model",
        "--policy",
        "--seed",
        "--max-steps",
        "--trace-out",
        "--state-out",
        "--dry-run",
        "--json",
    ],
    "check": [
        "--trace",
        "--scenario",
        "--state",
        "--strict-schema",
        "--verify-chain",
        "--report-out",
        "--format",
        "--scan-pii",
    ],
    "diagnose": [
        "--trace",
        "--output",
        "--model",
        "--max-rules",
        "--state",
        "--scenario",
        "--policy",
        "--verdict",
    ],
    "replay": [
        "--trace",
        "--policy",
        "--patch",
        "--scenario",
        "--state",
        "--model",
        "--max-steps",
        "--trace-out",
        "--state-out",
        "--regression",
        "--no-regression",
        "--report-out",
    ],
}


@pytest.mark.parametrize("command,flags", list(COMMAND_FLAGS.items()))
def test_help_documents_every_flag(command: str, flags: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--help"])
    assert exc.value.code == EXIT_OK
    out = capsys.readouterr().out
    for flag in flags:
        assert flag in out, f"{command} --help omits {flag}"


def test_bogus_command_exits_nonzero(capsys) -> None:
    assert main(["frobnicate"]) == EXIT_USAGE
    assert "unknown command" in capsys.readouterr().err


def test_top_level_help_lists_all_commands(capsys) -> None:
    assert main(["--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for command in COMMAND_FLAGS:
        assert command in out
