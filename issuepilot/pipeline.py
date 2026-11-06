"""URL-only draft-fix pipeline: eligibility -> file discovery -> patch generation.

Every stage reads the repository at ONE commit resolved at the start. Each
stage can stop the run with an explicit status; nothing is applied, executed
or published. The Docker sandbox and its --approve boundary remain a separate,
manual step.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import requests

from issuepilot.discovery import SELECTED, discover_files
from issuepilot.eligibility import ELIGIBLE, NEEDS_REVIEW as ELIGIBILITY_REVIEW, check_eligibility, is_test_path
from issuepilot.patch_generator import generate, sha256_text

# Run statuses and CLI exit codes.
DRAFT_READY = "draft_ready"
STOPPED_ELIGIBLE = "eligible"
STOPPED_SELECTED = "files_selected"
UNSUPPORTED = "unsupported"
NEEDS_REVIEW = "needs_review"
PATCH_FAILED = "patch_failed"
ERROR = "error"
EXIT_CODES = {DRAFT_READY: 0, STOPPED_ELIGIBLE: 0, STOPPED_SELECTED: 0, ERROR: 1,
              UNSUPPORTED: 3, NEEDS_REVIEW: 4, PATCH_FAILED: 5}
STAGES = ("eligibility", "discovery", "patch_generation")


class UsageMeter:
    """Wraps a Responses client to record per-stage request counts and tokens."""

    def __init__(self, client: Any):
        self._client = client
        self.stage = "setup"
        self.usage: dict[str, dict[str, int]] = {}
        self.responses = self

    def create(self, **kwargs: Any) -> Any:
        response = self._client.responses.create(**kwargs)
        bucket = self.usage.setdefault(self.stage, {"requests": 0, "input_tokens": 0, "output_tokens": 0})
        bucket["requests"] += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            bucket["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            bucket["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
        return response


@dataclass
class RunResult:
    status: str
    stage: str
    issue_url: str
    repository: str
    revision: str | None = None
    detail: str = ""
    eligibility: dict | None = None
    discovery: dict | None = None
    patch: str | None = None
    manifest: dict | None = None
    usage: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def record(self) -> dict:
        """Audit record for disk: decisions and tool arguments, never fetched source text."""
        data = asdict(self)
        data.pop("patch")
        data["patch_sha256"] = sha256_text(self.patch) if self.patch else None
        data["warning"] = ("Automated decisions and any draft patch are unverified. "
                           "Review before running the Docker sandbox with --approve.")
        return data


def run_auto(
    api: Any,
    client: Any,
    model: str = "gpt-5-mini",
    revision: str | None = None,
    investigation: str = "",
    stop_after: str | None = None,
    max_tool_calls: int = 14,
    max_rounds: int = 10,
    accept_needs_review: bool = False,
) -> RunResult:
    if stop_after not in (None, "eligibility", "discovery"):
        raise ValueError("stop_after must be eligibility, discovery or None")
    meter = UsageMeter(client)
    ref = api.ref
    run = RunResult(status=ERROR, stage="setup",
                    issue_url=f"https://github.com/{ref.owner}/{ref.repo}/issues/{ref.number}",
                    repository=f"{ref.owner}/{ref.repo}")
    run.usage = meter.usage
    try:
        run.stage = "eligibility"
        meter.stage = "eligibility"
        try:
            issue = api.get_issue()
        except ValueError as exc:  # The URL resolves to a pull request.
            run.status, run.detail = UNSUPPORTED, str(exc)
            return run
        pinned = api.resolve_revision(revision)
        run.revision = pinned
        tree = api.list_tree_at_revision(pinned)
        comments = api.get_issue_comments()
        decision = check_eligibility(issue, comments, api.get_repository_metadata(), tree, meter,
                                     model=model, explicit_revision=revision is not None)
        run.eligibility = decision.to_dict()
        # A human may continue past needs_review (never past unsupported); recorded for audit.
        overridden = accept_needs_review and decision.status == ELIGIBILITY_REVIEW
        run.eligibility["human_override"] = overridden
        if decision.status != ELIGIBLE and not overridden:
            run.status = decision.status
            run.detail = "; ".join(r["detail"] for r in decision.reasons)
            return run
        if stop_after == "eligibility":
            run.status = STOPPED_ELIGIBLE
            return run

        run.stage = "discovery"
        meter.stage = "discovery"
        found = discover_files(api, meter, issue, comments, pinned, model=model,
                               hints=decision.mentioned_symbols,
                               max_tool_calls=max_tool_calls, max_rounds=max_rounds)
        run.discovery = found.to_dict()
        if found.status != SELECTED:
            run.status = found.status
            run.detail = found.rationale or found.reason
            return run
        if stop_after == "discovery":
            run.status = STOPPED_SELECTED
            return run

        run.stage = "patch_generation"
        meter.stage = "patch_generation"
        context = "\n\n".join(part for part in (
            investigation.strip(),
            "Automated file discovery rationale (may contain mistakes):\n" + found.rationale,
        ) if part)
        try:
            patch, manifest = generate(api, meter, found.files, investigation=context,
                                       model=model, revision=pinned)
        except ValueError as exc:
            run.status, run.detail = PATCH_FAILED, str(exc)[:500]
            return run
        if all(is_test_path(path) for path in manifest["modified_files"]):
            run.status = PATCH_FAILED
            run.detail = "Draft only edits test files; no source fix was proposed"
            return run
        manifest["selection"] = {
            "mode": "automatic",
            "source_files": found.source_files,
            "test_files": found.test_files,
            "eligibility_category": decision.category,
            "discovery_confidence": found.confidence,
        }
        run.patch, run.manifest = patch, manifest
        run.status = DRAFT_READY
        return run
    except (requests.RequestException, ValueError, KeyError) as exc:
        run.status, run.detail = ERROR, f"{type(exc).__name__}: {str(exc)[:400]}"
        return run
