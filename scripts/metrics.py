"""Corpus precision/recall + replay-fix metrics (doc 15 M1 exit evidence).

Reads the labelled corpus in ``tests/corpus`` (``labels.yaml`` manifest),
judges every item with the deterministic verifiers, and reports:

- precision = flagged-failing-that-are-failing / all-flagged (target >= 0.95)
- recall = flagged-failing-that-are-failing / all-actually-failing (>= 0.90)
- replay fix rate on the 5 canonical failing traces (target >= 4/5,
  no new violations)

``--build`` regenerates the corpus traces with the offline stub agent
(same seeds, naive vs fixed policies); ``--write`` regenerates
``tests/corpus/worksheets.md`` with the computed numbers. Fully offline:
no wall-clock, no network, no model calls.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from agentcrash.adapter import run_react_loop
from agentcrash.cli import load_yaml
from agentcrash.verifiers import (
    load_state_snapshot,
    load_trace_events,
    run_verifiers,
)

SEED_MATRIX = [1, 7, 42, 123, 2026]
PRECISION_TARGET = 0.95
RECALL_TARGET = 0.90
FIX_RATE_TARGET = (4, 5)

CORPUS_SPECS = [
    ("s1", "payment_gateway_timeout", "timeout_retry", "policies/fixed-timeout.yaml"),
    ("s2", "poisoned_document", "poisoned_doc", "policies/fixed-poisoned-doc.yaml"),
    ("s3", "concurrent_update", "stale_read", "policies/fixed-stale-read.yaml"),
]
NAIVE_POLICY = "policies/default.yaml"
CANONICAL = [
    ("s1", 42),
    ("s2", 42),
    ("s3", 42),
    ("s1", 7),
    ("s2", 7),
]


def corpus_dir(repo: Path = REPO) -> Path:
    """Return the checked-in corpus directory."""
    return repo / "tests" / "corpus"


def load_manifest(repo: Path = REPO) -> dict:
    """Parse the corpus manifest."""
    path = corpus_dir(repo) / "labels.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def find_scenario(repo: Path, scenario_id: str) -> tuple[Path, dict, str]:
    """Locate a scenario.yaml by id; return (path, data, text)."""
    for candidate in sorted((repo / "scenarios").glob("*/scenario.yaml")):
        data, text = load_yaml(candidate)
        if data.get("scenario") == scenario_id:
            return candidate, data, text
    raise ValueError(f"scenario {scenario_id!r} not found under scenarios/")


def judge(repo: Path, scenario_id: str, trace_file: str, state_file: str) -> dict[str, object]:
    """Run the scenario's verifiers over a corpus item."""
    _, scenario, _ = find_scenario(repo, scenario_id)
    base = corpus_dir(repo)
    events = load_trace_events(str(base / trace_file))
    state = load_state_snapshot(str(base / state_file))
    outcome = run_verifiers(events, state, scenario.get("invariants", []))
    failed = sorted(row["id"] for row in outcome["results"] if not row["passed"])
    return {"verdict": outcome["verdict"], "failed": failed}


def compute_metrics(repo: Path = REPO) -> dict[str, object]:
    """Score the corpus; return counts, precision/recall, and per-item rows."""
    manifest = load_manifest(repo)
    rows: list[dict[str, object]] = []
    tp = fp = fn = tn = 0
    for item in manifest["items"]:
        result = judge(repo, item["scenario"], item["file"], item["state"])
        actual = result["verdict"]
        expected = item["expected"]
        match = actual == expected
        if expected == "FAIL" and actual == "FAIL":
            tp += 1
        elif expected == "PASS" and actual == "FAIL":
            fp += 1
        elif expected == "FAIL" and actual == "PASS":
            fn += 1
        else:
            tn += 1
        rows.append({**item, "actual": actual, "match": match, "failed": result["failed"]})
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    return {
        "n": len(rows),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "rows": rows,
    }


def replay_fix(repo: Path = REPO) -> dict[str, object]:
    """Replay each canonical failing trace under its fixed policy (tmp dirs)."""
    manifest = load_manifest(repo)
    fixed_by_prefix = {prefix: fixed for prefix, _, _, fixed in CORPUS_SPECS}
    naive_by_name = {
        (i["scenario"], i["seed"]): i for i in manifest["items"] if "naive" in i["file"]
    }
    scenario_by_prefix = {p: s for p, s, _, _ in CORPUS_SPECS}
    rows: list[dict[str, object]] = []
    fixed = 0
    new_violations = 0
    for prefix, seed in CANONICAL:
        scenario_id = scenario_by_prefix[prefix]
        item = naive_by_name[(scenario_id, seed)]
        before = judge(repo, scenario_id, item["file"], item["state"])
        _, scenario, scenario_text = find_scenario(repo, scenario_id)
        policy, policy_text = load_yaml(repo / fixed_by_prefix[prefix])
        with tempfile.TemporaryDirectory(prefix="agentcrash-metrics-") as tmp:
            summary = run_react_loop(
                scenario=scenario,
                policy=policy,
                seed=seed,
                trace_path=str(Path(tmp) / "trace.jsonl"),
                state_path=str(Path(tmp) / "state.json"),
                scenario_text=scenario_text,
                policy_text=policy_text,
                max_steps=scenario.get("max_steps", 12),
            )
            events = load_trace_events(summary["trace_path"])
            state = load_state_snapshot(str(summary["state_path"]))
            after = run_verifiers(events, state, scenario.get("invariants", []))
        before_failed = set(before["failed"])
        is_fixed = before["verdict"] == "FAIL" and after["verdict"] == "PASS"
        broken = sorted(
            row["id"]
            for row in after["results"]
            if not row["passed"] and row["id"] not in before_failed
        )
        fixed += 1 if is_fixed else 0
        new_violations += len(broken)
        rows.append(
            {
                "trace": item["file"],
                "scenario": scenario_id,
                "seed": seed,
                "before": before["verdict"],
                "after": after["verdict"],
                "fixed": is_fixed,
                "broken": broken,
            }
        )
    return {
        "fixed": fixed,
        "total": len(rows),
        "new_violations": new_violations,
        "rows": rows,
    }


def build_corpus(repo: Path = REPO) -> None:
    """Regenerate corpus traces/states + manifest with the offline stub."""
    base = corpus_dir(repo)
    base.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, object]] = []
    for prefix, scenario_id, ref, fixed_policy in CORPUS_SPECS:
        path, scenario, scenario_text = find_scenario(repo, scenario_id)
        del path, ref
        for seed in SEED_MATRIX:
            for variant, policy_ref in (("naive", NAIVE_POLICY), ("fixed", fixed_policy)):
                policy, policy_text = load_yaml(repo / policy_ref)
                name = f"{prefix}-{variant}-seed{seed}"
                run_react_loop(
                    scenario=scenario,
                    policy=policy,
                    seed=seed,
                    trace_path=str(base / f"{name}.jsonl"),
                    state_path=str(base / f"{name}.state.json"),
                    scenario_text=scenario_text,
                    policy_text=policy_text,
                    max_steps=scenario.get("max_steps", 12),
                )
                items.append(
                    {
                        "file": f"{name}.jsonl",
                        "state": f"{name}.state.json",
                        "scenario": scenario_id,
                        "seed": seed,
                        "policy": policy_ref,
                        "expected": "FAIL" if variant == "naive" else "PASS",
                    }
                )
    manifest = {
        "schema_version": "corpus/v1",
        "notes": (
            "Labelled S1-S3 corpus: naive runs expect FAIL (fault + naive policy), "
            "fixed runs expect PASS (same fault, fixed policy). Seeds cover the "
            "doc 10 S6 matrix."
        ),
        "items": items,
        "canonical": [
            {
                "trace": f"{prefix}-naive-seed{seed}.jsonl",
                "state": f"{prefix}-naive-seed{seed}.state.json",
                "fixed_policy": next(f for p, _, _, f in CORPUS_SPECS if p == prefix),
            }
            for prefix, seed in CANONICAL
        ],
    }
    (base / "labels.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False) + "", encoding="utf-8"
    )


def write_worksheets(repo: Path = REPO) -> Path:
    """Compute metrics and write ``tests/corpus/worksheets.md``."""
    metrics = compute_metrics(repo)
    fix = replay_fix(repo)
    assert isinstance(metrics["rows"], list) and isinstance(fix["rows"], list)
    need, total = FIX_RATE_TARGET
    lines = [
        "# Corpus worksheets (M1 exit evidence)",
        "",
        (
            "Computed by `scripts/metrics.py` with the offline stub agent; "
            "re-run with `uv run --no-sync python scripts/metrics.py --write`."
        ),
        "",
        "## Corpus inventory",
        "",
        f"{metrics['n']} labelled traces (S1-S3 x seeds 1,7,42,123,2026 x naive/fixed).",
        "Naive runs expect FAIL (fault + naive policy); fixed runs expect PASS",
        "(same fault, fixed policy). Manifest: `tests/corpus/labels.yaml`.",
        "",
        "| file | scenario | seed | policy | expected | actual | match |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in metrics["rows"]:
        mark = "yes" if row["match"] else "NO"
        lines.append(
            f"| {row['file']} | {row['scenario']} | {row['seed']} "
            f"| {row['policy']} | {row['expected']} | {row['actual']} | {mark} |"
        )
    lines += [
        "",
        "## Precision / recall worksheet",
        "",
        (f"TP={metrics['tp']} FP={metrics['fp']} " f"FN={metrics['fn']} TN={metrics['tn']}"),
        "",
        f"precision = {metrics['precision']:.3f} (target >= {PRECISION_TARGET:.2f})",
        f"recall = {metrics['recall']:.3f} (target >= {RECALL_TARGET:.2f})",
        "",
        (
            "PASS"
            if metrics["precision"] >= PRECISION_TARGET and metrics["recall"] >= RECALL_TARGET
            else "FAIL"
        )
        + ": verifier precision/recall gates",
        "",
        "## Replay-fix worksheet",
        "",
        (
            "Canonical failing traces replayed under scenario fixed policies: "
            f"{fix['fixed']}/{fix['total']} fixed (target >= {need}/{total}), "
            f"new violations: {fix['new_violations']} (target 0)."
        ),
        "",
        "| trace | scenario | seed | before | after | fixed | new violations |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in fix["rows"]:
        broken = ", ".join(row["broken"]) or "none"
        lines.append(
            f"| {row['trace']} | {row['scenario']} | {row['seed']} "
            f"| {row['before']} | {row['after']} "
            f"| {'yes' if row['fixed'] else 'NO'} | {broken} |"
        )
    verdict = "PASS" if fix["fixed"] >= need and fix["new_violations"] == 0 else "FAIL"
    lines += ["", f"{verdict}: replay fix-rate gate", ""]
    out = corpus_dir(repo) / "worksheets.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    """Run the metrics CLI."""
    parser = argparse.ArgumentParser(prog="metrics.py")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if args.build:
        build_corpus()
        print(f"corpus rebuilt: {corpus_dir()}")
    metrics = compute_metrics()
    fix = replay_fix()
    print(
        f"corpus n={metrics['n']} precision={metrics['precision']:.3f} "
        f"(>= {PRECISION_TARGET:.2f}) recall={metrics['recall']:.3f} "
        f"(>= {RECALL_TARGET:.2f})"
    )
    print(
        f"replay-fix {fix['fixed']}/{fix['total']} "
        f"(>= {FIX_RATE_TARGET[0]}/{FIX_RATE_TARGET[1]}), "
        f"new violations: {fix['new_violations']}"
    )
    if args.write:
        out = write_worksheets()
        print(f"worksheets: {out}")
    ok = (
        metrics["precision"] >= PRECISION_TARGET
        and metrics["recall"] >= RECALL_TARGET
        and fix["fixed"] >= FIX_RATE_TARGET[0]
        and fix["new_violations"] == 0
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
