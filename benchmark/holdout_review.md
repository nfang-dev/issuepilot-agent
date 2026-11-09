# Holdout pre-run review (2026-11-08, before any IssuePilot run on holdout.json)

`holdout.json` was produced by `mine_candidates.py` and committed unchanged (commit 58eb6d4).
This review only inspected the case list and the target repositories' test setup. No case
was removed, replaced or relabelled.

## Finding 1: three "positive" cases carry maintainer `enhancement` labels

`tomli-223`, `tomli-1` and `mdformat-507` were selected as positives because their fixing PR
changed a small source file plus a test. Their maintainers labelled the issues `enhancement`,
and IssuePilot's deterministic gate rejects that label as `unsupported` without a model call.
The selection rule did not filter positives by label; that is a gap in the rule, found only now.

Decision, fixed before running:
- `holdout.json` keeps `expected_eligibility: eligible` for these cases (the frozen rule).
- Eligibility accuracy is reported twice: against the frozen rule label, and against the
  maintainers' labels (`enhancement` -> `unsupported`). Neither number is dropped.
- These cases cannot reach discovery (`--accept-needs-review` does not override
  `unsupported`), so discovery and patch metrics come from the remaining 7 positives.

## Finding 2: sandbox test dependencies

Without extra packages, 8 of 10 positive cases would fail before any patch is applied:
packaging tests import `pretend` and `tomli_w`; markdown-it-py needs `mdurl` and its tests use
`pytest-regressions` (and `pytest-timeout` via its config); mdformat imports `markdown_it`.

Decision: the sandbox image installs these generic test dependencies
(`pretend tomli-w mdurl markdown-it-py<4 pytest-regressions pytest-timeout`) and records exact
versions in `/opt/issuepilot-requirements.lock`. This is test-environment setup, applied
identically to every case. The sandbox puts the checked-out repository first on `PYTHONPATH`, so
the installed `markdown-it-py` does not shadow the markdown-it-py checkout.

## Other notes

- `packaging-1185` (negative) is labelled `bug`, but its fixing PR changed no Python file.
- `tenacity-523` and `markdown-it-py-295` are negatives; they need no sandbox.
- Remaining sandbox risk: mdformat tests may rely on installed entry points. Any baseline that
  does not run cleanly is reported as `infrastructure`.
