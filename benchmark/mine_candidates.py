"""Select holdout benchmark cases from GitHub with a fixed, mechanical rule.

Run once (needs GITHUB_TOKEN); it writes benchmark/holdout.json. Commit that
file BEFORE any IssuePilot run on it, and never edit or re-select it after
seeing results.

Rule (fixed 2026-11-08, before selection):
  Repositories are scanned in REPOS order. For each repository, closed issues
  (closed as completed, since 2021-01-01, newest first, up to 300) are examined.
  An issue qualifies only if exactly one merged pull request closed it.
  * Positive case: the PR changes 1-2 non-test .py files with at most 30 changed
    lines in them, changes at least one test file, and every touched .py file
    is <= 24,000 bytes at the base commit (first parent of the merge commit).
  * Negative case: the PR changes no .py files at all (e.g. docs only) and the
    issue has no label that IssuePilot's deterministic gate already rejects,
    so the model has to decide.
  Take up to 2 positives and 1 negative per repository, stopping at 10
  positives and 3 negatives in total.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from issuepilot.eligibility import BLOCKING_LABEL_WORDS, _label_matches, is_test_path  # noqa: E402
from issuepilot.github_api import GitHubAPI, IssueRef  # noqa: E402

REPOS = ["pypa/packaging", "jd/tenacity", "executablebooks/markdown-it-py", "hukkin/tomli",
         "jazzband/tablib", "hukkin/mdformat", "pallets/markupsafe", "python-attrs/cattrs"]
MAX_POSITIVES, MAX_NEGATIVES = 10, 3
PER_REPO_POSITIVES, PER_REPO_NEGATIVES = 2, 1
SINCE = "2021-01-01"
OUT = Path(__file__).resolve().parent / "holdout.json"

QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(states: CLOSED, first: 50, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title closedAt stateReason
        labels(first: 20) { nodes { name } }
        closedByPullRequestsReferences(first: 5, includeClosedPrs: false) {
          nodes {
            number merged
            mergeCommit { oid parents(first: 1) { nodes { oid } } }
            files(first: 100) { nodes { path additions deletions } }
          }
        }
      }
    }
  }
}"""


def graphql(token: str, variables: dict) -> dict:
    response = requests.post("https://api.github.com/graphql", timeout=60,
                             json={"query": QUERY, "variables": variables},
                             headers={"Authorization": f"Bearer {token}", "User-Agent": "issuepilot-benchmark"})
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        raise RuntimeError(data["errors"])
    return data["data"]["repository"]["issues"]


def classify(issue: dict, owner: str, name: str) -> dict | None:
    if issue["stateReason"] != "COMPLETED" or (issue["closedAt"] or "") < SINCE:
        return None
    prs = [p for p in issue["closedByPullRequestsReferences"]["nodes"] if p["merged"] and p["mergeCommit"]]
    if len(prs) != 1:
        return None
    pr = prs[0]
    files = pr["files"]["nodes"]
    py = [f for f in files if f["path"].endswith(".py")]
    sources = [f for f in py if not is_test_path(f["path"])]
    tests = [f for f in py if is_test_path(f["path"])]
    labels = [l["name"] for l in issue["labels"]["nodes"]]
    parents = pr["mergeCommit"]["parents"]["nodes"]
    if not parents:
        return None
    case = {"repo": f"{owner}/{name}", "issue": issue["number"], "summary": issue["title"][:120],
            "fix_commit": pr["mergeCommit"]["oid"], "base_commit": parents[0]["oid"], "fix_pr": pr["number"],
            "labels": labels}
    if not py:
        if _label_matches(labels, BLOCKING_LABEL_WORDS):
            return None
        return {**case, "kind": "negative", "expected_eligibility": "unsupported",
                "category": "negative_no_python_change", "gold_source_files": [], "gold_test_files": []}
    churn = sum(f["additions"] + f["deletions"] for f in sources)
    if not (1 <= len(sources) <= 2 and tests and churn <= 30):
        return None
    return {**case, "kind": "positive", "expected_eligibility": "eligible", "category": "bug_fix",
            "gold_source_files": sorted(f["path"] for f in sources),
            "gold_test_files": sorted(f["path"] for f in tests)}


def sizes_ok(case: dict, token: str) -> bool:
    owner, name = case["repo"].split("/")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})
    api = GitHubAPI(IssueRef(owner, name, case["issue"]), session=session)
    tree = api.list_tree_at_revision(case["base_commit"])["files"]
    paths = case["gold_source_files"] + case["gold_test_files"]
    return all(p in tree and tree[p] <= 24_000 for p in paths)


def main() -> None:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required")
    if OUT.exists():
        sys.exit(f"{OUT} already exists; the holdout set must not be re-selected")
    positives, negatives = [], []
    for repo in REPOS:
        owner, name = repo.split("/")
        repo_pos = repo_neg = scanned = 0
        cursor = None
        while scanned < 300 and (repo_pos < PER_REPO_POSITIVES or repo_neg < PER_REPO_NEGATIVES):
            page = graphql(token, {"owner": owner, "name": name, "cursor": cursor})
            for issue in page["nodes"]:
                scanned += 1
                case = classify(issue, owner, name)
                if case is None:
                    continue
                if (case["kind"] == "positive" and repo_pos < PER_REPO_POSITIVES
                        and len(positives) < MAX_POSITIVES and sizes_ok(case, token)):
                    positives.append(case)
                    repo_pos += 1
                elif case["kind"] == "negative" and repo_neg < PER_REPO_NEGATIVES and len(negatives) < MAX_NEGATIVES:
                    negatives.append(case)
                    repo_neg += 1
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        print(f"{repo}: {repo_pos} positive, {repo_neg} negative (scanned {scanned} issues)")
        if len(positives) >= MAX_POSITIVES and len(negatives) >= MAX_NEGATIVES:
            break
    cases = []
    for case in positives + negatives:
        short = case["repo"].split("/")[1]
        cases.append({"id": f"{short}-{case['issue']}", **{k: v for k, v in case.items() if k != "kind"}})
    OUT.write_text(json.dumps({
        "benchmark": "issuepilot-holdout-v1",
        "selection_rule": __doc__.split("Rule", 1)[1].strip(),
        "cases": cases,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(cases)} cases to {OUT}. Commit it before running anything on it.")


if __name__ == "__main__":
    main()
