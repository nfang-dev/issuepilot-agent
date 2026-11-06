"""Deterministic offline fixtures for the automatic pipeline.

* ``FakeGitHub``: a fake GitHub REST API (requests.Session stand-in) serving
  several small synthetic projects with commit-addressable trees.
* ``ScriptedModel``: replays a fixed script of OpenAI Responses API replies,
  asserting which pipeline stage each request belongs to.

The projects and bugs below were written for these tests. They are NOT real
GitHub issues, and passing tests say nothing about how a live model behaves.
"""
from __future__ import annotations

import base64
import copy
import itertools
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS
from urllib.parse import unquote

import requests

from issuepilot.github_api import GitHubAPI, IssueRef

# ---------------------------------------------------------------- projects

ROWKIT_PARSER = '''"""Minimal delimited-row parser."""


def split_row(line, delimiter=","):
    """Split one line into fields, honouring double-quoted fields."""
    fields = []
    current = []
    in_quotes = False
    for char in line:
        if char == '"':
            in_quotes = not in_quotes
        elif char == delimiter:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields
'''

ROWKIT_TEST_PARSER = '''from rowkit.parser import split_row


def test_plain_fields():
    assert split_row("a,b,c") == ["a", "b", "c"]
'''

ROWKIT_FILES = {
    "pyproject.toml": '[project]\nname = "rowkit"\n',
    "README.md": "# rowkit\n",
    "docs/usage.md": "Use split_row to parse a line.\n",
    "docs/conf.py": 'project = "rowkit"\n',
    "src/rowkit/__init__.py": 'from rowkit.parser import split_row\nfrom rowkit.writer import join_row\n\n'
                              '__all__ = ["split_row", "join_row"]\n',
    "src/rowkit/parser.py": ROWKIT_PARSER,
    "src/rowkit/writer.py": '''"""Row serialisation helpers."""
from rowkit.parser import split_row


def join_row(fields, delimiter=","):
    """Join fields, quoting any that contain the delimiter."""
    out = []
    for value in fields:
        if delimiter in value:
            value = '"' + value + '"'
        out.append(value)
    return delimiter.join(out)


def roundtrip(line, delimiter=","):
    return join_row(split_row(line, delimiter), delimiter)
''',
    "tests/test_parser.py": ROWKIT_TEST_PARSER,
    "tests/test_writer.py": '''from rowkit.writer import join_row


def test_join_quotes_delimiter():
    assert join_row(["a", "b,c"]) == 'a,"b,c"'
''',
    ".env": "SECRET=do-not-read\n",
}

RETRYKIT_BACKOFF = '''"""Backoff schedules."""


def exponential_delays(base, factor, retries, cap=None):
    """Return the delay to wait before each of ``retries`` retry attempts."""
    delays = []
    delay = base
    for _ in range(retries - 1):
        delays.append(delay if cap is None else min(delay, cap))
        delay *= factor
    return delays
'''

RETRYKIT_TEST_BACKOFF = '''from retrykit.backoff import exponential_delays


def test_cap_applies():
    assert max(exponential_delays(1, 10, 3, cap=5)) == 5
'''

RETRYKIT_FILES = {
    "setup.py": "from setuptools import setup\nsetup(name='retrykit')\n",
    "retrykit/__init__.py": "from retrykit.backoff import exponential_delays\n",
    "retrykit/backoff.py": RETRYKIT_BACKOFF,
    "retrykit/policy.py": '''"""Retry policy built on backoff schedules."""
from retrykit.backoff import exponential_delays


class RetryPolicy:
    def __init__(self, retries=3, base=0.5, factor=2.0, cap=None):
        self.retries = retries
        self.base = base
        self.factor = factor
        self.cap = cap

    def schedule(self):
        return exponential_delays(self.base, self.factor, self.retries, self.cap)
''',
    "examples/demo.py": "from retrykit import exponential_delays\nprint(exponential_delays(1, 2, 3))\n",
    "tests/unit/test_backoff.py": RETRYKIT_TEST_BACKOFF,
    "tests/unit/test_policy.py": '''from retrykit.policy import RetryPolicy


def test_policy_uses_base():
    assert RetryPolicy(retries=2, base=1).schedule()[0] == 1
''',
}

FASTVEC_FILES = {
    "fastvec/__init__.py": 'from fastvec._native import dot\n\n__all__ = ["dot"]\n',
    "fastvec/fallback.py": "def dot(a, b):\n    return sum(x * y for x, y in zip(a, b))\n",
    "src/dot.cpp": "// SIMD dot product compiled into fastvec._native\nlong dot(const int* a, const int* b, int n);\n",
    "tests/test_dot.py": "from fastvec import dot\n\n\ndef test_dot():\n    assert dot([1, 2], [3, 4]) == 11\n",
}

PIXELUI_FILES = {
    "package.json": '{"name": "pixelui"}\n',
    "src/button.js": "export const hover = '#fff';\n",
    "README.md": "# pixelui\n",
}

GEOMCORE_FILES = {
    "src/geom.cpp": "double area(double w, double h) { return w * h; }\n",
    "python/geomcore/__init__.py": "from geomcore.shapes import Rect\n",
    "python/geomcore/shapes.py": "class Rect:\n    def __init__(self, w, h):\n        self.w, self.h = w, h\n",
    "tests/test_shapes.py": "def test_rect():\n    pass\n",
}

LONG_BODY = " Steps to reproduce are in the snippet above; this is reproducible every time."


def issue(number, title, body, labels=("bug",), state="open", comments=(), pull_request=False):
    return {"number": number, "title": title, "body": body + LONG_BODY, "labels": list(labels),
            "state": state, "comments": list(comments), "pull_request": pull_request}


ROWKIT_ISSUES = {
    12: issue(12, "split_row splits on delimiters inside quoted fields",
              "Calling `split_row('a,\"b,c\",d')` returns `['a', 'b', 'c', 'd']`.\n"
              "Expected `['a', 'b,c', 'd']` because the second field is quoted.",
              comments=["Same here with semicolons as delimiter."]),
    13: issue(13, "Support tab-separated input", "It would be great to have a TSV mode for split_row.",
              labels=("enhancement",)),
    14: issue(14, "Typo in usage guide", "docs/usage.md says 'parse a line'; it should say 'parse one line'.",
              labels=()),
    15: issue(15, "join_row does not escape quotes", "join_row(['a\"b']) produces invalid output.",
              state="closed"),
    16: issue(16, "Rows look wrong sometimes", "Sometimes rows look wrong with my data, not sure why."),
    17: issue(17, "Fix quoting", "PR body", pull_request=True),
    18: issue(18, "Quoted fields are parsed incorrectly", "split_row breaks quoted values.",
              labels=("bug", "documentation")),
}

ROWKIT_SHA = "1a" * 20
ROWKIT_SHA_V2 = "1b" * 20
RETRYKIT_SHA = "2a" * 20
FASTVEC_SHA = "3a" * 20
PIXELUI_SHA = "4a" * 20
GEOMCORE_SHA = "5a" * 20


@dataclass
class SyntheticRepo:
    owner: str
    name: str
    language: str | None
    files: dict
    issues: dict
    head: str
    default_branch: str = "main"
    archived: bool = False
    history: dict = field(default_factory=dict)

    def __post_init__(self):
        self.history = {self.head: dict(self.files)}

    def advance(self, new_sha: str, changes: dict) -> None:
        """Simulate a new commit landing on the default branch."""
        files = dict(self.history[self.head])
        files.update(changes)
        self.history[new_sha] = files
        self.head = new_sha

    def resolve(self, ref: str) -> str | None:
        if ref == self.default_branch:
            return self.head
        return ref if ref in self.history else None


def rowkit():
    return SyntheticRepo("acme", "rowkit", "Python", ROWKIT_FILES, ROWKIT_ISSUES, ROWKIT_SHA)


def retrykit():
    return SyntheticRepo("acme", "retrykit", "Python", RETRYKIT_FILES, {3: issue(
        3, "exponential_delays returns one delay too few",
        "`exponential_delays(1, 2, 3)` returns `[1, 2]` but I asked for 3 retries, so I expected "
        "`[1, 2, 4]`. `RetryPolicy(retries=3).schedule()` has the same problem.",
        labels=("type: bug",))}, RETRYKIT_SHA)


def fastvec():
    return SyntheticRepo("acme", "fastvec", "Python", FASTVEC_FILES, {5: issue(
        5, "dot() overflows for large int vectors",
        "`dot([2**40], [2**40])` returns a negative number instead of 2**80.")}, FASTVEC_SHA)


def pixelui():
    return SyntheticRepo("acme", "pixelui", "JavaScript", PIXELUI_FILES, {1: issue(
        1, "Primary button has wrong hover color", "Hover color should be #eee but is #fff.")},
        PIXELUI_SHA)


def geomcore():
    return SyntheticRepo("acme", "geomcore", "C++", GEOMCORE_FILES, {2: issue(
        2, "Rect area wrong for negative sizes", "Rect(-1, 2) area returns -2, expected an error.")},
        GEOMCORE_SHA)


# ------------------------------------------------------------- fake GitHub

class FakeHTTPResponse:
    def __init__(self, status: int, payload):
        self.status_code = status
        self.payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error from fake GitHub")

    def json(self):
        return copy.deepcopy(self.payload)


class FakeGitHub:
    """Serves /repos/{owner}/{repo}/... for SyntheticRepo objects; records every request."""

    def __init__(self, *repos: SyntheticRepo):
        self.repos = {(r.owner, r.name): r for r in repos}
        self.headers: dict = {}
        self.requests: list[tuple[str, dict]] = []
        self.fail_paths: set[str] = set()

    def get(self, url, params=None, timeout=None):
        assert url.startswith("https://api.github.com/"), url
        path = url[len("https://api.github.com"):]
        params = dict(params or {})
        self.requests.append((path, params))
        match = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
        repo = self.repos.get((match[1], match[2])) if match else None
        if repo is None:
            return FakeHTTPResponse(404, {"message": "Not Found"})
        rest = match[3] or ""
        if rest == "":
            return FakeHTTPResponse(200, {"default_branch": repo.default_branch,
                                          "language": repo.language, "archived": repo.archived})
        if rest.startswith("/commits/"):
            sha = repo.resolve(unquote(rest[len("/commits/"):]))
            return FakeHTTPResponse(200, {"sha": sha}) if sha else FakeHTTPResponse(404, {})
        if rest.startswith("/git/trees/"):
            sha = repo.resolve(unquote(rest[len("/git/trees/"):]))
            if not sha:
                return FakeHTTPResponse(404, {})
            files = repo.history[sha]
            return FakeHTTPResponse(200, {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t.encode())} for p, t in sorted(files.items())]})
        if rest.startswith("/contents/"):
            file_path = unquote(rest[len("/contents/"):])
            if file_path in self.fail_paths:
                return FakeHTTPResponse(500, {"message": "Server Error"})
            sha = repo.resolve(params.get("ref", repo.default_branch))
            text = repo.history.get(sha, {}).get(file_path)
            if text is None:
                return FakeHTTPResponse(404, {})
            return FakeHTTPResponse(200, {
                "type": "file", "encoding": "base64", "size": len(text.encode()),
                "content": base64.b64encode(text.encode()).decode(),
                "html_url": f"https://github.com/{repo.owner}/{repo.name}/blob/{sha}/{file_path}"})
        found = re.fullmatch(r"/issues/(\d+)(/comments)?", rest)
        if found and int(found[1]) in repo.issues:
            item = repo.issues[int(found[1])]
            url_base = f"https://github.com/{repo.owner}/{repo.name}/issues/{item['number']}"
            if found[2]:
                return FakeHTTPResponse(200, [{"body": c, "html_url": f"{url_base}#issuecomment-{i}"}
                                              for i, c in enumerate(item["comments"], 1)])
            payload = {"number": item["number"], "title": item["title"], "state": item["state"],
                       "body": item["body"], "html_url": url_base, "created_at": "2026-01-01T00:00:00Z",
                       "labels": [{"name": n} for n in item["labels"]]}
            if item["pull_request"]:
                payload["pull_request"] = {"url": url_base}
            return FakeHTTPResponse(200, payload)
        return FakeHTTPResponse(404, {})

    def content_requests(self) -> list[tuple[str, dict]]:
        return [(p, q) for p, q in self.requests if "/contents/" in p]


def api_for(repo: SyntheticRepo, number: int, fake: FakeGitHub | None = None) -> tuple[GitHubAPI, FakeGitHub]:
    fake = fake or FakeGitHub(repo)
    return GitHubAPI(IssueRef(repo.owner, repo.name, number), session=fake), fake


# ---------------------------------------------------------- scripted model

_call_ids = itertools.count(1)


def stage_of(request: dict) -> str:
    name = ((request.get("text") or {}).get("format") or {}).get("name")
    if name == "issue_eligibility":
        return "eligibility"
    if name == "source_grounded_patch_plan":
        return "patch"
    if request.get("tools"):
        return "discovery"
    return "other"


class ScriptedModel:
    """Stand-in for an OpenAI client: ``client.responses.create`` replays ``steps``.

    Each step is (stage, reply) where reply is a response object or a callable
    taking the request kwargs. Calling the model more often than scripted, or
    in an unexpected stage, fails the test.
    """

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.responses = self

    def create(self, **kwargs):
        # The caller keeps appending to its input list; snapshot what was sent.
        kwargs = dict(kwargs, input=list(kwargs.get("input", [])))
        self.requests.append(kwargs)
        if not self.steps:
            raise AssertionError(f"Unscripted {stage_of(kwargs)} model request")
        stage, reply = self.steps.pop(0)
        assert stage_of(kwargs) == stage, f"expected a {stage} request, got {stage_of(kwargs)}"
        return reply(kwargs) if callable(reply) else reply

    def stages(self) -> list[str]:
        return [stage_of(r) for r in self.requests]


def tool_call(name: str, **arguments) -> NS:
    return NS(output=[NS(type="function_call", name=name, arguments=json.dumps(arguments),
                         call_id=f"call_{next(_call_ids)}")],
              output_text="", usage=NS(input_tokens=50, output_tokens=10))


def raw_tool_call(name: str, raw_arguments: str) -> NS:
    return NS(output=[NS(type="function_call", name=name, arguments=raw_arguments,
                         call_id=f"call_{next(_call_ids)}")],
              output_text="", usage=NS(input_tokens=50, output_tokens=10))


def text_reply(text: str) -> NS:
    return NS(output=[NS(type="message")], output_text=text, usage=NS(input_tokens=80, output_tokens=40))


def json_reply(data: dict) -> NS:
    return text_reply(json.dumps(data))


def submit(decision="selected", source_files=(), test_files=(), confidence="high",
           rationale="Evidence from read_file shows the faulty branch.", evidence=()) -> NS:
    return tool_call("submit_file_selection", decision=decision, source_files=list(source_files),
                     test_files=list(test_files), confidence=confidence, rationale=rationale,
                     evidence=list(evidence))


def classification(category="logic_bug", **overrides) -> dict:
    data = {"category": category, "affects_python_code": True, "has_reproduction": True,
            "describes_expected_vs_actual": True, "requires_design_decision": False,
            "confidence": "high", "rationale": "Wrong return value with a concrete reproduction.",
            "mentioned_symbols": []}
    data.update(overrides)
    return data


def patch_plan(*edits, summary="Fix the faulty branch and add a regression test.") -> dict:
    return {"summary": summary, "risks": "Synthetic test fixture.",
            "regression_tests": ["regression test for the reported input"],
            "edits": [dict(path=p, old=o, new=n, reason="fix") for p, o, n in edits]}


# ------------------------------------------------- local checkout helpers

def materialize(files: dict, root: Path) -> None:
    for path, text in files.items():
        if path.startswith("."):
            continue
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def git_apply(root: Path, patch: str) -> subprocess.CompletedProcess:
    (root.parent / "change.patch").write_text(patch, encoding="utf-8")
    return subprocess.run(["git", "apply", str(root.parent / "change.patch")], cwd=root,
                          capture_output=True, text=True, timeout=30, check=False)


def run_pytest(root: Path) -> subprocess.CompletedProcess:
    """Run the synthetic project's own tests in a child interpreter (trusted fixture code only)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root / "src"), str(root),
                                                     env.get("PYTHONPATH", "")]))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False)
