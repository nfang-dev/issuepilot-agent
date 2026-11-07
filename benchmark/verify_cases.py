"""Pre-flight check of benchmark/cases.json against GitHub (needs GITHUB_TOKEN).

Confirms each number is an issue (not a PR) and that base_commit is the parent of
fix_commit. Prints labels and state for the record. Makes no model calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.harness import load_cases  # noqa: E402


def main() -> None:
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "issuepilot-benchmark"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    problems = 0
    for case in load_cases():
        base = f"https://api.github.com/repos/{case['repo']}"
        issue = requests.get(f"{base}/issues/{case['issue']}", headers=headers, timeout=20)
        commit = requests.get(f"{base}/commits/{case['fix_commit']}", headers=headers, timeout=20)
        notes = []
        if issue.status_code != 200:
            notes.append(f"issue HTTP {issue.status_code}")
            data = {}
        else:
            data = issue.json()
            if "pull_request" in data:
                notes.append("NUMBER IS A PULL REQUEST")
        if commit.status_code != 200:
            notes.append(f"commit HTTP {commit.status_code}")
        elif case["base_commit"] not in [p["sha"] for p in commit.json().get("parents", [])]:
            notes.append("base_commit is not a parent of fix_commit")
        problems += bool(notes)
        labels = ",".join(l["name"] for l in data.get("labels", [])) or "-"
        print(f"{'FAIL' if notes else 'ok  '} {case['id']:<16} state={data.get('state')} labels={labels} "
              f"title={data.get('title', '')[:60]!r} {'; '.join(notes)}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
