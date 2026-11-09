"""Offline tests for the benchmark harness (no network, model or Docker)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.harness import load_cases, render_markdown, score_case, split_diff, summarize  # noqa: E402

PATCH = """diff --git a/src/pkg/core.py b/src/pkg/core.py
--- a/src/pkg/core.py
+++ b/src/pkg/core.py
@@ -1 +1 @@
-x = 1
+x = 2
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1,2 @@
 import pkg
+assert pkg
"""

CASE = {"id": "demo-1", "category": "logic_edge_case", "expected_eligibility": "eligible",
        "gold_source_files": ["src/pkg/core.py"], "gold_test_files": ["tests/test_core.py"]}
NEGATIVE = {**CASE, "id": "demo-docs", "expected_eligibility": "unsupported",
            "gold_source_files": [], "gold_test_files": []}


def run_record(status="draft_ready", eligibility="eligible", sources=("src/pkg/core.py",),
               tests=("tests/test_core.py",)):
    return {"status": status, "stage": "patch_generation", "detail": "",
            "eligibility": {"status": eligibility},
            "discovery": {"status": "selected", "source_files": list(sources), "test_files": list(tests),
                          "exploration_calls": 6},
            "usage": {"eligibility": {"requests": 1, "input_tokens": 900, "output_tokens": 100},
                      "discovery": {"requests": 7, "input_tokens": 40000, "output_tokens": 2000}}}


def sandbox(status, baseline=True):
    return {"status": status, "baseline": {"passed": baseline}}


def test_registered_cases_are_well_formed():
    cases = load_cases()
    assert len(cases) == 8
    assert len({c["repo"] for c in cases}) == 4
    negatives = [c for c in cases if c["expected_eligibility"] == "unsupported"]
    assert len(negatives) == 2 and all(not c["gold_source_files"] for c in negatives)
    positives = [c for c in cases if c["expected_eligibility"] == "eligible"]
    assert all(c["gold_source_files"] and c["gold_test_files"] for c in positives)


def test_split_diff_separates_test_hunks():
    tests, sources = split_diff(PATCH)
    assert tests.startswith("diff --git a/tests/test_core.py") and "src/pkg" not in tests
    assert sources.startswith("diff --git a/src/pkg/core.py") and "tests/" not in sources


def test_fully_validated_case():
    row = score_case(CASE, {"patch_applies": True,
                            "sandbox_full": sandbox("patched_tests_passed"),
                            "sandbox_tests_only": sandbox("patched_tests_failed")}, run_record())
    assert row["outcome"] == "validated" and row["regression_validated"] is True
    assert row["source_recall"] == 1 and row["test_file_hit"] is True
    assert row["tokens"] == 43000 and row["tool_calls"] == 6


@pytest.mark.parametrize("result, run, outcome", [
    ({"patch_applies": True, "sandbox_full": sandbox("patched_tests_failed")}, run_record(), "tests_failed"),
    ({"patch_applies": True, "sandbox_full": sandbox("patched_tests_failed", baseline=False)}, run_record(),
     "infrastructure"),
    ({"patch_applies": True, "sandbox_full": sandbox("patched_tests_passed"),
      "sandbox_tests_only": sandbox("patched_tests_passed")}, run_record(), "not_regression_validated"),
    ({"patch_applies": True, "sandbox_full": sandbox("patched_tests_passed")}, run_record(), "no_regression_test"),
    ({"patch_applies": False}, run_record(), "patch_not_applicable"),
    ({}, {**run_record(status="patch_failed"), "detail": "Source mismatch"}, "patch_failed"),
    ({}, {**run_record(status="needs_review"), "stage": "discovery"}, "needs_review"),
    ({"harness_error": "RuntimeError: clone failed"}, run_record(), "harness_error"),
    ({"pipeline": {"stderr_tail": "Traceback"}}, None, "harness_error"),
])
def test_failure_outcomes_are_classified(result, run, outcome):
    assert score_case(CASE, result, run)["outcome"] == outcome


def test_wrong_file_selection_gives_zero_recall():
    row = score_case(CASE, {}, run_record(status="patch_failed", sources=("src/pkg/other.py",)))
    assert row["source_recall"] == 0 and row["outcome"] == "patch_failed"


def test_negative_cases_score_rejection_only():
    rejected = score_case(NEGATIVE, {}, run_record(status="unsupported", eligibility="unsupported"))
    missed = score_case(NEGATIVE, {}, run_record(eligibility="needs_review"))
    assert (rejected["outcome"], rejected["eligibility_correct"]) == ("correct_rejection", True)
    assert (missed["outcome"], missed["eligibility_correct"]) == ("missed_rejection", False)
    assert rejected["source_recall"] is None  # no gold files for negatives


def test_summary_rates_keep_denominators_and_exclude_infrastructure():
    rows = [
        score_case(CASE, {"patch_applies": True, "sandbox_full": sandbox("patched_tests_passed"),
                          "sandbox_tests_only": sandbox("patched_tests_failed")}, run_record()),
        score_case({**CASE, "id": "demo-2"}, {"patch_applies": True,
                   "sandbox_full": sandbox("patched_tests_failed", baseline=False)}, run_record()),
        score_case({**CASE, "id": "demo-3"}, {}, run_record(status="needs_review", eligibility="needs_review")),
        score_case(NEGATIVE, {}, run_record(status="unsupported", eligibility="unsupported")),
    ]
    summary = summarize(rows)
    assert summary["eligibility_accuracy"] == "3/4"
    assert summary["eligible_cases_flagged_needs_review"] == 1
    assert summary["patch_apply_rate"] == "2/2"
    assert summary["test_pass_rate"] == "1/1"  # infra case excluded from the denominator
    assert summary["infrastructure_failures"] == 1
    assert summary["outcomes"]["validated"] == 1
    markdown = render_markdown(summary, rows)
    assert "| demo-2 |" in markdown and "infrastructure" in markdown


# --------------------------------------------------------- holdout selection

from benchmark.mine_candidates import classify  # noqa: E402


def gql_issue(files, labels=(), state="COMPLETED", closed="2025-05-01T00:00:00Z", prs=1, merged=True):
    pr = {"number": 7, "merged": merged,
          "mergeCommit": {"oid": "b" * 40, "parents": {"nodes": [{"oid": "a" * 40}]}},
          "files": {"nodes": [{"path": p, "additions": a, "deletions": d} for p, a, d in files]}}
    return {"number": 42, "title": "Something is wrong", "closedAt": closed, "stateReason": state,
            "labels": {"nodes": [{"name": n} for n in labels]},
            "closedByPullRequestsReferences": {"nodes": [pr] * prs}}


def test_mining_selects_small_fix_with_tests_as_positive():
    case = classify(gql_issue([("src/pkg/core.py", 3, 1), ("tests/test_core.py", 10, 0)]), "o", "r")
    assert case["kind"] == "positive" and case["gold_source_files"] == ["src/pkg/core.py"]
    assert case["base_commit"] == "a" * 40 and case["fix_commit"] == "b" * 40


@pytest.mark.parametrize("issue", [
    gql_issue([("src/pkg/core.py", 3, 1)]),                                   # no test change
    gql_issue([("src/pkg/core.py", 25, 10), ("tests/test_core.py", 1, 0)]),  # > 30 lines
    gql_issue([("a.py", 1, 0), ("b.py", 1, 0), ("c.py", 1, 0), ("tests/test_a.py", 1, 0)]),  # 3 files
    gql_issue([("src/pkg/core.py", 3, 1), ("tests/test_core.py", 1, 0)], state="NOT_PLANNED"),
    gql_issue([("src/pkg/core.py", 3, 1), ("tests/test_core.py", 1, 0)], closed="2019-01-01T00:00:00Z"),
    gql_issue([("src/pkg/core.py", 3, 1), ("tests/test_core.py", 1, 0)], prs=2),
    gql_issue([("docs/index.md", 3, 1)], labels=("documentation",)),         # gate would reject anyway
])
def test_mining_rejects_cases_outside_the_rule(issue):
    assert classify(issue, "o", "r") is None


def test_mining_docs_only_fix_without_blocking_label_is_negative():
    case = classify(gql_issue([("docs/index.md", 3, 1), ("README.md", 1, 0)], labels=("bug",)), "o", "r")
    assert case["kind"] == "negative" and case["expected_eligibility"] == "unsupported"
