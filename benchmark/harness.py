"""Pure helpers for the IssuePilot mini benchmark: case loading, diff splitting and scoring.

No network, model or Docker access happens here, so this module is unit-tested offline.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from issuepilot.eligibility import is_test_path

BENCH_DIR = Path(__file__).resolve().parent
CASES_FILE = BENCH_DIR / "cases.json"
SANDBOX_PASSED = "patched_tests_passed"
SANDBOX_FAILED = "patched_tests_failed"


def load_cases(path: Path = CASES_FILE) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data["cases"]
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case ids")
    for case in cases:
        if case["expected_eligibility"] not in ("eligible", "unsupported"):
            raise ValueError(f"{case['id']}: bad expected_eligibility")
        for key in ("fix_commit", "base_commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", case[key]):
                raise ValueError(f"{case['id']}: {key} must be a full SHA")
    return cases


def issue_url(case: dict) -> str:
    return f"https://github.com/{case['repo']}/issues/{case['issue']}"


def split_diff(patch: str) -> tuple[str, str]:
    """Split a unified git diff into (test-file sections, non-test sections)."""
    sections = re.split(r"(?m)^(?=diff --git )", patch)
    tests, sources = [], []
    for section in sections:
        match = re.match(r"diff --git a/(\S+) b/", section)
        if not match:
            continue
        (tests if is_test_path(match[1]) else sources).append(section)
    return "".join(tests), "".join(sources)


def _sandbox_ok(report: dict | None) -> bool | None:
    """Did the unpatched baseline suite run cleanly? None when there is no report."""
    if not report:
        return None
    baseline = report.get("baseline") or {}
    return bool(baseline.get("passed"))


def score_case(case: dict, result: dict, run: dict | None) -> dict:
    """Turn one case's raw artefacts into benchmark metrics plus a failure reason."""
    eligibility = (run or {}).get("eligibility") or {}
    discovery = (run or {}).get("discovery") or {}
    raw_eligibility = eligibility.get("status")
    expected = case["expected_eligibility"]
    gold = set(case["gold_source_files"])
    selected = set(discovery.get("source_files") or [])
    gold_tests = set(case["gold_test_files"])
    selected_tests = set(discovery.get("test_files") or [])
    usage = (run or {}).get("usage") or {}
    full = result.get("sandbox_full")
    tests_only = result.get("sandbox_tests_only")

    row = {
        "id": case["id"],
        "category": case["category"],
        "expected_eligibility": expected,
        "raw_eligibility": raw_eligibility,
        "eligibility_correct": raw_eligibility == expected if raw_eligibility else None,
        "pipeline_status": (run or {}).get("status"),
        "discovery_status": discovery.get("status"),
        "selected_source_files": sorted(selected),
        "selected_test_files": sorted(selected_tests),
        "source_recall": (len(selected & gold) / len(gold)) if gold and discovery.get("status") == "selected" else None,
        "test_file_hit": bool(selected_tests & gold_tests) if gold_tests and discovery.get("status") == "selected" else None,
        "patch_generated": (run or {}).get("status") == "draft_ready",
        "patch_applies": result.get("patch_applies"),
        "baseline_clean": _sandbox_ok(full),
        "tests_pass": (full or {}).get("status") == SANDBOX_PASSED if full else None,
        "regression_validated": None,
        "tool_calls": discovery.get("exploration_calls"),
        "model_requests": sum(v.get("requests", 0) for v in usage.values()),
        "tokens": sum(v.get("input_tokens", 0) + v.get("output_tokens", 0) for v in usage.values()),
        "seconds": (result.get("pipeline") or {}).get("seconds"),
    }
    if full and tests_only and row["baseline_clean"]:
        row["regression_validated"] = (tests_only.get("status") == SANDBOX_FAILED
                                       and full.get("status") == SANDBOX_PASSED)
    row["outcome"], row["failure_reason"] = _outcome(row, result, run)
    return row


def _outcome(row: dict, result: dict, run: dict | None) -> tuple[str, str]:
    if result.get("harness_error"):
        return "harness_error", result["harness_error"][:300]
    if run is None:
        return "harness_error", (result.get("pipeline") or {}).get("stderr_tail", "no run record")[-300:]
    status = run.get("status")
    if row["expected_eligibility"] == "unsupported":
        return ("correct_rejection" if row["raw_eligibility"] == "unsupported" else "missed_rejection",
                f"eligibility={row['raw_eligibility']}")
    if status in ("unsupported", "needs_review", "error"):
        return status, f"stage={run.get('stage')}: {run.get('detail', '')[:200]}"
    if status == "patch_failed":
        return "patch_failed", run.get("detail", "")[:200]
    if not row["patch_applies"]:
        return "patch_not_applicable", "git apply --check failed on the base commit"
    if row["baseline_clean"] is False:
        return "infrastructure", "baseline test suite does not run cleanly in the sandbox"
    if row["tests_pass"] is False:
        return "tests_failed", "patched test suite failed"
    if row["regression_validated"] is False:
        return "not_regression_validated", "generated tests do not fail before the source change"
    if row["regression_validated"] is None:
        return "no_regression_test", "patch contains no test changes"
    return "validated", ""


def _rate(rows: list[dict], key: str) -> str:
    values = [r[key] for r in rows if r[key] is not None]
    return f"{sum(bool(v) for v in values)}/{len(values)}" if values else "0/0"


def summarize(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["expected_eligibility"] == "eligible"]
    recalls = [r["source_recall"] for r in rows if r["source_recall"] is not None]
    tokens = [r["tokens"] for r in rows if r["tokens"]]
    calls = [r["tool_calls"] for r in rows if r["tool_calls"] is not None]
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {
        "cases": len(rows),
        "eligibility_accuracy": _rate(rows, "eligibility_correct"),
        "eligible_cases_flagged_needs_review": sum(r["raw_eligibility"] == "needs_review" for r in positives),
        "discovery_full_recall": f"{sum(x == 1 for x in recalls)}/{len(recalls)}",
        "discovery_mean_recall": round(sum(recalls) / len(recalls), 2) if recalls else None,
        "test_file_hit": _rate(rows, "test_file_hit"),
        "patch_generated": f"{sum(r['patch_generated'] for r in positives)}/{len(positives)}",
        "patch_apply_rate": _rate(positives, "patch_applies"),
        "test_pass_rate": _rate([r for r in positives if r["baseline_clean"]], "tests_pass"),
        "regression_validated": _rate([r for r in positives if r["baseline_clean"]], "regression_validated"),
        "infrastructure_failures": sum(r["outcome"] == "infrastructure" for r in rows),
        "mean_tool_calls": round(sum(calls) / len(calls), 1) if calls else None,
        "mean_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        "outcomes": outcomes,
    }


def render_markdown(summary: dict, rows: list[dict]) -> str:
    lines = [
        "# IssuePilot mini benchmark results", "",
        "Denominators exclude cases that never reached a stage. Cases with a sandbox baseline "
        "that does not run cleanly are counted as infrastructure failures, not agent failures.", "",
        "| Metric | Value |", "| --- | --- |",
    ]
    for key, value in summary.items():
        if key != "outcomes":
            lines.append(f"| {key} | {value} |")
    lines.append(f"| outcomes | {', '.join(f'{k}: {v}' for k, v in sorted(summary['outcomes'].items()))} |")
    lines += ["", "| Case | Expected | Eligibility | Selected source | Recall | Patch | Applies | Tests | Regression | Tool calls | Tokens | Outcome | Reason |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    fmt = lambda v: "-" if v is None else ("yes" if v is True else "no" if v is False else str(v))
    for r in rows:
        lines.append("| " + " | ".join([
            r["id"], r["expected_eligibility"], fmt(r["raw_eligibility"]),
            ", ".join(r["selected_source_files"]) or "-", fmt(r["source_recall"]),
            fmt(r["patch_generated"]), fmt(r["patch_applies"]), fmt(r["tests_pass"]),
            fmt(r["regression_validated"]), fmt(r["tool_calls"]), fmt(r["tokens"]),
            r["outcome"], r["failure_reason"].replace("|", "/").replace("\n", " ")[:160],
        ]) + " |")
    return "\n".join(lines) + "\n"
