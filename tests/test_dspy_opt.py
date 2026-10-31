"""Offline tests for the DSPy optimization probe (no live model)."""

import json

import dspy
import pytest

from agentcrash.dspy_opt import (
    BASELINE_INSTRUCTION,
    TRAINSET_ROWS,
    ProbeBudgetExceeded,
    build_program,
    build_trainset,
    check_budget,
    evaluate,
    odrl_metric,
    pack_for_report,
    program_instruction,
    score_doc,
    split_rows,
    trainset_hash,
    write_pack,
)


def _example(nl, expected):
    return dspy.Example(nl_request=nl, schema_hint="hint", expected=expected).with_inputs(
        "nl_request", "schema_hint"
    )


class _Pred:
    def __init__(self, text):
        self.odrl_document = text


def test_trainset_expected_docs_score_perfect():
    for row in TRAINSET_ROWS:
        assert score_doc(row["expected"], row["expected"]) == 1.0, row["id"]


def test_score_doc_rejects_attacker_grant():
    poisoned = dict(TRAINSET_ROWS[2]["expected"])
    poisoned["permission"] = list(poisoned["permission"]) + [
        {
            "action": "use",
            "target": "urn:data:dataset-7",
            "assignee": "attacker-client",
        }
    ]
    assert score_doc(poisoned, TRAINSET_ROWS[2]["expected"]) < 1.0


def test_score_doc_attacker_only_scores_low():
    only_attacker = dict(TRAINSET_ROWS[2]["expected"])
    only_attacker["permission"] = [
        {
            "action": "use",
            "target": "urn:data:dataset-7",
            "assignee": "attacker-client",
        }
    ]
    assert score_doc(only_attacker, TRAINSET_ROWS[2]["expected"]) < 0.5


def test_score_doc_rejects_non_dict():
    assert score_doc("not a doc", TRAINSET_ROWS[0]["expected"]) == 0.0
    assert score_doc(None, TRAINSET_ROWS[0]["expected"]) == 0.0


def test_metric_zero_on_garbage_output():
    example = _example("hello", TRAINSET_ROWS[0]["expected"])
    assert odrl_metric(example, _Pred("no json here")) == 0.0


def test_metric_perfect_on_expected_json():
    row = TRAINSET_ROWS[0]
    example = _example(row["nl"], row["expected"])
    assert odrl_metric(example, _Pred(json.dumps(row["expected"]))) == 1.0


def test_trainset_hash_stable():
    assert trainset_hash(TRAINSET_ROWS) == trainset_hash(list(TRAINSET_ROWS))


def test_split_rows_validation():
    train, val = split_rows(TRAINSET_ROWS, 3)
    assert len(train) == len(TRAINSET_ROWS) - 3
    assert len(val) == 3
    with pytest.raises(ValueError):
        split_rows(TRAINSET_ROWS, 0)
    with pytest.raises(ValueError):
        split_rows(TRAINSET_ROWS, len(TRAINSET_ROWS))


def test_build_program_carries_baseline_instruction():
    program = build_program()
    assert program_instruction(program) == BASELINE_INSTRUCTION


def test_build_program_custom_instruction():
    program = build_program(instruction="Be terse.")
    assert program_instruction(program) == "Be terse."


def test_evaluate_with_dummy_lm():
    rows = TRAINSET_ROWS[:2]
    answers = [{"odrl_document": json.dumps(r["expected"])} for r in rows]
    dspy.configure(lm=dspy.utils.DummyLM(answers))
    try:
        program = build_program()
        result = evaluate(program, build_trainset(rows))
    finally:
        dspy.configure(lm=None)
    assert result["n"] == 2
    assert result["mean"] == 1.0


def test_check_budget():
    check_budget(24, 24, "final")
    with pytest.raises(ProbeBudgetExceeded):
        check_budget(25, 24, "final")


def _report(baseline_mean, optimized_mean):
    return {
        "model": "test-model",
        "baseline_val": {"mean": baseline_mean, "scores": [], "n": 0},
        "optimized_val": {"mean": optimized_mean, "scores": [], "n": 0},
        "live_calls": 3,
        "max_live_calls": 24,
        "train_ids": ["a"],
        "val_ids": ["b"],
        "trainset_hash": "deadbeef",
        "optimized_instruction": "Better.",
        "baseline_instruction": BASELINE_INSTRUCTION,
        "config": {},
    }


def test_pack_only_on_improvement(tmp_path):
    out = tmp_path / "pack-v1.json"
    assert write_pack(_report(0.8, 0.8), out) is None
    assert not out.exists()
    assert write_pack(_report(0.9, 0.8), out) is None
    assert not out.exists()


def test_write_pack_roundtrip_and_force(tmp_path):
    out = tmp_path / "pack-v1.json"
    written = write_pack(_report(0.5, 0.9), out)
    assert written == out
    pack = json.loads(out.read_text(encoding="utf-8"))
    assert pack["instruction"] == "Better."
    assert pack["optimized_val_mean"] == 0.9
    with pytest.raises(FileExistsError):
        write_pack(_report(0.5, 0.9), out)
    assert write_pack(_report(0.5, 0.95), out, force=True) == out


def test_pack_for_report_none_when_not_better():
    assert pack_for_report(_report(0.7, 0.7)) is None
    pack = pack_for_report(_report(0.7, 0.71))
    assert pack is not None and pack["pack_version"] == 1


def test_cli_refuses_without_creds(monkeypatch):
    from agentcrash.cli import EXIT_RUNABORTED, cmd_optimize_prompts

    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    monkeypatch.delenv("LLAMA_BASE_URL", raising=False)
    assert cmd_optimize_prompts([]) == EXIT_RUNABORTED


def test_run_probe_rejects_bad_compile_rows():
    from agentcrash.dspy_opt import run_probe

    with pytest.raises(ValueError):
        run_probe(
            model="m",
            api_base="http://x",
            api_key="k",
            timeout_s=1,
            compile_rows=999,
        )
