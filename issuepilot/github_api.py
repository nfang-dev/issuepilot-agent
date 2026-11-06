"""Read-only GitHub API adapter. No code execution and no write endpoints."""
from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from urllib.parse import quote

import requests

ISSUE_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)(?:/)?(?:[?#].*)?$")
ALLOWED_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".md", ".json", ".toml", ".yaml", ".yml", ".rs", ".cpp", ".h"}


@dataclass(frozen=True)
class IssueRef:
    owner: str
    repo: str
    number: int


def parse_issue_url(value: str) -> IssueRef:
    match = ISSUE_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("Expected https://github.com/OWNER/REPO/issues/NUMBER")
    return IssueRef(match.group(1), match.group(2), int(match.group(3)))


def allowed_path(path: str) -> bool:
    # Refuse internal files, path traversal, URLs, dotfiles, and binary/secret-prone files.
    if not path or path.startswith(("/", "~")) or "\\" in path or "//" in path:
        return False
    segments = path.split("/")
    if any(s in ("", ".", "..") or s.startswith(".") for s in segments):
        return False
    if len(path) > 280 or len(segments) > 18:
        return False
    last = segments[-1].lower()
    if last in {"dockerfile", "makefile", "license"}:
        return True
    return any(last.endswith(ext) for ext in ALLOWED_SUFFIXES)


class GitHubAPI:
    def __init__(self, ref: IssueRef, session: requests.Session | None = None):
        self.ref = ref
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json", "User-Agent": "IssuePilot/0.1"})
        token = os.getenv("GITHUB_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self._branch: str | None = None
        self._paths: list[str] | None = None

    def _get(self, endpoint: str, **params):
        # _get is only used with hardcoded GitHub API endpoints below.
        res = self.session.get(f"https://api.github.com{endpoint}", params=params or None, timeout=15)
        res.raise_for_status()
        return res.json()

    @property
    def prefix(self) -> str:
        return f"/repos/{quote(self.ref.owner)}/{quote(self.ref.repo)}"

    def get_issue(self) -> dict:
        raw = self._get(f"{self.prefix}/issues/{self.ref.number}")
        if "pull_request" in raw:
            raise ValueError("This project analyzes Issues, not PRs.")
        return {
            "number": raw["number"], "title": raw["title"], "state": raw["state"],
            "body": (raw.get("body") or "")[:9000], "url": raw["html_url"],
            "labels": [x["name"] for x in raw.get("labels", [])],
            "created_at": raw.get("created_at"),
        }

    def get_issue_comments(self) -> list[dict]:
        raw = self._get(f"{self.prefix}/issues/{self.ref.number}/comments", per_page=10)
        return [{"body": (c.get("body") or "")[:1400], "url": c["html_url"]} for c in raw[:10]]

    def list_repository_files(self, path_prefix: str) -> dict:
        if len(path_prefix) > 120 or ".." in path_prefix or path_prefix.startswith(("/", ".")):
            return {"error": "invalid prefix"}
        if self._paths is None:
            raw = self._get(self.prefix)
            self._branch = raw["default_branch"]
            tree = self._get(f"{self.prefix}/git/trees/{quote(self._branch, safe='')}", recursive=1)
            self._paths = sorted(x["path"] for x in tree.get("tree", []) if x.get("type") == "blob" and allowed_path(x["path"]))
            self._tree_truncated = bool(tree.get("truncated"))
        paths = [p for p in self._paths if p.lower().startswith(path_prefix.lower())]
        return {"files": paths[:100], "matching_count": len(paths), "truncated": len(paths) > 100 or self._tree_truncated}

    def read_repository_file(self, path: str) -> dict:
        if not allowed_path(path):
            return {"error": "File path is not permitted"}
        # Only source files already listed by GitHub's repository tree can be read.
        if self._paths is None:
            self.list_repository_files("")
        if path not in (self._paths or []):
            return {"error": "File was not found in allowed repository tree"}
        raw = self._get(f"{self.prefix}/contents/{quote(path, safe='/')}")
        if raw.get("type") != "file" or raw.get("size", 0) > 40_000:
            return {"error": "File is not a small text file"}
        if raw.get("encoding") != "base64":
            return {"error": "Unsupported GitHub file encoding"}
        try:
            content = base64.b64decode(raw["content"], validate=False).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            return {"error": "File cannot be read as UTF-8"}
        return {"path": path, "content": content[:14_000], "url": raw.get("html_url"), "truncated": len(content) > 14_000}

    def find_related_issues(self, query: str) -> list[dict]:
        cleaned = " ".join(re.findall(r"[a-zA-Z0-9_.-]+", query))[:130]
        if not cleaned:
            return []
        # GitHub search is always pinned to the selected repo and only issues.
        q = f"repo:{self.ref.owner}/{self.ref.repo} is:issue {cleaned}"
        raw = self._get("/search/issues", q=q, per_page=8)
        return [{"number": x["number"], "title": x["title"], "url": x["html_url"], "state": x["state"]}
                for x in raw.get("items", []) if x.get("number") != self.ref.number][:7]
