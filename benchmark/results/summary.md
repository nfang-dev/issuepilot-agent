# IssuePilot mini benchmark results

Denominators exclude cases that never reached a stage. Cases with a sandbox baseline that does not run cleanly are counted as infrastructure failures, not agent failures.

| Metric | Value |
| --- | --- |
| cases | 8 |
| eligibility_accuracy | 7/8 |
| eligible_cases_flagged_needs_review | 0 |
| discovery_full_recall | 6/6 |
| discovery_mean_recall | 1.0 |
| test_file_hit | 5/6 |
| patch_generated | 4/6 |
| patch_apply_rate | 4/4 |
| test_pass_rate | 3/3 |
| regression_validated | 3/3 |
| infrastructure_failures | 1 |
| mean_tool_calls | 4.7 |
| mean_tokens | 50380 |
| outcomes | correct_rejection: 1, infrastructure: 1, missed_rejection: 1, patch_failed: 2, validated: 3 |

| Case | Expected | Eligibility | Selected source | Recall | Patch | Applies | Tests | Regression | Tool calls | Tokens | Outcome | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dotenv-699 | eligible | eligible | src/dotenv/cli.py | 1.0 | yes | yes | no | - | 3 | 27512 | infrastructure | baseline test suite does not run cleanly in the sandbox |
| dotenv-697 | eligible | eligible | src/dotenv/cli.py, src/dotenv/main.py | 1.0 | no | - | - | - | 5 | 57198 | patch_failed | Model did not return valid structured edit JSON |
| cachetools-423 | eligible | eligible | src/cachetools/__init__.py | 1.0 | yes | yes | yes | yes | 3 | 35745 | validated |  |
| cachetools-405 | eligible | eligible | src/cachetools/__init__.py | 1.0 | yes | yes | yes | yes | 5 | 64976 | validated |  |
| pathspec-84 | eligible | eligible | pathspec/pathspec.py | 1.0 | yes | yes | yes | yes | 9 | 96606 | validated |  |
| pathspec-77 | eligible | eligible | pathspec/patterns/gitwildmatch.py | 1.0 | no | - | - | - | 5 | 57909 | patch_failed | Source mismatch for pathspec/patterns/gitwildmatch.py: exact old text must occur once; no patch was created |
| cachetools-395 | unsupported | unsupported | - | - | no | - | - | - | - | 0 | correct_rejection | eligibility=unsupported |
| semver-429 | unsupported | eligible | - | - | no | - | - | - | 3 | 12716 | missed_rejection | eligibility=eligible |
