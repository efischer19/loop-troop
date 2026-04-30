import json

import httpx
import pytest
from pydantic import ValidationError

from loop_troop.core.github_client import GitHubClient, InMemoryETagStore
from loop_troop.shadow_log import ShadowLog


@pytest.mark.asyncio
async def test_poll_issue_events_uses_env_pat_and_returns_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            headers={"ETag": '"issues-events-etag"'},
            json=[
                {
                    "id": 1,
                    "event": "closed",
                    "created_at": "2026-04-26T00:00:00Z",
                    "actor": {"login": "octocat", "id": 1},
                    "issue": {"number": 12, "title": "Example issue"},
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with GitHubClient(client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com")) as client:
        response = await client.poll_issue_events("octo", "repo")

    assert response.not_modified is False
    assert response.etag == '"issues-events-etag"'
    assert response.pages_fetched == 1
    assert len(response.items) == 1
    assert response.items[0].event == "closed"
    assert response.items[0].actor is not None
    assert response.items[0].actor.login == "octocat"


@pytest.mark.asyncio
async def test_poll_pull_requests_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 2,
                        "number": 102,
                        "state": "open",
                        "title": "Second PR",
                        "user": {"login": "octocat"},
                    }
                ],
            )
        return httpx.Response(
            200,
            headers={
                "Link": '<https://api.github.com/repos/octo/repo/pulls?state=open&per_page=100&page=2>; rel="next"'
            },
            json=[
                {
                    "id": 1,
                    "number": 101,
                    "state": "open",
                    "title": "First PR",
                    "user": {"login": "hubot"},
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    async with GitHubClient(client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com")) as client:
        response = await client.poll_pull_requests("octo", "repo")

    assert response.pages_fetched == 2
    assert [item.number for item in response.items] == [101, 102]
    assert seen_urls == [
        "https://api.github.com/repos/octo/repo/pulls?state=open&per_page=100",
        "https://api.github.com/repos/octo/repo/pulls?state=open&per_page=100&page=2",
    ]


@pytest.mark.asyncio
async def test_poll_pull_requests_logs_unique_opened_and_updated_events(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    shadow_log = ShadowLog(tmp_path / "shadow.db")
    responses = iter(
        [
            httpx.Response(
                200,
                json=[
                    {
                        "id": 41,
                        "number": 7,
                        "state": "open",
                        "title": "First PR snapshot",
                        "created_at": "2026-04-30T10:00:00Z",
                        "updated_at": "2026-04-30T10:00:00Z",
                        "head": {"sha": "abc123", "ref": "feature/pr"},
                    }
                ],
            ),
            httpx.Response(
                200,
                json=[
                    {
                        "id": 41,
                        "number": 7,
                        "state": "open",
                        "title": "Updated PR snapshot",
                        "created_at": "2026-04-30T10:00:00Z",
                        "updated_at": "2026-04-30T11:00:00Z",
                        "head": {"sha": "def456", "ref": "feature/pr"},
                    }
                ],
            ),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    transport = httpx.MockTransport(handler)
    try:
        async with GitHubClient(
            client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com"),
            shadow_log=shadow_log,
        ) as client:
            await client.poll_pull_requests("octo", "repo")
            await client.poll_pull_requests("octo", "repo")

        rows = [
            tuple(row)
            for row in shadow_log._connection.execute(
            "SELECT event_id, event_type FROM raw_events ORDER BY id ASC"
            ).fetchall()
        ]
        assert rows == [
            ("pull_request:41:2026-04-30T10:00:00Z", "opened"),
            ("pull_request:41:2026-04-30T11:00:00Z", "edited"),
        ]
    finally:
        shadow_log.close()


@pytest.mark.asyncio
async def test_poll_issue_comments_retries_rate_limited_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    responses = iter(
        [
            httpx.Response(
                429,
                headers={"Retry-After": "2", "X-RateLimit-Remaining": "0"},
                json={"message": "rate limited"},
            ),
            httpx.Response(
                200,
                json=[{"id": 5, "body": "hello", "user": {"login": "octocat"}}],
            ),
        ]
    )
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    transport = httpx.MockTransport(handler)
    async with GitHubClient(
        client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com"),
        sleep=fake_sleep,
        backoff_base_seconds=0.5,
    ) as client:
        response = await client.poll_issue_comments("octo", "repo")

    assert slept == [2.0]
    assert [item.id for item in response.items] == [5]


@pytest.mark.asyncio
async def test_poll_issue_comments_reuses_etag_and_handles_not_modified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    etag_store = InMemoryETagStore()
    request_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_headers.append(request.headers)
        if request.headers.get("If-None-Match") == '"comments-etag"':
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={"ETag": '"comments-etag"'},
            json=[{"id": 9, "body": "first", "user": {"login": "octocat"}}],
        )

    transport = httpx.MockTransport(handler)
    async with GitHubClient(
        client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com"),
        etag_store=etag_store,
    ) as client:
        first = await client.poll_issue_comments("octo", "repo")
        second = await client.poll_issue_comments("octo", "repo")

    assert first.not_modified is False
    assert [item.id for item in first.items] == [9]
    assert second.not_modified is True
    assert second.items == []
    assert second.etag == '"comments-etag"'
    assert request_headers[1]["If-None-Match"] == '"comments-etag"'


@pytest.mark.asyncio
async def test_poll_issue_events_logs_before_model_validation(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    shadow_log = ShadowLog(tmp_path / "shadow.db")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": 77,
                    "created_at": "2026-04-26T00:00:00Z",
                    "actor": {"login": "octocat", "id": 1},
                }
            ],
        )

    transport = httpx.MockTransport(handler)
    try:
        async with GitHubClient(
            client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com"),
            shadow_log=shadow_log,
        ) as client:
            with pytest.raises(ValidationError):
                await client.poll_issue_events("octo", "repo")

        pending = shadow_log.get_pending_events()
        assert [item.event_id for item in pending] == ["77"]
        assert pending[0].event_type == "issue_event"
        assert pending[0].repo == "octo/repo"
    finally:
        shadow_log.close()


@pytest.mark.asyncio
async def test_create_issue_and_comment_use_github_rest_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    seen_requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode()
        if request.url.path.endswith("/issues/12/comments"):
            seen_requests.append((request.method, request.url.path, {"raw": payload}))
            return httpx.Response(201, json={"id": 44, "body": "planned"})
        if request.url.path.endswith("/issues"):
            seen_requests.append((request.method, request.url.path, {"raw": payload}))
            return httpx.Response(
                201,
                json={
                    "number": 12,
                    "state": "open",
                    "title": "Child issue",
                    "body": "Work item",
                    "labels": [{"name": "loop: needs-planning"}],
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with GitHubClient(client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com")) as client:
        created_issue = await client.create_issue(
            "octo",
            "repo",
            title="Child issue",
            body="Work item",
            labels=["loop: needs-planning"],
        )
        created_comment = await client.create_issue_comment("octo", "repo", 12, body="planned")

    assert created_issue.number == 12
    assert created_issue.labels[0].name == "loop: needs-planning"
    assert created_comment.body == "planned"
    assert seen_requests == [
        ("POST", "/repos/octo/repo/issues", {"raw": '{"title":"Child issue","body":"Work item","labels":["loop: needs-planning"]}'}),
        ("POST", "/repos/octo/repo/issues/12/comments", {"raw": '{"body":"planned"}'}),
    ]


@pytest.mark.asyncio
async def test_pull_request_review_helpers_use_expected_github_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    seen_requests: list[tuple[str, str, str | None, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        payload = json.loads(body) if body else None
        seen_requests.append((request.method, request.url.path, request.headers.get("Accept"), payload))
        if request.url.path.endswith("/pulls/12/files"):
            return httpx.Response(200, json=[{"filename": "tests/test_reviewer.py", "patch": "@@ -1 +1 @@"}])
        if request.url.path.endswith("/commits/abc123/check-runs"):
            return httpx.Response(200, json={"check_runs": [{"id": 9, "name": "pytest", "conclusion": "success"}]})
        if request.url.path.endswith("/pulls/12/reviews"):
            return httpx.Response(200, json={"id": 77, "state": "APPROVED"})
        if request.url.path.endswith("/pulls/12"):
            if request.headers.get("Accept") == "application/vnd.github.diff":
                return httpx.Response(200, text="diff --git a/file.py b/file.py")
            return httpx.Response(
                200,
                json={
                    "id": 12,
                    "number": 12,
                    "state": "open",
                    "title": "Review me",
                    "body": "Closes #42",
                    "labels": [{"name": "loop: needs-review"}],
                    "head": {"sha": "abc123", "ref": "feature/review"},
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    async with GitHubClient(client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com")) as client:
        pull_request = await client.get_pull_request("octo", "repo", 12)
        pull_request_diff = await client.get_pull_request_diff("octo", "repo", 12)
        files = await client.list_pull_request_files("octo", "repo", 12)
        check_runs = await client.get_check_runs("octo", "repo", "abc123")
        review = await client.create_pull_request_review(
            "octo",
            "repo",
            12,
            event="APPROVE",
            body="Looks good",
            comments=[{"path": "tests/test_reviewer.py", "body": "Nice", "line": 8, "side": "RIGHT"}],
            commit_id="abc123",
        )

    assert pull_request.labels[0].name == "loop: needs-review"
    assert pull_request.head is not None
    assert pull_request.head.sha == "abc123"
    assert pull_request_diff == "diff --git a/file.py b/file.py"
    assert files[0].filename == "tests/test_reviewer.py"
    assert check_runs[0].conclusion == "success"
    assert review["state"] == "APPROVED"
    assert seen_requests == [
        ("GET", "/repos/octo/repo/pulls/12", "application/vnd.github+json", None),
        ("GET", "/repos/octo/repo/pulls/12", "application/vnd.github.diff", None),
        ("GET", "/repos/octo/repo/pulls/12/files", "application/vnd.github+json", None),
        ("GET", "/repos/octo/repo/commits/abc123/check-runs", "application/vnd.github+json", None),
        (
            "POST",
            "/repos/octo/repo/pulls/12/reviews",
            "application/vnd.github+json",
            {
                "event": "APPROVE",
                "body": "Looks good",
                "comments": [{"path": "tests/test_reviewer.py", "body": "Nice", "line": 8, "side": "RIGHT"}],
                "commit_id": "abc123",
            },
        ),
    ]
