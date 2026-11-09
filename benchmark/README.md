# IssuePilot mini benchmark (pre-registered)

A small, honest benchmark: 8 real, already-fixed GitHub issues from 4 Python repositories, run once each, with every outcome kept. It measures IssuePilot as it is; it does not tune IssuePilot to these cases.

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
python benchmark/run.py                 # 8 cases; add --case ID to run one
python benchmark/score.py               # writes results/summary.md and summary.json
```

`run.py` clones each target repository into `benchmark/checkouts/` (git-ignored) and executes its tests inside the Docker sandbox. Results go to `benchmark/results/<case>/`.

## Results: run 1 (2026-11-07, gpt-5-mini)

Raw per-case artefacts are in [`results/`](results/) and the generated table is [`results/summary.md`](results/summary.md). Analysis below was added after reading every case's records; no case was re-run or relabelled.

| Metric | Result | Note |
| --- | --- | --- |
| Eligibility accuracy | 7/8 | The one correct docs rejection (cachetools-395) came from the deterministic `docs` label rule, not the model |
| Discovery source recall | 6/6 cases, mean 1.0 | Every gold source file was selected |
| Gold test file selected | 5/6 | cachetools-405 chose `tests/test_lru.py`; upstream used the shared `tests/__init__.py` mixin |
| Patch generated | 4/6 positives | Both failures failed closed (no patch written) |
| Patch applies | 4/4 | |
| Patched suite passes in Docker | 3/3 | Excludes dotenv-699 (baseline not clean) |
| Regression validated in Docker | 3/3 | Draft's tests fail without the source change |
| Mean discovery tool calls / tokens | 4.7 / ~50K | Per case, all stages |

**Upstream-oracle check (added after the run, outside Docker).** For the 3 validated cases, the agent's *source* changes were applied without its tests, then the upstream fix's own tests were run (Python 3.13, plain pytest). Upstream tests fail on the original source and pass with the agent's change in all 3:

| Case | Original source + upstream tests | Agent source + upstream tests | Agent fix vs upstream |
| --- | --- | --- | --- |
| cachetools-423 | 1 failed | 338 passed | Same approach (check emptiness before `choice`) |
| cachetools-405 | 7 failed | 312 passed | Different implementation (adjusted eviction condition vs upstream's split insert/replace branches) |
| pathspec-84 | 1 failed | 119 passed | Identical one-line change (`return bool(include)`) |

**Failures, by root cause**

- **dotenv-697: patch generator output budget.** The patch call used exactly 7,000 output tokens (the `max_output_tokens` cap), so the JSON was truncated and rejected. A real IssuePilot limitation with reasoning models; discovery had selected the gold file.
- **pathspec-77: anchor not found.** The model's `old` text did not occur in the pinned source, so the generator refused to build a diff. The source-grounding check worked as designed; no fix was produced.
- **dotenv-699: test environment.** python-dotenv's CLI tests spawn the installed `dotenv` executable, which the sandbox (plain checkout, no install) lacks: 15 tests fail on the unpatched baseline. Counted as infrastructure. Informally, the agent's new test (`test_get_empty_value`) was the only additional failure in the tests-only run and passed with the full patch, but this case is not counted as validated.
- **semver-429: eligibility miss, rejected later.** The issue reports a bug in example code inside the docs; the classifier called it a `logic_bug`. Discovery then inspected the code and submitted `unsupported` ("the required change is to the .rst documentation"), so the run produced no patch. Scored as a miss for eligibility, as registered.

**What this run supports:** on small, self-contained fixes, discovery located the right source file in every case it reached, and half of the bug cases ended with a Docker-validated draft whose source change also passes the upstream regression tests. **What it does not support:** any success rate beyond these 8 cases, or performance on large files, multi-file fixes, or fresh (unfixed) issues.

## Run 2 protocol: holdout set + LLM-only baseline (fixed 2026-11-08, before selection)

The 8 cases above are now the **development set**: their results have been read and one change was made because of them (patch-generation output cap raised from 7,000 to 16,000 tokens after dotenv-697 truncated; truncation is now reported explicitly). Nothing else was tuned.

1. **Holdout selection.** `mine_candidates.py` selects about 13 new cases from repositories not in the dev set, using a mechanical rule written in its docstring (issue closed as completed, closed by exactly one merged PR, PR changes 1-2 small source files plus a test; negatives are no-Python-change fixes without a gate-blocking label). It writes `holdout.json`, which is committed **before** any run and never re-selected or edited.
2. **One run.** `run.py --cases benchmark/holdout.json --results benchmark/results-holdout` runs every holdout case once with the frozen code. No prompt or code changes between holdout cases, and no re-runs to improve scores.
3. **Baseline.** `baseline.py` runs an LLM-only file-localization baseline on both sets: same model, issue text and pinned commit, the full list of Python files, one structured call, no tools, no file contents, no evidence validation. It answers whether the tool-using agent localizes files better than a single prompt.
4. **Reporting.** Dev and holdout results are reported separately; the headline numbers come from the holdout set.
5. **Pre-run review.** See [`holdout_review.md`](holdout_review.md): three positives are maintainer-labelled `enhancement` (eligibility reported both ways), and generic test dependencies were added to the sandbox image. No case was changed.

## Results: run 2, holdout set + baseline (2026-11-08, gpt-5-mini, frozen code)

Holdout cases were selected by `mine_candidates.py`, committed (58eb6d4) and reviewed ([`holdout_review.md`](holdout_review.md)) before this run, then run once. Raw artefacts: [`results-holdout/`](results-holdout/).

| Metric (holdout, 13 cases) | Result | Note |
| --- | --- | --- |
| Eligibility accuracy, frozen rule labels | 8/13 | |
| Eligibility accuracy, maintainer labels | 11/13 | tomli-223, tomli-1, mdformat-507 are maintainer-labelled `enhancement`; the gate rejected them deterministically |
| Remaining eligibility misses | 2 | markdown-it-py-352 classified `feature_request` (it asks for a new CLI option); packaging-660 `needs_review` (design decision); continued via override |
| Negatives correctly rejected | 3/3 | All three needed the model (no blocking labels) |
| Discovery source recall | 6/6 cases, mean 1.0 | Selected exactly the gold source set every time |
| Gold test file selected | 4/6 | |
| Patch generated / applies | 5/6 / 5/5 | mdformat-357: generator rejected an ineffective test-file edit (fail-closed) |
| Docker: tests pass and regression validated | 2/2 | markupsafe-417, markupsafe-310 |
| Sandbox could not run the suite | 3 | IssuePilot's own staging limits (below) |

**Upstream-oracle check** (outside Docker, after the run): with only the agent's source change applied, the upstream fix's tests pass for both markupsafe cases (36 and 35 passed) and fail on the original source (1 failed each).

**Sandbox failures were IssuePilot limitations, not external ones.** Both packaging cases failed at collection because the staging allowlist does not copy binary test fixtures (`tests/musllinux/musl-x86_64`). markdown-it-py-415 exceeded the staging size/file limits. These are counted separately but are real gaps in the sandbox design.

### LLM-only baseline (file localization)

`baseline.py`: same model and issue text, full list of Python files at the base commit, one structured call, no tools, no file contents.

| Set | Baseline full source recall | Agent full source recall | Baseline extra (non-gold) files | Mean tokens: baseline / agent |
| --- | --- | --- | --- | --- |
| Holdout | 10/10 | 6/6 reached discovery | 4 of 10 cases | ~1.3K / ~28-70K |
| Dev | 6/6 | 6/6 | 1 of 6 cases | ~1.2K / ~50K |

**Interpretation.** On these small, single-package repositories the baseline found every gold file, often from the issue text alone (some titles name the file). This benchmark therefore does **not** show that tool-based discovery localizes files better than a single prompt; it shows it is no worse and slightly more precise, at much higher token cost. The agent's other properties (only selecting files it read, pinned reads, explicit needs_review on uncertainty, and feeding verified file contents to the patch generator) are design guarantees, not measured accuracy gains. Larger repositories would be needed to test a localization advantage.

### Combined (dev + holdout: 21 cases, 10 repositories)

- Root-cause source file selected in 12/12 bug cases that reached discovery.
- 5 Docker-validated fixes (3 dev, 2 holdout); all 5 also pass the upstream maintainers' regression tests.
- Failures kept: 3 patch-generation failures that failed closed, 4 sandbox runs blocked (1 dev environment, 3 holdout staging limits), and eligibility misses.

## Known limitations (declared up front)

- 8 cases is a case study, not a statistically meaningful success rate.
- Closed issues: the issue text and comments are given to the model and may mention the eventual fix. This can make discovery easier than on a fresh issue.
- Two humanize cases were removed **before any live run** because pre-flight verification showed the fix commits' issue numbers do not match the current repository (#159 is a pull request). See `excluded_before_run` in `cases.json`. No replacements were added.
- Regression validation only checks that the draft's tests fail without the source change; it does not prove they test the reported bug. Read the diffs.
- The Docker image adds `click` (needed by python-dotenv's test fixtures). This is a generic test-environment dependency, not case-specific tuning.
