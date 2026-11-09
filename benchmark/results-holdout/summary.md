# IssuePilot mini benchmark results

Denominators exclude cases that never reached a stage. Cases with a sandbox baseline that does not run cleanly are counted as infrastructure failures, not agent failures.

| Metric | Value |
| --- | --- |
| cases | 13 |
| eligibility_accuracy | 8/13 |
| eligible_cases_flagged_needs_review | 1 |
| discovery_full_recall | 6/6 |
| discovery_mean_recall | 1.0 |
| test_file_hit | 4/6 |
| patch_generated | 5/10 |
| patch_apply_rate | 5/5 |
| test_pass_rate | 2/2 |
| regression_validated | 2/2 |
| infrastructure_failures | 3 |
| mean_tool_calls | 5.3 |
| mean_tokens | 28249 |
| outcomes | correct_rejection: 3, infrastructure: 3, patch_failed: 1, unsupported: 4, validated: 2 |

| Case | Expected | Eligibility | Selected source | Recall | Patch | Applies | Tests | Regression | Tool calls | Tokens | Outcome | Reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| packaging-938 | eligible | eligible | src/packaging/markers.py | 1.0 | yes | yes | no | - | 4 | 53102 | infrastructure | baseline test suite does not run cleanly in the sandbox |
| packaging-660 | eligible | needs_review | src/packaging/utils.py | 1.0 | yes | yes | no | - | 5 | 41631 | infrastructure | baseline test suite does not run cleanly in the sandbox |
| markdown-it-py-415 | eligible | eligible | markdown_it/rules_block/heading.py, markdown_it/rules_block/html_block.py | 1.0 | yes | yes | no | - | 5 | 42828 | infrastructure | baseline test suite does not run cleanly in the sandbox |
| markdown-it-py-352 | eligible | unsupported | - | - | no | - | - | - | - | 1342 | unsupported | stage=eligibility: Classified as feature_request. |
| tomli-223 | eligible | unsupported | - | - | no | - | - | - | - | 0 | unsupported | stage=eligibility: Issue labels mark it as out of scope: enhancement. |
| tomli-1 | eligible | unsupported | - | - | no | - | - | - | - | 0 | unsupported | stage=eligibility: Issue labels mark it as out of scope: enhancement. |
| mdformat-507 | eligible | unsupported | - | - | no | - | - | - | - | 0 | unsupported | stage=eligibility: Issue labels mark it as out of scope: enhancement. |
| mdformat-357 | eligible | eligible | src/mdformat/_util.py | 1.0 | no | - | - | - | 9 | 70326 | patch_failed | Empty, oversized, or ineffective replacement for tests/test_cli.py |
| markupsafe-417 | eligible | eligible | src/markupsafe/__init__.py | 1.0 | yes | yes | yes | yes | 4 | 33398 | validated |  |
| markupsafe-310 | eligible | eligible | src/markupsafe/__init__.py | 1.0 | yes | yes | yes | yes | 5 | 34060 | validated |  |
| packaging-1185 | unsupported | unsupported | - | - | no | - | - | - | - | 1947 | correct_rejection | eligibility=unsupported |
| tenacity-523 | unsupported | unsupported | - | - | no | - | - | - | - | 1444 | correct_rejection | eligibility=unsupported |
| markdown-it-py-295 | unsupported | unsupported | - | - | no | - | - | - | - | 2408 | correct_rejection | eligibility=unsupported |
