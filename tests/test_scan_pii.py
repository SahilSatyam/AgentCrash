"""check --scan-pii: PII guardrail over raw trace + state files."""

from __future__ import annotations

import json
from pathlib import Path

from agentcrash.cli import cmd_check
from agentcrash.scan_pii import scan_files, scan_text

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "traces" / "odrl_convert_timeout-naive-seed42.jsonl"
GOLDEN_STATE = (
    REPO / "tests" / "fixtures" / "traces" / "odrl_convert_timeout-naive-seed42.state.json"
)


def test_placeholders_are_exempt() -> None:
    text = "mail redacted@example.com card 4111111111111111 ok"
    assert scan_text(text, "x") == []


def test_email_and_card_shapes_flagged_with_locations() -> None:
    text = "first line clean\ncontact attacker@evil.test now\ncard 5500 0000 0000 0004 end"
    hits = scan_text(text, "f")
    assert [(h.location, h.kind) for h in hits] == [
        ("f:2", "email"),
        ("f:3", "card"),
    ]
    assert hits[0].sample == "attacker@evil.test"


def test_scan_files_lists_hit_locations(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_text('{"a": 1}\n{"b": "x@y.zz"}\n', encoding="utf-8")
    hits = scan_files([target])
    assert len(hits) == 1
    assert hits[0].location == f"{target}:2"
    assert hits[0].kind == "email"


def test_golden_trace_and_state_are_pii_clean() -> None:
    assert scan_files([GOLDEN, GOLDEN_STATE]) == []


def test_check_scan_pii_passes_on_golden(tmp_path: Path, capsys) -> None:
    report = tmp_path / "v.json"
    code = cmd_check(
        [
            "--trace",
            str(GOLDEN),
            "--state",
            str(GOLDEN_STATE),
            "--report-out",
            str(report),
            "--scan-pii",
        ]
    )
    assert code == 1  # golden verdict is FAIL; PII gate itself passes
    out = capsys.readouterr().out
    assert "PII scan: clean" in out


def test_check_scan_pii_fails_on_leaked_pii(tmp_path: Path, capsys) -> None:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[1])
    event["detail"]["note"] = "contact vip@leak.test"
    lines[1] = json.dumps(event, sort_keys=True)
    trace = tmp_path / "leak.jsonl"
    trace.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state = tmp_path / "leak.state.json"
    state.write_text(GOLDEN_STATE.read_text(encoding="utf-8"), encoding="utf-8")
    code = cmd_check(
        [
            "--trace",
            str(trace),
            "--state",
            str(state),
            "--report-out",
            str(tmp_path / "v.json"),
            "--no-verify-chain",
            "--scan-pii",
        ]
    )
    assert code != 0
    out = capsys.readouterr().out
    assert "vip@leak.test" in out
    assert "leak.jsonl:" in out
    assert "vip@leak.test" in out.split("PII scan:")[1]
