"""Score benchmark results: writes benchmark/results/summary.json and summary.md."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.harness import BENCH_DIR, CASES_FILE, load_cases, render_markdown, score_case, summarize  # noqa: E402


def main(cases_file: Path = CASES_FILE, results: Path = BENCH_DIR / "results") -> None:
    rows = []
    for case in load_cases(cases_file):
        folder = results / case["id"]
        if not (folder / "result.json").exists():
            print(f"missing: {case['id']} (not run yet)")
            continue
        result = json.loads((folder / "result.json").read_text())
        run_file = folder / "run.json"
        run = json.loads(run_file.read_text()) if run_file.exists() else None
        rows.append(score_case(case, result, run))
    if not rows:
        print("No results yet. Run: python benchmark/run.py")
        return
    results.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    (results / "summary.json").write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    markdown = render_markdown(summary, rows)
    (results / "summary.md").write_text(markdown)
    print(markdown)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES_FILE)
    parser.add_argument("--results", type=Path, default=BENCH_DIR / "results")
    args = parser.parse_args()
    main(args.cases, args.results)
