"""URL-only pipeline and CLI on synthetic issues (offline, scripted model).

The end-to-end tests apply the generated diff to a local copy of a synthetic
project and run that project's tests. This checks the pipeline mechanics
(pinning, grounding, diff rendering, manifest provenance) when the model is
scripted to propose the right edit; it does not measure live model quality.
"""
import json
import shutil
from pathlib import Path

import pytest

import issuepilot
import issuepilot.patch_generator as pg
from issuepilot.discovery import DISCOVERY_SYSTEM
from issuepilot.eligibility import ELIGIBILITY_SYSTEM
from issuepilot.github_api import GitHubAPI
from issuepilot.pipeline import (
    DRAFT_READY, ERROR, NEEDS_REVIEW, PATCH_FAILED, STOPPED_ELIGIBLE, STOPPED_SELECTED, UNSUPPORTED,
    run_auto,
)
from issuepilot.sandbox import stage_source, verify_patch_manifest
from synthetic_github import (
    RETRYKIT_BACKOFF, RETRYKIT_FILES, RETRYKIT_TEST_BACKOFF, ROWKIT_FILES, ROWKIT_PARSER, ROWKIT_SHA,
    ROWKIT_SHA_V2, ROWKIT_TEST_PARSER, FakeGitHub, ScriptedModel, api_for, classification, git_apply,
    json_reply, materialize, patch_plan, pixelui, retrykit, rowkit, run_pytest, submit, tool_call,
)

PARSER = "src/rowkit/parser.py"
PARSER_TEST = "tests/test_parser.py"
PARSER_FIX = (PARSER, "        elif char == delimiter:\n",
              "        elif char == delimiter and not in_quotes:\n")
PARSER_REGRESSION = (PARSER_TEST, ROWKIT_TEST_PARSER.splitlines(keepends=True)[-1],
                     ROWKIT_TEST_PARSER.splitlines(keepends=True)[-1]
                     + "\n\ndef test_delimiter_inside_quotes():\n"
                       "    assert split_row('a,\"b,c\",d') == [\"a\", \"b,c\", \"d\"]\n")
ROWKIT_DISCOVERY = [
    tool_call("search_code", query="split_row", path_prefix=""),
    tool_call("read_file", path=PARSER),
    tool_call("find_test_files", source_path=PARSER),
    submit(source_files=[PARSER], test_files=[PARSER_TEST],
           rationale="split_row toggles in_quotes but the delimiter branch ignores it."),
]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is required to apply diffs")


def script(*discovery, eligibility=None, patch=None):
    steps = [("eligibility", json_reply(eligibility or classification(mentioned_symbols=["split_row"])))]
    steps += [("discovery", s) for s in discovery]
    if patch is not None:
        steps.append(("patch", json_reply(patch)))
    return ScriptedModel(*steps)


def assert_regression_test_catches_bug(tmp_path: Path, files: dict, patch: str, source: str):
    root = tmp_path / "checkout"
    materialize(files, root)
    applied = git_apply(root, patch)
    assert applied.returncode == 0, applied.stderr
    fixed = run_pytest(root)
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    (root / source).write_text(files[source], encoding="utf-8")  # revert only the source fix
    broken = run_pytest(root)
    assert broken.returncode == 1 and "1 failed" in broken.stdout, broken.stdout


@needs_git
def test_url_only_run_produces_pinned_grounded_draft_for_src_layout(tmp_path):
    api, fake = api_for(rowkit(), 12)
    model = script(*ROWKIT_DISCOVERY, patch=patch_plan(PARSER_FIX, PARSER_REGRESSION))
    result = run_auto(api, model)

    assert result.status == DRAFT_READY and result.exit_code == 0
    assert model.stages() == ["eligibility"] + ["discovery"] * 4 + ["patch"]
    manifest = result.manifest
    assert manifest["base_commit"] == result.revision == ROWKIT_SHA
    assert manifest["selected_files"] == [PARSER, PARSER_TEST]
    assert manifest["selection"]["mode"] == "automatic"
    assert manifest["status"] == "draft_unverified"
    # Source-grounded: the patch request carries the exact pinned file bytes and the rationale.
    patch_prompt = model.requests[-1]["input"][0]["content"]
    assert ROWKIT_PARSER in patch_prompt and "delimiter branch ignores it" in patch_prompt
    # The default branch is resolved exactly once; every file read uses the pinned SHA.
    assert sum(p.endswith("/commits/main") for p, _ in fake.requests) == 1
    assert all(q.get("ref") == ROWKIT_SHA for _, q in fake.content_requests())
    # The manifest is accepted by the existing pre-Docker provenance check.
    checkout, staged = tmp_path / "pinned", tmp_path / "staged"
    materialize(ROWKIT_FILES, checkout)
    staged.mkdir()
    stage_source(checkout, staged)
    (tmp_path / "fix.patch").write_text(result.patch)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    verify_patch_manifest(tmp_path / "manifest.json", tmp_path / "fix.patch", staged, checkout)
    assert_regression_test_catches_bug(tmp_path, ROWKIT_FILES, result.patch, PARSER)


@needs_git
def test_url_only_run_on_flat_layout_project(tmp_path):
    api, _ = api_for(retrykit(), 3)
    regression = (RETRYKIT_TEST_BACKOFF.splitlines(keepends=True)[-1],)
    model = script(
        tool_call("inspect_python_symbol", path="retrykit/backoff.py", symbol="exponential_delays"),
        tool_call("find_test_files", source_path="retrykit/backoff.py"),
        submit(source_files=["retrykit/backoff.py"], test_files=["tests/unit/test_backoff.py"]),
        patch=patch_plan(
            ("retrykit/backoff.py", "    for _ in range(retries - 1):\n", "    for _ in range(retries):\n"),
            ("tests/unit/test_backoff.py", regression[0], regression[0]
             + "\n\ndef test_one_delay_per_retry():\n    assert exponential_delays(1, 2, 3) == [1, 2, 4]\n"),
        ),
    )
    result = run_auto(api, model)
    assert result.status == DRAFT_READY
    assert result.manifest["source_sha256"].keys() == {"retrykit/backoff.py", "tests/unit/test_backoff.py"}
    assert RETRYKIT_BACKOFF in model.requests[-1]["input"][0]["content"]
    assert_regression_test_catches_bug(tmp_path, RETRYKIT_FILES, result.patch, "retrykit/backoff.py")


def test_branch_moving_mid_run_does_not_change_the_pinned_sources():
    repo = rowkit()
    api, fake = api_for(repo, 12)

    def submit_then_branch_moves(_request):
        repo.advance(ROWKIT_SHA_V2, {PARSER: ROWKIT_PARSER.replace("Minimal", "Upstream-edited")})
        return ROWKIT_DISCOVERY[-1]

    model = script(*ROWKIT_DISCOVERY[:-1], submit_then_branch_moves,
                   patch=patch_plan(PARSER_FIX, PARSER_REGRESSION))
    result = run_auto(api, model)
    assert result.status == DRAFT_READY
    assert result.manifest["base_commit"] == ROWKIT_SHA != repo.head
    assert "Upstream-edited" not in model.requests[-1]["input"][0]["content"]
    assert all(q.get("ref") == ROWKIT_SHA for _, q in fake.content_requests())


def test_ineligible_issue_stops_before_any_model_call():
    api, _ = api_for(pixelui(), 1)
    model = ScriptedModel()
    result = run_auto(api, model)
    assert (result.status, result.stage, result.exit_code) == (UNSUPPORTED, "eligibility", 3)
    assert model.requests == [] and result.discovery is None


def test_pull_request_url_is_unsupported():
    api, _ = api_for(rowkit(), 17)
    result = run_auto(api, ScriptedModel())
    assert result.status == UNSUPPORTED and "not PRs" in result.detail


def test_discovery_budget_exhaustion_never_reaches_patch_generation():
    api, _ = api_for(rowkit(), 12)
    model = script(tool_call("read_file", path=PARSER),
                   tool_call("search_code", query="in_quotes", path_prefix=""))
    result = run_auto(api, model, max_tool_calls=1)
    assert (result.status, result.stage, result.exit_code) == (NEEDS_REVIEW, "discovery", 4)
    assert result.discovery["reason"] == "tool_budget_exhausted"
    assert result.discovery["provisional"]["source_files"] == [PARSER]
    assert "patch" not in model.stages() and result.patch is None


@pytest.mark.parametrize("plan, detail", [
    (patch_plan((PARSER, "        elif char == sep:\n", "        elif char == sep and not quoted:\n")),
     "Source mismatch"),
    (patch_plan(PARSER_REGRESSION), "only edits test files"),
    ({**patch_plan(), "edits": []}, "1-6 concrete source edits"),
    (patch_plan((PARSER, "        elif char == delimiter:\n", "        elif char == delimiter\n")),
     "syntax"),
])
def test_patch_generation_failures_fail_closed(plan, detail):
    api, _ = api_for(rowkit(), 12)
    result = run_auto(api, script(*ROWKIT_DISCOVERY, patch=plan))
    assert (result.status, result.stage, result.exit_code) == (PATCH_FAILED, "patch_generation", 5)
    assert detail in result.detail
    assert result.patch is None and result.manifest is None


def test_human_can_continue_past_needs_review_but_not_unsupported():
    model = script(*ROWKIT_DISCOVERY, eligibility=classification(requires_design_decision=True))
    api, _ = api_for(rowkit(), 12)
    blocked = run_auto(api, model, stop_after="discovery")
    assert blocked.status == NEEDS_REVIEW and blocked.eligibility["human_override"] is False

    api, _ = api_for(rowkit(), 12)
    model = script(*ROWKIT_DISCOVERY, eligibility=classification(requires_design_decision=True))
    result = run_auto(api, model, stop_after="discovery", accept_needs_review=True)
    assert result.status == STOPPED_SELECTED
    assert result.eligibility["status"] == NEEDS_REVIEW and result.eligibility["human_override"] is True

    api, _ = api_for(pixelui(), 1)
    result = run_auto(api, ScriptedModel(), accept_needs_review=True)
    assert result.status == UNSUPPORTED and result.eligibility["human_override"] is False


def test_stop_after_eligibility_and_discovery():
    api, _ = api_for(rowkit(), 12)
    model = script()
    assert run_auto(api, model, stop_after="eligibility").status == STOPPED_ELIGIBLE
    assert model.stages() == ["eligibility"]
    api, _ = api_for(rowkit(), 12)
    model = script(*ROWKIT_DISCOVERY)
    result = run_auto(api, model, stop_after="discovery")
    assert result.status == STOPPED_SELECTED and result.exit_code == 0
    assert result.discovery["source_files"] == [PARSER] and "patch" not in model.stages()


def test_github_errors_become_error_status():
    api, _ = api_for(rowkit(), 404)
    result = run_auto(api, ScriptedModel())
    assert (result.status, result.exit_code) == (ERROR, 1)
    assert "HTTPError" in result.detail


def test_usage_is_metered_per_stage_and_record_has_no_source_text():
    api, _ = api_for(rowkit(), 12)
    result = run_auto(api, script(*ROWKIT_DISCOVERY, patch=patch_plan(PARSER_FIX, PARSER_REGRESSION)))
    assert result.usage["eligibility"]["requests"] == 1
    assert result.usage["discovery"] == {"requests": 4, "input_tokens": 200, "output_tokens": 40}
    assert result.usage["patch_generation"]["requests"] == 1
    record = json.dumps(result.record())
    assert "in_quotes = not in_quotes" not in record and "patch_sha256" in record
    assert '"patch":' not in record


# ------------------------------------------------------------------- CLI

@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    def install(repo, model):
        fake = FakeGitHub(repo)
        monkeypatch.setattr(pg, "GitHubAPI", lambda ref: GitHubAPI(ref, session=fake))
        monkeypatch.setattr(pg, "_openai_client", lambda: model)
        return fake
    return install


def test_cli_manual_files_mode_is_unchanged(cli, tmp_path):
    model = ScriptedModel(("patch", json_reply(patch_plan(PARSER_FIX, PARSER_REGRESSION))))
    cli(rowkit(), model)
    pg.main(["https://github.com/acme/rowkit/issues/12", "--files", PARSER, PARSER_TEST])
    assert model.stages() == ["patch"]  # no eligibility or discovery calls
    manifest = json.loads((tmp_path / "issuepilot-patch-manifest.json").read_text())
    assert manifest["base_commit"] == ROWKIT_SHA and "selection" not in manifest
    assert (tmp_path / "issuepilot-fix.patch").read_text().startswith("diff --git a/")
    assert not (tmp_path / "issuepilot-run.json").exists()


def test_cli_url_only_mode_writes_draft_and_run_record(cli, tmp_path):
    cli(rowkit(), script(*ROWKIT_DISCOVERY, patch=patch_plan(PARSER_FIX, PARSER_REGRESSION)))
    pg.main(["https://github.com/acme/rowkit/issues/12"])
    record = json.loads((tmp_path / "issuepilot-run.json").read_text())
    assert record["status"] == DRAFT_READY and record["revision"] == ROWKIT_SHA
    assert record["discovery"]["source_files"] == [PARSER]
    assert (tmp_path / "issuepilot-fix.patch").exists()


def test_cli_unsupported_issue_exits_nonzero_without_patch(cli, tmp_path):
    cli(pixelui(), ScriptedModel())
    with pytest.raises(SystemExit) as exc:
        pg.main(["https://github.com/acme/pixelui/issues/1"])
    assert exc.value.code == 3
    assert json.loads((tmp_path / "issuepilot-run.json").read_text())["status"] == UNSUPPORTED
    assert not (tmp_path / "issuepilot-fix.patch").exists()


@pytest.mark.parametrize("argv", [
    ["--files", PARSER, "--stop-after", "discovery"],
    ["--files", PARSER, "--max-tool-calls", "5"],
    ["--max-tool-calls", "0"],
    ["--revision", "main"],
    ["--output", "same.json", "--manifest", "same.json"],
])
def test_cli_rejects_invalid_option_combinations(cli, argv):
    cli(rowkit(), ScriptedModel())
    with pytest.raises(SystemExit) as exc:
        pg.main(["https://github.com/acme/rowkit/issues/12", *argv])
    assert exc.value.code == 2


def test_cli_stop_after_ignores_existing_patch_outputs(cli, tmp_path):
    cli(rowkit(), script(*ROWKIT_DISCOVERY))
    (tmp_path / "issuepilot-fix.patch").write_text("previous draft")
    (tmp_path / "issuepilot-patch-manifest.json").write_text("{}")
    pg.main(["https://github.com/acme/rowkit/issues/12", "--stop-after", "discovery"])
    record = json.loads((tmp_path / "issuepilot-run.json").read_text())
    assert record["status"] == STOPPED_SELECTED
    assert (tmp_path / "issuepilot-fix.patch").read_text() == "previous draft"  # untouched


def test_cli_stop_after_still_protects_the_run_record(cli, tmp_path):
    cli(rowkit(), ScriptedModel())
    (tmp_path / "issuepilot-run.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        pg.main(["https://github.com/acme/rowkit/issues/12", "--stop-after", "eligibility"])
    assert exc.value.code == 2


def test_cli_refuses_to_overwrite_outputs(cli, tmp_path):
    cli(rowkit(), ScriptedModel())
    (tmp_path / "issuepilot-run.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        pg.main(["https://github.com/acme/rowkit/issues/12"])
    assert exc.value.code == 2


# ----------------------------------------------------- no hardcoding guard

FIXTURE_AND_ISSUE_TOKENS = ("itsdangerous", "max_age", "#429", "payload", "expiry", "rowkit",
                            "retrykit", "fastvec", "split_row", "exponential_delays", "in_quotes")


@pytest.mark.parametrize("prompt", [DISCOVERY_SYSTEM, ELIGIBILITY_SYSTEM, pg.SYSTEM])
def test_prompts_have_no_issue_specific_hardcoding(prompt):
    assert not [t for t in FIXTURE_AND_ISSUE_TOKENS if t in prompt.lower()]


@pytest.mark.parametrize("module", ["eligibility.py", "discovery.py", "pipeline.py"])
def test_pipeline_code_has_no_fixture_specific_logic(module):
    source = (Path(issuepilot.__file__).parent / module).read_text(encoding="utf-8").lower()
    assert not [t for t in FIXTURE_AND_ISSUE_TOKENS if t in source]
