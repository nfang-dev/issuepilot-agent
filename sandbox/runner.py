"""Executed *inside* the disposable Docker sandbox. Never invoked on the host.

The source and patch are mounted read-only; all writes occur on /work tmpfs.
No network access, privileged permissions, or Docker socket are available at runtime.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def command_result(argv: list[str], cwd: Path, seconds: int) -> dict:
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True,
            errors="replace", timeout=seconds, check=False,
            env={**os.environ, "PYTHONPATH": f"{cwd / 'src'}:{cwd}"},
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "output": output[-6000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "passed": False,
            "exit_code": None,
            "output": f"Command exceeded {seconds}s. Partial output: {str(exc.stdout or '')[-1500:]}",
            "timed_out": True,
        }


def run(source: Path, patch: Path, test_argv: list[str], seconds: int) -> dict:
    """Copy source into container-only scratch, check/apply patch, and test."""
    result = {
        "status": "internal_error",
        "patch_check": None,
        "baseline": None,
        "patch_apply": None,
        "patched": None,
        "test_command": test_argv,
        "warning": "Tests run only inside a Docker container; success is not a security guarantee.",
    }
    work = Path("/work/repo")
    if not source.is_dir() or not patch.is_file():
        result["error"] = "Sandbox input mounts are missing"
        return result

    shutil.copytree(source, work, symlinks=False)
    # Staged host files were made read-only; only the container's private copy
    # should be writable so git apply can update tracked files in /work.
    for child in work.rglob("*"):
        if child.is_file():
            child.chmod(0o600)
    check = command_result(["git", "apply", "--check", str(patch)], work, 20)
    result["patch_check"] = check
    if not check["passed"]:
        result["status"] = "patch_rejected"
        return result

    result["baseline"] = command_result(test_argv, work, seconds)
    apply = command_result(["git", "apply", str(patch)], work, 20)
    result["patch_apply"] = apply
    if not apply["passed"]:
        result["status"] = "patch_apply_failed"
        return result

    result["patched"] = command_result(test_argv, work, seconds)
    if result["patched"]["passed"]:
        result["status"] = "patched_tests_passed"
    else:
        result["status"] = "patched_tests_failed"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Docker-only patch validator")
    parser.add_argument("--test-argv-json", required=True)
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    try:
        argv = json.loads(args.test_argv_json)
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(s, str) or not s or len(s) > 300 for s in argv) or
                args.timeout < 1 or args.timeout > 600):
            raise ValueError("Invalid sandbox test argv or timeout")
        result = run(Path("/input"), Path("/patches/change.patch"), argv, args.timeout)
    except Exception as exc:
        result = {"status": "internal_error", "error": str(exc)[:500]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
