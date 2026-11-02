"""Shared helpers for tests that mock GitHub with httpx.MockTransport (no network)."""

from collections import Counter

import httpx

from app.services.github import GitHubClient

NOW = 1_700_000_000.0
TOKEN = "ghp_SUPERSECRETTOKENVALUE123"

REPO = {
    "name": "r", "full_name": "o/r", "owner": {"login": "o"}, "html_url": "https://github.com/o/r",
    "default_branch": "main", "private": False, "description": None, "language": "Python", "topics": ["ml"],
    "stargazers_count": 5, "forks_count": 1, "open_issues_count": 3, "archived": False, "fork": False,
    "license": {"spdx_id": "MIT"}, "pushed_at": "2024-01-02T03:04:05Z", "created_at": "2020-01-01T00:00:00Z",
}


def issue(n, pr=False, **kw):
    d = {"number": n, "title": f"t{n}", "body": None, "state": "open", "html_url": f"https://github.com/o/r/issues/{n}",
         "labels": [{"name": "bug"}], "user": {"login": "u"}, "assignees": [], "comments": 0,
         "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-02T00:00:00Z", **kw}
    if pr:
        d["pull_request"] = {"url": "x"}
    return d


class Script:
    """Feeds queued responses/exceptions to the mock transport and records requests and sleeps."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if callable(step):
            step = step(request)
        if isinstance(step, Exception):
            raise step
        return step

    def client(self, **kw) -> GitHubClient:
        kw.setdefault("sleep", self.sleeps.append)
        kw.setdefault("clock", lambda: NOW + sum(self.sleeps))  # time advances as the client "sleeps"
        kw.setdefault("rng", lambda: 0.0)
        return GitHubClient(transport=httpx.MockTransport(self.handler), **kw)


def ok(data, **headers):
    return httpx.Response(200, json=data, headers=headers)


def status(code, data=None, **headers):
    return httpx.Response(code, json=data if data is not None else {"message": "x"}, headers=headers)


class FakeGitHub:
    """Routes mocked GitHub requests by path and counts calls."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: Counter = Counter()
        self.script = Script(self._handle)
        self.client = self.script.client(token=TOKEN)

    def _handle(self, request: httpx.Request):
        self.calls[request.url.path] += 1
        route = self.routes.get(request.url.path)
        if route is None:
            return status(404, {"message": "Not Found"})
        return route(request) if callable(route) else route

    @property
    def total(self) -> int:
        return sum(self.calls.values())
