"""Offline tests for V4 exact-source diff construction and sandbox provenance."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from issuepilot.patch_generator import generate, render_patch, sha256_text
from issuepilot.sandbox import stage_source, verify_patch_manifest


ORIGINAL = "def loads(value, **kwargs):\n    return value\n"
OLD = "    return value\n"
NEW = "    if kwargs:\n        raise TypeError('unexpected keyword')\n    return value\n"
COMMIT = "a" * 40


def edit(path="src/serializer.py", old=OLD, new=NEW):
    return {"path": path, "old": old, "new": new, "reason": "Fail fast on unsupported options"}


def plan(edits=None):
    return {
        "summary": "Reject unexpected kwargs",
        "risks": "Existing callers may rely on ignored options.",
        "regression_tests": ["Untimed loads with max_age raises TypeError"],
        "edits": [edit()] if edits is None else edits,
    }


def test_render_real_git_unified_diff():
    patch, files = render_patch({"src/serializer.py": ORIGINAL}, [edit()])
    assert patch.startswith("diff --git a/src/serializer.py b/src/serializer.py\n")
    assert "--- a/src/serializer.py" in patch
    assert "+++ b/src/serializer.py" in patch
    assert "@@ " in patch
    assert "+    if kwargs:" in patch
    assert files == ["src/serializer.py"]


def test_wrong_exact_context_rejected():
    with pytest.raises(ValueError, match="Source mismatch"):
        render_patch({"src/serializer.py": ORIGINAL},
                     [edit(old="    return self.load_payload(signer.unsign(s))\n")])


def test_ambiguous_snippet_rejected():
    with pytest.raises(ValueError, match="once"):
        render_patch({"src/serializer.py": "pass\npass\n"},
                     [edit(old="pass\n", new="return None\n")])


def test_outside_selected_files_rejected():
    with pytest.raises(ValueError, match="approved"):
        render_patch({"src/serializer.py": ORIGINAL},
                     [edit(path="../secrets.py")])


def test_syntax_error_rejected():
    with pytest.raises(ValueError, match="syntax"):
        render_patch({"src/serializer.py": ORIGINAL},
                     [edit(new="    if kwargs\n        return value\n")])


def test_no_edits_is_not_a_fake_success():
    with pytest.raises(ValueError, match="1-6"):
        render_patch({"src/serializer.py": ORIGINAL}, [])


def test_duplicate_sequential_edits_are_rejected():
    with pytest.raises(ValueError, match="Source mismatch"):
        render_patch({"src/serializer.py": ORIGINAL}, [edit(), edit()])


class FakeGitHub:
    ref = NS(owner="demo", repo="app", number=42)

    def __init__(self):
        self.read_paths = []

    def get_repository_revision(self):
        return COMMIT

    def read_source_at_revision(self, path, revision):
        self.read_paths.append((path, revision))
        return ORIGINAL

    def get_issue(self):
        return {"url": "https://github.com/demo/app/issues/42",
                "title": "Dangerous ignored option", "body": "max_age ignored"}


class FakeResponses:
    def __init__(self, data):
        self.data = data
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return NS(output_text=json.dumps(self.data))


def test_generate_pinned_evidence_and_manifest():
    api = FakeGitHub()
    response = FakeResponses(plan())
    patch, manifest = generate(api, NS(responses=response), ["src/serializer.py"],
                               investigation="Prior analysis may be inaccurate.")
    assert api.read_paths == [("src/serializer.py", COMMIT)]
    assert manifest["base_commit"] == COMMIT
    assert manifest["source_sha256"]["src/serializer.py"] == sha256_text(ORIGINAL)
    assert manifest["patch_sha256"] == sha256_text(patch)
    assert manifest["status"] == "draft_unverified"
    assert "full" not in manifest
    assert "text" in response.requests[0]  # Structured output requested
    assert ORIGINAL in response.requests[0]["input"][0]["content"]
    assert patch.count("diff --git") == 1


def test_generate_wrong_model_snippet_fails_closed():
    response = FakeResponses(plan([edit(old="def loads(unseen_arg):\n")]))
    with pytest.raises(ValueError, match="Source mismatch"):
        generate(FakeGitHub(), NS(responses=response), ["src/serializer.py"])


def test_manifest_rejects_tampering_or_wrong_checkout(tmp_path):
    source = tmp_path / "source"
    staged = tmp_path / "staged"
    source.mkdir()
    staged.mkdir()
    src = source / "src"
    src.mkdir()
    (src / "serializer.py").write_text(ORIGINAL)
    stage_source(source, staged)
    patch, _ = render_patch({"src/serializer.py": ORIGINAL}, [edit()])
    patch_path = tmp_path / "fix.patch"
    patch_path.write_text(patch)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "base_commit": COMMIT,
        "repository": "demo/app",
        "patch_sha256": sha256_text(patch),
        "source_sha256": {"src/serializer.py": sha256_text(ORIGINAL)},
        "modified_files": ["src/serializer.py"],
    }))
    valid = verify_patch_manifest(manifest, patch_path, staged, source)
    assert valid["base_commit"] == COMMIT
    patch_path.write_text(patch + "\n")
    with pytest.raises(ValueError, match="Patch SHA256"):
        verify_patch_manifest(manifest, patch_path, staged, source)
    patch_path.write_text(patch)
    (staged / "src" / "serializer.py").chmod(0o600)
    (staged / "src" / "serializer.py").write_text("corrupt")
    with pytest.raises(ValueError, match="revision mismatch"):
        verify_patch_manifest(manifest, patch_path, staged, source)
