"""Offline safety and regression checks for the optional local Docker validator."""
from pathlib import Path
import pytest

from issuepilot.sandbox import (
    checked_tests, check_patch, stage_source,
)


PATCH = """diff --git a/calculator.py b/calculator.py
--- a/calculator.py
+++ b/calculator.py
@@ -1 +1 @@
-return 1
+return 2
"""


def test_only_pytest_command_accepted():
    assert checked_tests(["pytest", "-q"]) == ["python", "-m", "pytest", "-q"]
    for command in (["bash", "-c", "echo secret"], ["pytest", "/etc/passwd"],
                    ["pytest", "../secret"], ["pytest", "--override-ini=x=y"]):
        with pytest.raises(ValueError):
            checked_tests(command)


def test_reject_markdown_patch_and_binary(tmp_path):
    path = tmp_path / "patch.txt"
    path.write_text("Draft Diff\n*** Begin Draft Diff\n")
    with pytest.raises(ValueError, match="unified"):
        check_patch(path)
    path.write_bytes(b"x\x00y")
    with pytest.raises(ValueError):
        check_patch(path)


def test_reject_unsafe_patch_target(tmp_path):
    path = tmp_path / "bad.patch"
    path.write_text(PATCH.replace("a/calculator.py b/calculator.py",
                                  "a/../secret.py b/../secret.py"))
    with pytest.raises(ValueError):
        check_patch(path)


def test_accept_reviewed_git_diff(tmp_path):
    path = tmp_path / "good.patch"
    path.write_text(PATCH)
    assert check_patch(path).startswith("diff --git")


def test_staging_excludes_secrets_and_symlinks(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "calculator.py").write_text("def f(): return 1\n")
    (source / ".env").write_text("TOKEN=topsecret\n")
    (source / "private.key").write_text("not allowed")
    (source / "link.py").symlink_to(source / "calculator.py")
    stats = stage_source(source, target)
    assert stats["files"] == 1
    assert (target / "calculator.py").is_file()
    assert not (target / ".env").exists()
    assert not (target / "private.key").exists()
    assert not (target / "link.py").exists()


def test_staging_reports_empty_source(tmp_path):
    source = tmp_path / "empty"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / ".env").write_text("secret")
    with pytest.raises(ValueError, match="no permitted files"):
        stage_source(source, target)
