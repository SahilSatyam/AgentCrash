"""Seeded database: oracle runs recorded through the real pipeline (offline)."""

from __future__ import annotations

from pathlib import Path

from agentcrash import db as db_module
from agentcrash.seed import CASES, seed_database


def test_seed_records_oracle_runs(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    keys = seed_database(Path(__file__).resolve().parent.parent, out_dir=tmp_path)
    assert len(keys) == len(CASES) == 8
    conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        runs = db_module.list_runs(conn)
        assert len(runs) == 8
        by_id = {}
        for run in runs:
            snap = db_module.get_run(conn, run["run_key"])
            assert snap is not None
            assert snap["status"] == "done"
            assert snap["chain_ok"] is True
            assert snap["events"], run["run_key"]
            by_id[(snap["spec"]["scenario_id"], snap["spec"]["policy_variant"])] = snap
        for scenario_id, _ in CASES:
            assert any(s == scenario_id for s, _ in by_id), scenario_id
        naive = by_id[("odrl_convert_timeout", "naive")]
        assert naive["verdict"]["verdict"] == "FAIL"
        assert naive["fault_summary"] == ["timeout_after_commit"]
        assert naive["patch"] is not None
        assert naive["patch"]["patch"]["rules"]
        fixed = by_id[("odrl_convert_timeout", "fixed")]
        assert fixed["verdict"]["verdict"] == "PASS"
        pairs = db_module.list_odrl_docs(conn)
        assert len(pairs) >= 8
        assert all(p["doc"].get("uid") for p in pairs)
    finally:
        conn.close()


def test_seed_is_idempotent(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    root = Path(__file__).resolve().parent.parent
    first = seed_database(root, out_dir=tmp_path)
    second = seed_database(root, out_dir=tmp_path)
    assert first == second
    conn = db_module.connect(tmp_path / "agentcrash.db")
    try:
        assert len(db_module.list_runs(conn)) == 8
    finally:
        conn.close()
