import base64
from types import SimpleNamespace as NS

import pytest

from issuepilot.github_api import GitHubAPI, IssueRef, parse_issue_url, allowed_path
from issuepilot.agent import dispatch, investigate


class FakeHTTPResponse:
    def __init__(self, value):
        self.value = value
    def raise_for_status(self):
        pass
    def json(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.urls = []
    def get(self, url, params=None, timeout=None):
        self.urls.append((url, params))
        if url.endswith('/issues/7'):
            return FakeHTTPResponse({'number': 7, 'title':'Timeout in task queue', 'state':'open',
                                    'body':'timeout', 'html_url':'https://github.com/demo/app/issues/7','labels':[]})
        if url.endswith('/issues/7/comments'):
            return FakeHTTPResponse([{'body':'Reproducible','html_url':'https://github.com/demo/app/issues/7#issuecomment-3'}])
        if url.endswith('/repos/demo/app'):
            return FakeHTTPResponse({'default_branch':'main'})
        if '/git/trees/' in url:
            return FakeHTTPResponse({'tree':[{'path':'src/queue.py','type':'blob'},
                                             {'path':'.env','type':'blob'},
                                             {'path':'private.key','type':'blob'}], 'truncated':False})
        if '/contents/' in url:
            return FakeHTTPResponse({'type':'file', 'size':30, 'encoding':'base64',
                                     'content':base64.b64encode(b'def wait():\n    return None\n').decode(),
                                     'html_url':'https://github.com/demo/app/blob/main/src/queue.py'})
        if '/search/issues' in url:
            return FakeHTTPResponse({'items':[{'number':4,'title':'Another timeout','state':'open','html_url':'https://github.com/demo/app/issues/4'}]})
        raise AssertionError(url)


def test_parse_issue_url():
    assert parse_issue_url('https://github.com/demo/app/issues/7') == IssueRef('demo','app',7)
    for value in ('https://evil.com/demo/app/issues/7', 'https://github.com/demo/app/pull/7',
                  'https://github.com/demo/app/issues/0'):
        with pytest.raises(ValueError):
            parse_issue_url(value)


def test_paths_reject_secrets_and_traversal():
    assert allowed_path('src/queue.py')
    for p in ('../secrets.py', '.env', 'private.key', 'dir/.private.py','/etc/passwd','source.zip'):
        assert not allowed_path(p)


def test_read_only_api_filters_files():
    http = FakeSession()
    api = GitHubAPI(IssueRef('demo','app',7), session=http)
    assert api.get_issue()['title'] == 'Timeout in task queue'
    assert api.get_issue_comments()[0]['body'] == 'Reproducible'
    assert api.list_repository_files('src')['files'] == ['src/queue.py']
    assert api.read_repository_file('src/queue.py')['content'].startswith('def wait')
    assert 'error' in api.read_repository_file('../secret.py')
    assert api.find_related_issues('timeout')[0]['number'] == 4
    assert all('/repos/demo/app' in url or url.endswith('/search/issues') for url, _ in http.urls)


def test_search_is_scoped_to_repo():
    http = FakeSession()
    api = GitHubAPI(IssueRef('demo','app',7),session=http)
    api.find_related_issues('repo:other/other is:pr timeout')
    q = http.urls[-1][1]['q']
    assert q.startswith('repo:demo/app is:issue')
    assert 'repo:other/other' not in q


def test_dispatch_rejects_unknown_tools_and_parameters():
    api=GitHubAPI(IssueRef('demo','app',7),session=FakeSession())
    with pytest.raises(ValueError):
        dispatch(api,'delete_issue',{})
    with pytest.raises(ValueError):
        dispatch(api,'get_issue', {'token':'x'})


class FakeResponses:
    def __init__(self):
        self.requests=[]
    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests)==1:
            return NS(output=[NS(type='function_call', name='get_issue', arguments='{}', call_id='call_1')],
                      usage=NS(input_tokens=100,output_tokens=10), output_text='')
        assert any(isinstance(i,dict) and i.get('type') == 'function_call_output' for i in kwargs['input'])
        return NS(output=[NS(type='message')],usage=NS(input_tokens=200,output_tokens=25),
                  output_text='## Issue Summary\nTimeout report\n## Evidence\nIssue URL')


def test_real_tool_loop_sequence_with_mock_model():
    client=NS(responses=FakeResponses())
    api=GitHubAPI(IssueRef('demo','app',7),session=FakeSession())
    result=investigate(api,client)
    assert len(client.responses.requests)==2
    assert result.tool_calls == [{'name':'get_issue','arguments':{},'ok':True}]
    assert result.usage == {'input_tokens':300,'output_tokens':35}
    assert 'Issue Summary' in result.report


def test_invalid_source_path_never_hits_github():
    http = FakeSession()
    api = GitHubAPI(IssueRef('demo','app',7), session=http)
    assert 'error' in api.read_repository_file('.env')
    assert not http.urls


class EmptyResponses:
    def create(self, **kwargs):
        return NS(output=[NS(type='message')], usage=None, output_text='Unsupported speculation')


def test_agent_rejects_answer_without_tools():
    with pytest.raises(RuntimeError, match='without using any tools'):
        investigate(GitHubAPI(IssueRef('demo','app',7), session=FakeSession()), NS(responses=EmptyResponses()))
