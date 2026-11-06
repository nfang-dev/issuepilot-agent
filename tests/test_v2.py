"""V2 tests: bounded code search, AST inspection, and non-applying patch proposals."""
import base64
from types import SimpleNamespace as NS

from issuepilot.code_intelligence import (
    find_text_matches, inspect_python_symbols, rank_file_paths,
)
from issuepilot.github_api import GitHubAPI, IssueRef
from issuepilot.agent import Investigation, dispatch, propose_patch, TOOLS


SAMPLE = """class Base:
    def validate(self, value):
        return value

class Child(Base):
    def validate(self, value):
        return super().validate(value)

def check():
    return Child().validate("token")
"""


class Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class Session:
    def __init__(self):
        self.headers = {}
        self.calls = []
        self.files = {
            "src/example.py": SAMPLE,
            "src/other.py": "def no_match():\n    return None\n",
        }

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if url.endswith("/repos/demo/app"):
            return Response({"default_branch": "main"})
        if "/git/trees/" in url:
            return Response({"tree": [
                {"path": path, "type": "blob"} for path in self.files
            ] + [{"path": ".env", "type": "blob"}], "truncated": False})
        if "/contents/" in url:
            path = url.split("/contents/", 1)[1]
            text = self.files[path]
            return Response({
                "type": "file", "size": len(text),
                "content": base64.b64encode(text.encode()).decode(),
                "encoding": "base64",
                "html_url": f"https://github.com/demo/app/blob/main/{path}",
            })
        raise AssertionError(f"Unexpected GitHub call: {url}")


def test_rank_file_paths_prefers_relevant_source():
    paths = ["docs/help.md", "tests/test_signer.py", "src/signer.py"]
    assert rank_file_paths(paths, "signer")[0] == "src/signer.py"


def test_search_returns_line_numbered_evidence():
    session = Session()
    api = GitHubAPI(IssueRef("demo", "app", 42), session=session)
    result = api.search_repository_code("validate", "")
    assert result["matches"]
    assert result["matches"][0]["path"] == "src/example.py"
    assert result["matches"][0]["url"].endswith("#L2")
    assert "2: " in result["matches"][0]["snippet"]
    assert result["files_scanned"] <= 18


def test_code_search_rejects_invalid_prefix():
    session = Session()
    api = GitHubAPI(IssueRef("demo", "app", 42), session=session)
    result = api.search_repository_code("validate", "../")
    assert "error" in result
    assert not session.calls


def test_ast_inspection_returns_bases_and_calls():
    api = GitHubAPI(IssueRef("demo", "app", 42), session=Session())
    result = api.inspect_python_symbol("src/example.py", "Child.validate")
    assert result["matches"][0]["qualified_name"] == "Child.validate"
    assert "Base" in result["matches"][0]["bases"]
    assert "super().validate" in result["matches"][0]["calls"]
    assert result["matches"][0]["url"].endswith("#L6-L7")


def test_ast_rejects_bad_symbol_without_execution():
    result = inspect_python_symbols(SAMPLE, "src/example.py", "__import__('os')", "")
    assert "error" in result


def test_ast_only_allows_known_python_source():
    session = Session()
    api = GitHubAPI(IssueRef("demo", "app", 42), session=session)
    assert "error" in api.inspect_python_symbol(".env", "foo")
    assert not session.calls


def test_search_reports_partial_coverage():
    api = GitHubAPI(IssueRef("demo", "app", 42), session=Session())
    result = api.search_repository_code("nonexistent", "")
    assert result["matches"] == []
    assert result["candidate_files"] == 2


def test_function_call_schema_includes_v2_tools():
    names = {tool["name"] for tool in TOOLS}
    assert {"search_repository_code", "inspect_python_symbol"}.issubset(names)
    api = GitHubAPI(IssueRef("demo", "app", 42), session=Session())
    result = dispatch(api, "inspect_python_symbol", {
        "path": "src/example.py", "symbol": "check",
    })
    assert "Child().validate" in result["matches"][0]["calls"]


class PatchResponses:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return NS(output_text="## Proposed Change\nInvestigate the verifier first.\n"
                              "## Regression Tests\nReject unsupported kwargs.\n"
                              "## Risks and Compatibility\nRequires review.\n"
                              "## Unverified Assumptions\nNo runtime verification.\n")


def test_draft_proposal_never_writes_repo_or_calls_tools():
    responses = PatchResponses()
    session = Session()
    api = GitHubAPI(IssueRef("demo", "app", 42), session=session)
    report = Investigation(report="Potential issue. Insufficient evidence.",
                           tool_calls=[{"name": "get_issue", "arguments": {}, "ok": True}])
    proposal = propose_patch(api, NS(responses=responses), report)
    assert "NOT APPLIED OR VERIFIED" in proposal
    assert "Investigate the verifier" in proposal
    assert responses.requests[0]["tool_choice"] == "none"
    assert not session.calls
