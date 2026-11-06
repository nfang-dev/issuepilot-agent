# Local Docker Sandbox (V3 MVP)

This component validates an **already reviewed real unified diff** against an explicitly selected, trusted local Python repository. It does not generate the patch, execute it on your host, alter the target checkout, or write to GitHub. `patch-proposal.md` from V2 is explanatory Markdown, **not** a valid patch.

## Build the image (network access during image build only)

From the IssuePilot repository root, with Docker Desktop running:

```bash
docker build -f sandbox/Dockerfile -t issuepilot-sandbox:v1 .
```

The image includes Python 3.12, Git, pytest, and freezegun. It does **not** install arbitrary dependencies of a target repository. Unsupported dependencies will cause test failures; that is not a patch failure.

## Try the fixture first

```bash
python -m issuepilot.sandbox \
  --repo-dir examples/sandbox_fixture \
  --patch examples/sandbox_fixture.patch \
  --approve \
  --output validation-report.json
```

Expected: `patched_tests_passed`, with a failing baseline and passing patched tests. This tiny toy fixture only validates the sandbox mechanism, not a real GitHub bug fix.

## Validate a manually reviewed patch on a trusted checkout

1. Obtain a trusted local copy of a Python project at a known commit; record the SHA.
2. Review the patch, including code it adds. Ensure it is a true unified Git diff, not a Markdown description.
3. Provide the checkout and patch to `--repo-dir` and `--patch`. Only pytest is supported.
4. Read `validation-report.json`: `patch_check`, `baseline`, `patch_apply`, and `patched` are reported separately.

```bash
python -m issuepilot.sandbox \
  --repo-dir /absolute/path/to/trusted/python/repo \
  --patch /absolute/path/to/reviewed-change.patch \
  --approve
```

**Safety and limits:** The host launcher rejects non-diff input and stages only bounded, allowlisted, non-symlink source files. At runtime mounts are read-only. The container uses a non-root user, disabled network, dropped capabilities, resource limits, and temporary scratch storage. Docker is **not** a strong security boundary for malicious code. Review code first; for genuinely adversarial workloads, prefer a disposable VM. The Docker image build itself uses standard network and privileged image-building operations. Do not put credentials in test source code.

**Out of scope:** arbitrary build commands, installation of target-specific dependencies, fully automated patch generation, PRs, comprehensive repository checkout, and production-grade security. The launcher does not write to any GitHub repository.
