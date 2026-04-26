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
