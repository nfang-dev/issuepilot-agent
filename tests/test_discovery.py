"""Bounded file discovery on synthetic projects with a scripted model (offline)."""
import json

import pytest

from issuepilot.discovery import (
    DISCOVERY_TOOLS, NEEDS_REVIEW, SELECTED, SUBMIT_TOOL, UNSUPPORTED, PinnedRepoTools,
    discover_files, module_name, validate_selection,
)
from synthetic_github import (
    FASTVEC_SHA, RETRYKIT_SHA, ROWKIT_SHA, ScriptedModel, api_for, fastvec, retrykit, rowkit,
    submit, text_reply, tool_call,
)

PARSER = "src/rowkit/parser.py"
PARSER_TEST = "tests/test_parser.py"


def run(repo, number, *steps, **budgets):
    api, fake = api_for(repo, number)
    model = ScriptedModel(*(("discovery", s) for s in steps))
    result = discover_files(api, model, api.get_issue(), api.get_issue_comments(), repo.head, **budgets)
    return result, model, fake


def outputs(request):
    """Tool outputs the model received in a request, decoded."""
    return [json.loads(i["output"]) for i in request["input"]
            if isinstance(i, dict) and i.get("type") == "function_call_output"]


def test_src_layout_issue_selects_implementation_and_matching_test():
    result, model, fake = run(
        rowkit(), 12,
        tool_call("search_code", query="split_row", path_prefix=""),
        tool_call("read_file", path=PARSER),
        tool_call("find_test_files", source_path=PARSER),
        submit(source_files=[PARSER], test_files=[PARSER_TEST],
               evidence=[{"path": PARSER, "detail": "delimiter branch ignores in_quotes"}]),
    )
    assert result.status == SELECTED and result.reason == "grounded_selection"
    assert result.files == [PARSER, PARSER_TEST]
    assert result.revision == ROWKIT_SHA
    assert result.exploration_calls == 3 and [c["ok"] for c in result.tool_calls] == [True] * 3
    # search hits are linked to the pinned commit, not a branch
    hits = outputs(model.requests[1])[0]["data"]["matches"]
    assert hits and all(f"/blob/{ROWKIT_SHA}/" in h["url"] for h in hits)
    tests = outputs(model.requests[3])[-1]["data"]["tests"]
    assert [t["path"] for t in tests] == [PARSER_TEST]
    # The prompt carries the issue and a pinned file listing; secrets are not listed.
    prompt = model.requests[0]["input"][0]["content"]
    assert ROWKIT_SHA in prompt and "split_row" in prompt and ".env" not in prompt


def test_flat_layout_with_nested_tests_and_ast_inspection():
    result, _, _ = run(
        retrykit(), 3,
        tool_call("inspect_python_symbol", path="retrykit/backoff.py", symbol="exponential_delays"),
        tool_call("find_test_files", source_path="retrykit/backoff.py"),
        submit(source_files=["retrykit/backoff.py"], test_files=["tests/unit/test_backoff.py"]),
    )
    assert result.status == SELECTED
    assert result.files == ["retrykit/backoff.py", "tests/unit/test_backoff.py"]
    assert result.revision == RETRYKIT_SHA


def test_every_read_is_pinned_to_one_commit():
    repo = rowkit()
    _, _, fake = run(
        repo, 12,
        tool_call("search_code", query="split_row", path_prefix="src/"),
        tool_call("read_file", path="src/rowkit/writer.py"),
        submit(decision="needs_review", confidence="low", rationale="Not enough evidence yet."),
    )
    contents = fake.content_requests()
    assert contents and all(params == {"ref": ROWKIT_SHA} for _, params in contents)
    trees = [p for p, _ in fake.requests if "/git/trees/" in p]
    assert trees == [f"/repos/acme/rowkit/git/trees/{ROWKIT_SHA}"]
    assert not any(p.endswith("/commits/main") for p, _ in fake.requests)


# ------------------------------------------------------- wrong selections

@pytest.fixture
def primed_tools():
    api, _ = api_for(rowkit(), 12)
    tools = PinnedRepoTools(api, ROWKIT_SHA)
    tools.read_file(PARSER)
    tools.find_test_files(PARSER)
    return tools


def selection(**overrides):
    data = {"decision": "selected", "source_files": [PARSER], "test_files": [PARSER_TEST],
            "confidence": "high", "rationale": "Faulty branch found.", "evidence": []}
    data.update(overrides)
    return data


def test_valid_selection_is_accepted(primed_tools):
    problems, normalized = validate_selection(selection(), primed_tools)
    assert problems == [] and normalized["source_files"] == [PARSER]


@pytest.mark.parametrize("overrides, expected", [
    ({"source_files": ["src/rowkit/csv_parser.py"]}, "does not exist at the pinned commit"),
    ({"source_files": ["src/rowkit/writer.py"]}, "never read"),
    ({"source_files": [PARSER_TEST], "test_files": []}, "is a test module"),
    ({"test_files": ["src/rowkit/writer.py"]}, "is not a test module"),
    ({"test_files": ["tests/test_writer.py"]}, "never appeared"),
    ({"source_files": ["docs/conf.py"]}, "not library source"),
    ({"source_files": ["docs/usage.md"]}, "not a permitted .py path"),
    ({"source_files": ["../secrets.py"]}, "not a permitted .py path"),
    ({"source_files": [".env"]}, "not a permitted .py path"),
    ({"source_files": []}, "select 1-3 source files"),
    ({"source_files": [PARSER, PARSER]}, "more than once"),
    ({"test_files": [PARSER_TEST, "tests/test_writer.py", "tests/test_parser.py"]}, "at most 2 test files"),
    ({"evidence": [{"path": "src/rowkit/writer.py", "detail": "invented"}]}, "not returned by any tool"),
    ({"decision": "fixed"}, "decision must be"),
    ({"source_files": "src/rowkit/parser.py"}, "lists of strings"),
    ({"rationale": "  "}, "rationale must explain"),
])
def test_wrong_file_selections_are_rejected(primed_tools, overrides, expected):
    problems, _ = validate_selection(selection(**overrides), primed_tools)
    assert any(expected in p for p in problems), problems


def test_selection_with_missing_fields_is_rejected(primed_tools):
    problems, _ = validate_selection({"decision": "selected"}, primed_tools)
    assert problems and "exactly" in problems[0]


def test_rejected_selection_is_explained_to_model_then_corrected():
    result, model, _ = run(
        rowkit(), 12,
        tool_call("read_file", path=PARSER),
        submit(source_files=["src/rowkit/writer.py"], test_files=["tests/test_quoting.py"]),
        tool_call("find_test_files", source_path=PARSER),
        submit(source_files=[PARSER], test_files=[PARSER_TEST]),
    )
    assert result.status == SELECTED and result.files == [PARSER, PARSER_TEST]
    first = result.submissions[0]
    assert first["accepted"] is False
    assert any("writer.py: never read" in p for p in first["problems"])
    assert any("tests/test_quoting.py: does not exist" in p for p in first["problems"])
    feedback = outputs(model.requests[2])[-1]
    assert feedback["error"] == "Selection rejected" and feedback["budget"]["submissions_remaining"] == 2
    assert [s["accepted"] for s in result.submissions] == [False, True]


def test_repeated_invalid_selections_end_in_needs_review():
    result, model, _ = run(
        rowkit(), 12,
        tool_call("read_file", path=PARSER),
        submit(source_files=["src/rowkit/missing.py"]),
        submit(source_files=[PARSER_TEST]),
        submit(source_files=["src/rowkit/writer.py"]),
    )
    assert result.status == NEEDS_REVIEW and result.reason == "selection_validation_failed"
    assert len(result.submissions) == 3 and not any(s["accepted"] for s in result.submissions)
    assert result.provisional["source_files"] == [PARSER]  # read evidence ranks first
    assert len(model.requests) == 4  # stopped; no further model calls


# ------------------------------------------------------ budgets & failures

def test_tool_budget_exhaustion_gives_one_submit_only_round_then_needs_review():
    result, model, _ = run(
        rowkit(), 12,
        tool_call("search_code", query="split_row", path_prefix=""),
        tool_call("read_file", path=PARSER),
        tool_call("search_code", query="in_quotes", path_prefix=""),
        tool_call("read_file", path="src/rowkit/writer.py"),  # grace round: refused
        max_tool_calls=3,
    )
    assert result.status == NEEDS_REVIEW and result.reason == "tool_budget_exhausted"
    assert result.exploration_calls == 3
    grace = model.requests[-1]
    assert grace["tools"] == [SUBMIT_TOOL] and grace["tool_choice"] == "required"
    assert all(r["tools"] == DISCOVERY_TOOLS + [SUBMIT_TOOL] for r in model.requests[:-1])
    assert result.tool_calls[-1] == {"round": 4, "name": "read_file", "ok": False,
                                     "error": "exploration budget exhausted"}
    assert result.provisional["source_files"][0] == PARSER
    remaining = [o["budget"]["exploration_calls_remaining"] for o in outputs(model.requests[-1])]
    assert remaining == [2, 1, 0]


def test_valid_submission_in_grace_round_is_accepted_with_warning():
    result, _, _ = run(
        rowkit(), 12,
        tool_call("read_file", path=PARSER),
        tool_call("find_test_files", source_path=PARSER),
        submit(source_files=[PARSER], test_files=[PARSER_TEST]),
        max_tool_calls=2,
    )
    assert result.status == SELECTED
    assert any("exploration budget was exhausted" in w for w in result.warnings)


def test_round_budget_exhaustion_when_model_never_calls_tools():
    result, model, _ = run(rowkit(), 12, text_reply("I think it is parser.py"),
                           text_reply("Definitely parser.py"), max_rounds=2)
    assert result.status == NEEDS_REVIEW and result.reason == "round_budget_exhausted"
    assert result.source_files == []
    assert model.requests[1]["input"][-1]["content"].startswith("Respond only with tool calls")


def test_repeated_identical_calls_are_cached_but_still_count():
    result, _, fake = run(
        rowkit(), 12,
        tool_call("read_file", path=PARSER),
        tool_call("read_file", path=PARSER),
        submit(decision="needs_review", confidence="low", rationale="stop"),
    )
    assert result.tool_calls[1]["cached"] is True and result.exploration_calls == 2
    assert len(fake.content_requests()) == 1


def test_github_failure_is_reported_to_model_not_raised():
    repo = rowkit()
    api, fake = api_for(repo, 12)
    fake.fail_paths.add(PARSER)
    model = ScriptedModel(
        ("discovery", tool_call("read_file", path=PARSER)),
        ("discovery", submit(decision="needs_review", confidence="low",
                             rationale="Could not read the parser module.")),
    )
    result = discover_files(api, model, api.get_issue(), [], ROWKIT_SHA)
    assert result.status == NEEDS_REVIEW and result.reason == "model_needs_review"
    assert result.tool_calls[0]["ok"] is False and "500" in result.tool_calls[0]["error"]
    assert PARSER not in result.provisional["source_files"]  # failed reads are not evidence


def test_transient_github_error_is_not_cached():
    repo = rowkit()
    api, fake = api_for(repo, 12)
    fake.fail_paths.add(PARSER)

    def recover(_request):
        fake.fail_paths.clear()
        return tool_call("read_file", path=PARSER)

    model = ScriptedModel(
        ("discovery", tool_call("read_file", path=PARSER)),
        ("discovery", recover),
        ("discovery", submit(source_files=[PARSER])),
    )
    result = discover_files(api, model, api.get_issue(), [], ROWKIT_SHA)
    assert [c["ok"] for c in result.tool_calls] == [False, True]
    assert "cached" not in result.tool_calls[1]
    assert result.status == SELECTED and result.exploration_calls == 2


def test_unsafe_or_unknown_tool_requests_never_reach_github():
    result, model, fake = run(
        rowkit(), 12,
        tool_call("read_file", path=".env"),
        tool_call("read_file", path="../outside.py"),
        tool_call("list_python_files", path_prefix="../"),
        tool_call("delete_branch", branch="main"),
        tool_call("read_file", path=PARSER, ref="main"),
        submit(decision="needs_review", confidence="low", rationale="Nothing usable."),
    )
    assert [c["ok"] for c in result.tool_calls] == [False] * 5
    assert "Tool not allowed" in result.tool_calls[3]["error"]
    assert fake.content_requests() == []


def test_file_fetch_budget_bounds_github_traffic():
    result, _, fake = run(
        rowkit(), 12,
        tool_call("search_code", query="row", path_prefix=""),  # fetches 2 files, then budget hits
        tool_call("read_file", path="src/rowkit/writer.py"),
        submit(decision="needs_review", confidence="low", rationale="stop"),
        max_file_fetches=2,
    )
    assert result.file_fetches == 2 and len(fake.content_requests()) == 2
    assert "budget exhausted" in result.tool_calls[1]["error"]


def test_model_can_declare_bug_outside_python_unsupported():
    result, _, _ = run(
        fastvec(), 5,
        tool_call("search_code", query="dot", path_prefix=""),
        tool_call("read_file", path="fastvec/__init__.py"),
        submit(decision="unsupported", confidence="medium",
               rationale="dot is imported from the compiled fastvec._native extension (src/dot.cpp)."),
    )
    assert result.status == UNSUPPORTED and result.reason == "model_unsupported"
    assert result.revision == FASTVEC_SHA and result.source_files == []


def test_low_confidence_selection_is_not_used_for_patching():
    result, _, _ = run(
        rowkit(), 12,
        tool_call("read_file", path=PARSER),
        submit(source_files=[PARSER], confidence="low"),
    )
    assert result.status == NEEDS_REVIEW and result.reason == "low_confidence"
    assert result.source_files == [] and result.provisional["source_files"] == [PARSER]


def test_malformed_tool_arguments_are_errors():
    from synthetic_github import raw_tool_call
    result, _, _ = run(
        rowkit(), 12,
        raw_tool_call("read_file", "{not json"),
        raw_tool_call("submit_file_selection", "[]"),
        submit(decision="needs_review", confidence="low", rationale="stop"),
    )
    assert result.tool_calls[0]["ok"] is False
    assert result.submissions[0]["accepted"] is False


@pytest.mark.parametrize("path, dotted", [
    ("src/pkg/mod.py", "pkg.mod"), ("pkg/__init__.py", "pkg"), ("lib/a/b/c.py", "a.b.c"),
])
def test_module_name(path, dotted):
    assert module_name(path) == dotted
