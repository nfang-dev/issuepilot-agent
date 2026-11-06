"""Eligibility gate on synthetic issues from several projects (offline, deterministic)."""
import pytest

from issuepilot.eligibility import (
    ELIGIBLE, NEEDS_REVIEW, UNSUPPORTED, check_eligibility, is_library_source, is_test_path,
    parse_classification,
)
from synthetic_github import (
    ROWKIT_SHA, ScriptedModel, api_for, classification, geomcore, json_reply, pixelui, retrykit,
    rowkit, text_reply,
)


def evaluate(repo, number, *steps, explicit_revision=False):
    api, _ = api_for(repo, number)
    model = ScriptedModel(*steps)
    revision = api.resolve_revision(repo.head if explicit_revision else None)
    decision = check_eligibility(api.get_issue(), api.get_issue_comments(), api.get_repository_metadata(),
                                 api.list_tree_at_revision(revision), model,
                                 explicit_revision=explicit_revision)
    return decision, model


def codes(decision):
    return [r["code"] for r in decision.reasons]


def test_supported_logic_bug_is_eligible_and_hints_are_grounded():
    decision, model = evaluate(rowkit(), 12, ("eligibility", json_reply(classification(
        mentioned_symbols=["split_row", "parse_csv_fast", "__import__('os')"]))))
    assert decision.status == ELIGIBLE
    assert decision.model_consulted and len(model.requests) == 1
    # Identifiers absent from the issue text (or not identifiers at all) are dropped.
    assert decision.mentioned_symbols == ["split_row"]
    request = model.requests[0]
    assert request["text"]["format"]["strict"] is True
    assert "Same here with semicolons" in request["input"][0]["content"]  # comments included


def test_flat_layout_project_with_prefixed_bug_label_is_eligible():
    decision, _ = evaluate(retrykit(), 3, ("eligibility", json_reply(classification())))
    assert decision.status == ELIGIBLE
    assert decision.signals["bug_labels"] == ["type: bug"]


def test_repository_without_python_source_is_unsupported_without_model_call():
    decision, model = evaluate(pixelui(), 1)
    assert decision.status == UNSUPPORTED
    assert "no_python_library_source" in codes(decision)
    assert model.requests == [] and not decision.model_consulted


def test_feature_label_is_unsupported_without_model_call():
    decision, model = evaluate(rowkit(), 13)
    assert decision.status == UNSUPPORTED
    assert codes(decision) == ["blocking_label"]
    assert model.requests == []


def test_conflicting_labels_need_review():
    decision, model = evaluate(rowkit(), 18)
    assert decision.status == NEEDS_REVIEW
    assert codes(decision) == ["conflicting_labels"]
    assert model.requests == []


def test_closed_issue_needs_review_unless_revision_is_pinned_explicitly():
    decision, model = evaluate(rowkit(), 15)
    assert decision.status == NEEDS_REVIEW and codes(decision) == ["issue_not_open"]
    assert model.requests == []
    pinned, model = evaluate(rowkit(), 15, ("eligibility", json_reply(classification())),
                             explicit_revision=True)
    assert pinned.status == ELIGIBLE and pinned.signals["explicit_revision"] is True


def test_non_python_primary_language_needs_review():
    decision, _ = evaluate(geomcore(), 2)
    assert decision.status == NEEDS_REVIEW
    assert codes(decision) == ["primary_language_not_python"]


def test_archived_repository_is_unsupported():
    repo = rowkit()
    repo.archived = True
    decision, _ = evaluate(repo, 12)
    assert decision.status == UNSUPPORTED and "repository_archived" in codes(decision)


@pytest.mark.parametrize("fields, status, code", [
    ({"category": "documentation"}, UNSUPPORTED, "category_documentation"),
    ({"category": "feature_request"}, UNSUPPORTED, "category_feature_request"),
    ({"category": "performance"}, UNSUPPORTED, "category_performance"),
    ({"affects_python_code": False}, UNSUPPORTED, "not_python_code"),
    ({"category": "security_sensitive"}, NEEDS_REVIEW, "category_security_sensitive"),
    ({"requires_design_decision": True}, NEEDS_REVIEW, "requires_design_decision"),
    ({"confidence": "low"}, NEEDS_REVIEW, "low_confidence"),
    ({"has_reproduction": False, "describes_expected_vs_actual": False}, NEEDS_REVIEW, "no_reproduction"),
    ({"category": "crash_or_exception"}, ELIGIBLE, "category_crash_or_exception"),
])
def test_policy_table_overrides_model_wording(fields, status, code):
    decision, _ = evaluate(rowkit(), 12, ("eligibility", json_reply(classification(**fields))))
    assert decision.status == status
    assert codes(decision) == [code]


def test_docs_issue_classified_as_documentation_is_unsupported():
    decision, _ = evaluate(rowkit(), 14, ("eligibility", json_reply(classification("documentation"))))
    assert decision.status == UNSUPPORTED


def test_vague_issue_with_low_confidence_needs_review():
    decision, _ = evaluate(rowkit(), 16, ("eligibility", json_reply(classification(confidence="low"))))
    assert decision.status == NEEDS_REVIEW


@pytest.mark.parametrize("raw", [
    "not json",
    '{"category": "logic_bug"}',
    '{"category": "made_up", "affects_python_code": true, "has_reproduction": true, '
    '"describes_expected_vs_actual": true, "requires_design_decision": false, '
    '"confidence": "high", "rationale": "x", "mentioned_symbols": []}',
])
def test_malformed_classifier_output_fails_closed(raw):
    decision, _ = evaluate(rowkit(), 12, ("eligibility", text_reply(raw)))
    assert decision.status == NEEDS_REVIEW
    assert codes(decision) == ["classifier_output_invalid"]


def test_parse_classification_rejects_non_boolean_flags():
    data = classification(has_reproduction="yes")
    import json
    assert parse_classification(json.dumps(data), "text") is None


def test_pull_request_url_is_rejected_by_api():
    api, _ = api_for(rowkit(), 17)
    with pytest.raises(ValueError, match="not PRs"):
        api.get_issue()


def test_explicit_revision_must_exist():
    api, _ = api_for(rowkit(), 12)
    assert api.resolve_revision(ROWKIT_SHA) == ROWKIT_SHA
    with pytest.raises(ValueError, match="40-character"):
        api.resolve_revision("main")
    import requests
    with pytest.raises(requests.HTTPError):
        api.resolve_revision("f" * 40)


@pytest.mark.parametrize("path, test, library", [
    ("src/pkg/mod.py", False, True),
    ("tests/test_mod.py", True, False),
    ("pkg/tests/helpers.py", True, False),
    ("pkg/mod_test.py", True, False),
    ("conftest.py", True, False),
    ("docs/conf.py", False, False),
    ("setup.py", False, False),
    ("pkg/testing.py", False, True),
])
def test_path_classifiers(path, test, library):
    assert is_test_path(path) is test
    assert is_library_source(path) is library
