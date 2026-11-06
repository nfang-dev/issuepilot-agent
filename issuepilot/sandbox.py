"""Explicitly approved local-only patch validation with Docker.

Only run against source directories you own/trust. Docker is not a hardened VM.
No target GitHub repository writes, credentials, host shell, or container network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


IMAGE = "issuepilot-sandbox:v1"
REPORT_MAX = 16000
SAFE_TEST_ARGS = re.compile(r"^[A-Za-z0-9_./:=+\-]+$")
IGNORED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
           ".tox", ".ruff_cache", "node_modules", ".idea", ".DS_Store"}
SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
                ".npmrc", ".pypirc", "credentials", "credentials.json"}
SAFE_SUFFIX = {".py", ".toml", ".ini", ".txt", ".md", ".cfg", ".json", ".yaml",
               ".yml", ".rst", ".typed", ".gitignore"}
MAX_FILES = 2000
MAX_BYTES = 12_000_000


def checked_tests(arguments: list[str]) -> list[str]:
    """Allow only pytest module and simple pytest paths/flags; no arbitrary shell."""
    if not arguments or arguments[0] != "pytest":
        raise ValueError("Only pytest commands are supported (use --test tests/...).")
    if len(arguments) > 12 or any(not SAFE_TEST_ARGS.fullmatch(arg) for arg in arguments):
        raise ValueError("Invalid test arguments")
    forbidden = {"-c", "--override-ini", "--pyargs", "--import-mode", "--rootdir"}
    for arg in arguments[1:]:
        if (arg in forbidden or arg.startswith(("--override-ini=", "-c=", "--pyargs",
                                                "--import-mode=", "--rootdir=", "--confcutdir="))
                or arg.startswith("/") or ".." in Path(arg).parts):
            raise ValueError(f"Unsupported pytest argument: {arg}")
    return ["python", "-m", *arguments]


def _should_copy(path: Path) -> bool:
    if path.name in IGNORED or path.name.lower() in SECRET_NAMES:
        return False
    if any(p.startswith(".") and p not in (".gitignore",) for p in path.parts):
        return False
    if path.suffix.lower() not in SAFE_SUFFIX and path.name not in {"LICENSE", "Makefile"}:
        return False
    return True


def stage_source(source: Path, destination: Path) -> dict:
    """Copy a bounded allowlist of regular files. Never follow source symlinks."""
    count = 0
    size = 0
    for current, dirs, files in os.walk(source, followlinks=False):
        relative = Path(current).relative_to(source)
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()
                   and d not in IGNORED and not d.startswith(".")]
        for name in files:
            src = Path(current) / name
            rel = relative / name
            if src.is_symlink() or not src.is_file() or not _should_copy(rel):
                continue
            stat = src.stat()
            count += 1
            size += stat.st_size
            if count > MAX_FILES or size > MAX_BYTES or stat.st_size > 100_000:
                raise ValueError("Source exceeds sandbox staging size limits")
            target = destination / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            target.chmod(0o444)
    if count == 0:
        raise ValueError("Source folder has no permitted files")
    return {"files": count, "bytes": size}


def check_patch(patch: Path) -> str:
    """Reject binary, unsafe paths, symlinks, and non-unified-diff proposals."""
    if not patch.is_file() or patch.is_symlink():
        raise ValueError("Patch must be a regular text file")
    raw = patch.read_bytes()
    if not raw or len(raw) > 100_000 or b"\0" in raw:
        raise ValueError("Patch is empty, binary, or too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Patch must be UTF-8") from exc
    if not text.startswith("diff --git a/") or "\n@@ " not in text:
        raise ValueError("Expected a real unified git diff, not a Markdown patch proposal")
    if any(token in text for token in ("GIT binary patch", "Binary files ", "new file mode 120000",
                                      "old mode 120000", "rename from ", "rename to ",
                                      "copy from ", "copy to ", "Submodule ")):
        raise ValueError("Patch contains unsupported operations")
    headers = re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$", text, re.MULTILINE)
    if not headers or len(headers) > 20:
        raise ValueError("Patch must contain 1-20 changed paths")
    for left, right in headers:
        path = Path(left)
        if (left != right or path.is_absolute() or ".." in path.parts or
                not _should_copy(path) or path.suffix not in {".py", ".toml", ".ini", ".cfg"}):
            raise ValueError(f"Unsafe or unsupported patch target: {left}")
    return text



def verify_patch_manifest(manifest: Path, patch: Path, staged: Path, source: Path) -> dict:
    """Verify patch and staged source hashes before starting the Docker container."""
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 32_000:
        raise ValueError("Manifest must be a small regular JSON file")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid patch manifest JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported patch manifest version")
    expected_commit = data.get("base_commit")
    if not isinstance(expected_commit, str) or not re.fullmatch(r"[a-f0-9]{40}", expected_commit):
        raise ValueError("Manifest is missing a valid immutable commit")
    patch_hash = hashlib.sha256(patch.read_bytes()).hexdigest()
    if patch_hash != data.get("patch_sha256"):
        raise ValueError("Patch SHA256 does not match the generated manifest")
    expected = data.get("source_sha256")
    if not isinstance(expected, dict) or not 1 <= len(expected) <= 4:
        raise ValueError("Manifest must contain 1-4 source hashes")
    for path, digest in expected.items():
        if (not isinstance(path, str) or not _should_copy(Path(path)) or
                Path(path).is_absolute() or ".." in Path(path).parts or
                not path.endswith(".py") or not isinstance(digest, str) or
                not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("Manifest has an unsafe file path or source hash")
        staged_file = staged / path
        if not staged_file.is_file():
            raise ValueError(f"Staged source is missing: {path}")
        actual = hashlib.sha256(staged_file.read_bytes()).hexdigest()
        if digest != actual:
            raise ValueError(f"Source revision mismatch: {path}; check out {expected_commit}")

    changed = data.get("modified_files")
    if (not isinstance(changed, list) or not changed or
            not set(changed).issubset(expected)):
        raise ValueError("Manifest changed-file set is invalid")
    patch_headers = re.findall(r"^diff --git a/([^\n]+) b/([^\n]+)$",
                               patch.read_text(encoding="utf-8"), re.MULTILINE)
    if {left for left, right in patch_headers} != set(changed):
        raise ValueError("Patch target paths differ from the source manifest")

    # Content hashes make this comparison useful even for non-Git checkouts.
    # For Git repositories, require the exact pinned checkout as well.
    if (source / ".git").exists():
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source,
                             capture_output=True, text=True, timeout=10, check=False)
        if rev.returncode != 0 or rev.stdout.strip() != expected_commit:
            raise ValueError(f"Local checkout HEAD is not the pinned commit {expected_commit}")
    return {"base_commit": expected_commit, "repository": data.get("repository"),
            "files_checked": len(expected), "patch_sha256": patch_hash}


def run_validation(
    source: Path,
    patch: Path,
    tests: list[str],
    timeout: int = 90,
    image: str = IMAGE,
    manifest: Path | None = None,
) -> dict:
    """Run a bounded, network-disabled Docker test using isolated temporary input."""
    source = source.resolve(strict=True)
    patch = patch.resolve(strict=True)
    if not source.is_dir():
        raise ValueError("Source must be a directory")
    if not 10 <= timeout <= 600:
        raise ValueError("Timeout must be between 10 and 600 seconds")
    cmd = checked_tests(tests)
    check_patch(patch)
    if not shutil.which("docker"):
        raise RuntimeError("Docker CLI was not found in PATH")
    with tempfile.TemporaryDirectory(prefix="issuepilot-sandbox-") as tmp:
        root = Path(tmp)
        staged = root / "source"
        staged.mkdir()
        stats = stage_source(source, staged)
        provenance = verify_patch_manifest(manifest, patch, staged, source) if manifest else None
        staged_patch = root / "change.patch"
        staged_patch.write_bytes(patch.read_bytes())
        docker_cmd = [
            "docker", "run", "--rm", "--network", "none", "--pull", "never",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "512m", "--cpus", "1",
            "--user", "65534:65534",
            "--tmpfs", "/work:rw,mode=1777,size=256m",
            "--tmpfs", "/tmp:rw,mode=1777,size=64m",
            "--mount", f"type=bind,src={staged},dst=/input,readonly",
            "--mount", f"type=bind,src={staged_patch},dst=/patches/change.patch,readonly",
            image, "python", "/opt/issuepilot/runner.py",
            "--test-argv-json", json.dumps(cmd), "--timeout", str(timeout),
        ]
        try:
            result = subprocess.run(docker_cmd, capture_output=True, text=True,
                                    timeout=2 * timeout + 90, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "docker_timeout", "source": stats,
                    "error": "Docker execution exceeded overall timeout"}
        if result.returncode != 0:
            return {"status": "docker_error", "source": stats,
                    "error": (result.stderr or result.stdout)[-2000:]}
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return {"status": "docker_error", "source": stats, "error": "No runner output"}
        try:
            report = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"status": "docker_error", "source": stats,
                    "error": "Runner did not return valid JSON"}
        report["source"] = stats
        if provenance is not None:
            report["provenance"] = provenance
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a reviewed unified diff inside local Docker")
    parser.add_argument("--repo-dir", required=True, type=Path,
                        help="Existing locally checked-out source directory")
    parser.add_argument("--patch", required=True, type=Path,
                        help="Real unified git diff file, NOT patch-proposal.md")
    parser.add_argument("--manifest", type=Path, help="Optional generated provenance manifest for source SHA verification")
    parser.add_argument("--approve", action="store_true",
                        help="Explicit consent to execute reviewed source code in Docker")
    parser.add_argument("--test", nargs="+", default=["pytest", "-q"],
                        help="pytest command and arguments, default: pytest -q")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path, default=Path("validation-report.json"))
    args = parser.parse_args()
    if not args.approve:
        parser.error("Explicit --approve is required before any container execution")
    try:
        report = run_validation(args.repo_dir, args.patch, args.test, args.timeout,
                                manifest=args.manifest)
    except (ValueError, RuntimeError, OSError) as exc:
        report = {"status": "input_error", "error": str(exc)[:500]}
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Validation status: {report['status']}")
    print(f"Report: {args.output}")
    if report["status"] != "patched_tests_passed":
        sys.exit(1)


if __name__ == "__main__":
    main()
