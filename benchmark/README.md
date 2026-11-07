# IssuePilot mini benchmark (pre-registered)

A small, honest benchmark: 10 real, already-fixed GitHub issues from 5 Python repositories, run once each, with every outcome kept. It measures IssuePilot as it is; it does not tune IssuePilot to these cases.

## Protocol

1. **Selection rule (fixed before any run).** From each repository, the 2 most recent non-merge fix commits that reference exactly one issue, change 1-2 non-test Python source files (at most 30 changed source lines), also change a test file, and touch only Python files under 24,000 bytes at the parent commit. Plus 2 documentation-only fixes as negatives that should be rejected. See [`cases.json`](cases.json); it was committed before any live run and must not be changed after seeing results.
2. **Ground truth.** Each case pins `--revision` to the parent of the upstream fix commit. Gold source/test files are the files that fix changed (version-only bumps excluded).
3. **Run.** IssuePilot runs URL-only with `--accept-needs-review` so later stages are measured; the raw eligibility decision is still recorded and scored.
4. **Validation.** In Docker: the full draft (baseline vs patched suite), and the draft's test hunks alone, which must fail without the source change.

## Metrics

| Metric | Definition |
| --- | --- |
| Eligibility accuracy | raw eligibility equals the expected label (`eligible` for bug fixes, `unsupported` for docs-only negatives) |
| File discovery recall | share of gold source files that discovery selected |
| Patch apply rate | draft passes `git apply --check` at the base commit |
| Test pass rate | patched suite passes in Docker (cases whose baseline suite runs cleanly) |
| Regression validated | draft's own tests fail without the source change and the full draft passes |
| Tool calls / tokens | discovery exploration calls; total model tokens from the run record |

Every case gets one outcome: `validated`, `tests_failed`, `not_regression_validated`, `no_regression_test`, `patch_failed`, `patch_not_applicable`, `needs_review`, `unsupported`, `correct_rejection`, `missed_rejection`, `infrastructure`, or `harness_error`.

## How to run (on your machine)

```bash
export OPENAI_API_KEY=...  GITHUB_TOKEN=...
docker build -f sandbox/Dockerfile -t issuepilot-sandbox:v1 .   # rebuild: image now includes click
python benchmark/verify_cases.py        # issue numbers are issues, base = parent of fix
python benchmark/run.py                 # ~10 cases; add --case ID to run one
python benchmark/score.py               # writes results/summary.md and summary.json
```

`run.py` clones each target repository into `benchmark/checkouts/` (git-ignored) and executes its tests inside the Docker sandbox. Results go to `benchmark/results/<case>/`.

## Known limitations (declared up front)

- 10 cases is a case study, not a statistically meaningful success rate.
- Closed issues: the issue text and comments are given to the model and may mention the eventual fix. This can make discovery easier than on a fresh issue.
- Both humanize cases may fail at import in the sandbox (generated `_version.py` / `pkg_resources` metadata). These count as `infrastructure`, not agent failures, and are reported separately.
- Regression validation only checks that the draft's tests fail without the source change; it does not prove they test the reported bug. Read the diffs.
- The Docker image adds `click` (needed by python-dotenv's test fixtures). This is a generic test-environment dependency, not case-specific tuning.
