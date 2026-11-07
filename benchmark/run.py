"""Run the pre-registered IssuePilot benchmark cases on your machine.

Requirements: OPENAI_API_KEY, GITHUB_TOKEN, git, and the Docker image built with
`docker build -f sandbox/Dockerfile -t issuepilot-sandbox:v1 .`

For each case:
  1. IssuePilot runs URL-only at the case's base commit (--revision, --accept-needs-review).
     The raw eligibility decision is still recorded and scored.
  2. The target repository is checked out at the base commit under benchmark/checkouts/.
  3. `git apply --check` tests whether the draft applies.
  4. Docker runs the full draft (baseline vs patched suite).
  5. Docker runs the draft's test hunks alone: they should FAIL without the source change.

Every case is run once and every outcome is kept. Running this script executes code from
the target repositories inside the Docker sandbox; it never pushes or comments anywhere.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.harness import BENCH_DIR, issue_url, load_cases, split_diff  # noqa: E402

OUTPUTS = ("fix.patch", "manifest.json", "run.json", "sandbox_full.json",
           "tests_only.patch", "sandbox_tests_only.json", "result.json")


def sh(argv: list, timeout: int = 1800, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd, check=False)


def checkout(case: dict, root: Path) -> Path:
    """Clean checkout of the case's base commit inside benchmark/checkouts (harness-owned)."""
    repo_dir = root / case["repo"].replace("/", "__")
    if not (repo_dir / ".git").exists():
        cloned = sh(["git", "clone", "--filter=blob:none", f"https://github.com/{case['repo']}", repo_dir])
        if cloned.returncode:
            raise RuntimeError(f"clone failed: {cloned.stderr[-300:]}")
    sh(["git", "-C", repo_dir, "fetch", "--quiet", "origin", case["base_commit"]])
    switched = sh(["git", "-C", repo_dir, "checkout", "--quiet", "--force", "--detach", case["base_commit"]])
    if switched.returncode:
        raise RuntimeError(f"checkout failed: {switched.stderr[-300:]}")
    sh(["git", "-C", repo_dir, "clean", "-fdxq"])
    return repo_dir


def sandbox(repo_dir: Path, patch: Path, output: Path, manifest: Path | None = None) -> dict:
    argv = [sys.executable, "-m", "issuepilot.sandbox", "--repo-dir", repo_dir, "--patch", patch,
            "--approve", "--timeout", "300", "--output", output]
    if manifest is not None:
        argv += ["--manifest", manifest]
    sh(argv, timeout=900)
    return json.loads(output.read_text()) if output.exists() else {"status": "harness_error"}


def run_case(case: dict, out: Path, checkouts: Path, model: str) -> dict:
    for name in OUTPUTS:
        (out / name).unlink(missing_ok=True)
    result: dict = {"case": case["id"], "issue_url": issue_url(case), "base_commit": case["base_commit"]}
    started = time.monotonic()
    proc = sh([sys.executable, "-m", "issuepilot.patch_generator", issue_url(case),
               "--revision", case["base_commit"], "--accept-needs-review", "--model", model,
               "--output", out / "fix.patch", "--manifest", out / "manifest.json",
               "--run-record", out / "run.json"])
    result["pipeline"] = {"exit_code": proc.returncode, "seconds": round(time.monotonic() - started, 1),
                          "stdout_tail": proc.stdout[-1500:], "stderr_tail": proc.stderr[-1500:]}
    patch = out / "fix.patch"
    if not patch.exists():
        return result

    try:
        repo_dir = checkout(case, checkouts)
        result["patch_applies"] = sh(["git", "-C", repo_dir, "apply", "--check", patch.resolve()]).returncode == 0
        if not result["patch_applies"]:
            return result
        result["sandbox_full"] = sandbox(repo_dir, patch, out / "sandbox_full.json", out / "manifest.json")
        tests_part, source_part = split_diff(patch.read_text(encoding="utf-8"))
        if tests_part and source_part:
            (out / "tests_only.patch").write_text(tests_part, encoding="utf-8")
            result["sandbox_tests_only"] = sandbox(repo_dir, out / "tests_only.patch",
                                                   out / "sandbox_tests_only.json")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result["harness_error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="Run only these case ids (repeatable)")
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--results", type=Path, default=BENCH_DIR / "results")
    parser.add_argument("--checkouts", type=Path, default=BENCH_DIR / "checkouts")
    parser.add_argument("--skip-existing", action="store_true", help="Keep cases that already have result.json")
    args = parser.parse_args()
    cases = load_cases()
    if args.case:
        unknown = set(args.case) - {c["id"] for c in cases}
        if unknown:
            parser.error(f"Unknown case ids: {sorted(unknown)}")
        cases = [c for c in cases if c["id"] in args.case]
    args.checkouts.mkdir(parents=True, exist_ok=True)
    for i, case in enumerate(cases, 1):
        out = args.results / case["id"]
        out.mkdir(parents=True, exist_ok=True)
        if args.skip_existing and (out / "result.json").exists():
            print(f"[{i}/{len(cases)}] {case['id']}: skipped (result exists)")
            continue
        print(f"[{i}/{len(cases)}] {case['id']}: running ...", flush=True)
        try:
            result = run_case(case, out, args.checkouts, args.model)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            result = {"case": case["id"], "harness_error": f"{type(exc).__name__}: {exc}"[:500]}
        (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        run = json.loads((out / "run.json").read_text()) if (out / "run.json").exists() else {}
        print(f"    pipeline={run.get('status')} applies={result.get('patch_applies')} "
              f"sandbox={result.get('sandbox_full', {}).get('status')} "
              f"tests_only={result.get('sandbox_tests_only', {}).get('status')}")
    print("Done. Score with: python benchmark/score.py")


if __name__ == "__main__":
    main()
