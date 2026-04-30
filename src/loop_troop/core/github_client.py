"""Async GitHub REST API polling client."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, Mapping, Protocol, TypeVar
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)
SleepFn = Callable[[float], Awaitable[None]]


class GitHubUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    login: str
    id: int | None = None


class GitHubIssueRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: int
    title: str | None = None


class GitHubIssueEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    event: str
    created_at: str | None = None
    actor: GitHubUser | None = None
    issue: GitHubIssueRef | None = None


class GitHubLabel(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str


class GitHubPullRequestHead(BaseModel):
    model_config = ConfigDict(extra="allow")

    ref: str | None = None
    sha: str


class GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    number: int
    state: str
    title: str
    body: str | None = None
    updated_at: str | None = None
    user: GitHubUser | None = None
    labels: list[GitHubLabel] = Field(default_factory=list)
    head: GitHubPullRequestHead | None = None


class GitHubIssueComment(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    body: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    user: GitHubUser | None = None


class GitHubPullRequestFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    filename: str
    status: str | None = None
    patch: str | None = None


class GitHubCheckRun(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    name: str
    status: str | None = None
    conclusion: str | None = None


class GitHubIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: int
    state: str
    title: str | None = None
    body: str | None = None
    labels: list[GitHubLabel] = Field(default_factory=list)


@dataclass(slots=True)
class PollResponse(Generic[T]):
    items: list[T]
    not_modified: bool = False
    etag: str | None = None
    pages_fetched: int = 0


class ETagStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


class ShadowEventLogger(Protocol):
    def log_event(
        self,
        event: Mapping[str, Any],
        *,
        repo: str,
        default_event_type: str = "github_event",
    ) -> bool: ...


class InMemoryETagStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def set(self, key: str, value: str) -> None:
        self._values[key] = value


class GitHubClient:
    def __init__(
        self,
        *,
        pat: str | None = None,
        base_url: str = "https://api.github.com",
        timeout: float = 10.0,
        poll_interval_seconds: float = 60.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        etag_store: ETagStore | None = None,
        shadow_log: ShadowEventLogger | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn = asyncio.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.poll_interval_seconds = poll_interval_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._etag_store = etag_store or InMemoryETagStore()
        self._shadow_log = shadow_log
        self._sleep = sleep
        self._now = now

        token = pat or os.getenv("GITHUB_PAT")
        if not token:
            raise ValueError("GITHUB_PAT must be set in the environment or passed to GitHubClient.")

        self._default_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "loop-troop-github-client",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def poll_issue_events(
        self,
        owner: str,
        repo: str,
        *,
        per_page: int = 100,
    ) -> PollResponse[GitHubIssueEvent]:
        return await self._poll_collection(
            owner=owner,
            repo=repo,
            path="/issues/events",
            model=GitHubIssueEvent,
            params={"per_page": per_page},
        )

    async def poll_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        state: str = "open",
        per_page: int = 100,
    ) -> PollResponse[GitHubPullRequest]:
        return await self._poll_collection(
            owner=owner,
            repo=repo,
            path="/pulls",
            model=GitHubPullRequest,
            params={"state": state, "per_page": per_page},
        )

    async def poll_issue_comments(
        self,
        owner: str,
        repo: str,
        *,
        since: str | None = None,
        per_page: int = 100,
    ) -> PollResponse[GitHubIssueComment]:
        params: dict[str, str | int] = {"per_page": per_page}
        if since:
            params["since"] = since
        return await self._poll_collection(
            owner=owner,
            repo=repo,
            path="/issues/comments",
            model=GitHubIssueComment,
            params=params,
        )

    async def get_authenticated_user(self) -> GitHubUser:
        response = await self._client.get("/user", headers=self._default_headers)
        response.raise_for_status()
        return GitHubUser.model_validate(response.json())

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/issues/{issue_number}",
            headers=self._default_headers,
        )
        response.raise_for_status()
        return GitHubIssue.model_validate(response.json())

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": per_page},
            headers=self._default_headers,
        )
        response.raise_for_status()
        return [GitHubIssueComment.model_validate(item) for item in response.json()]

    async def get_pull_request(self, owner: str, repo: str, pull_number: int) -> GitHubPullRequest:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            headers=self._default_headers,
        )
        response.raise_for_status()
        return GitHubPullRequest.model_validate(response.json())

    async def get_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            headers={
                **self._default_headers,
                "Accept": "application/vnd.github.diff",
            },
        )
        response.raise_for_status()
        return response.text

    async def list_pull_request_files(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubPullRequestFile]:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
            params={"per_page": per_page},
            headers=self._default_headers,
        )
        response.raise_for_status()
        return [GitHubPullRequestFile.model_validate(item) for item in response.json()]

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[GitHubCheckRun]:
        response = await self._client.get(
            f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
            headers=self._default_headers,
        )
        response.raise_for_status()
        payload = response.json()
        return [GitHubCheckRun.model_validate(item) for item in payload.get("check_runs", [])]

    async def create_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        event: str,
        body: str,
        comments: list[dict[str, Any]] | None = None,
        commit_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event": event,
            "body": body,
        }
        if comments:
            payload["comments"] = comments
        if commit_id:
            payload["commit_id"] = commit_id
        response = await self._client.post(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            json=payload,
            headers=self._default_headers,
        )
        response.raise_for_status()
        return response.json()

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]:
        response = await self._client.put(
            f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
            json={"labels": labels},
            headers=self._default_headers,
        )
        response.raise_for_status()
        return [GitHubLabel.model_validate(item).name for item in response.json()]

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str,
    ) -> GitHubIssueComment:
        response = await self._client.post(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
            headers=self._default_headers,
        )
        response.raise_for_status()
        return GitHubIssueComment.model_validate(response.json())

    async def create_issue(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> GitHubIssue:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = await self._client.post(
            f"/repos/{owner}/{repo}/issues",
            json=payload,
            headers=self._default_headers,
        )
        response.raise_for_status()
        return GitHubIssue.model_validate(response.json())

    async def _poll_collection(
        self,
        *,
        owner: str,
        repo: str,
        path: str,
        model: type[T],
        params: dict[str, str | int],
    ) -> PollResponse[T]:
        next_url: str | None = f"/repos/{owner}/{repo}{path}"
        request_params: dict[str, str | int] | None = params
        items: list[T] = []
        pages_fetched = 0
        response_etag: str | None = None

        while next_url:
            cache_key = self._cache_key(next_url, request_params)
            response = await self._get(next_url, params=request_params, cache_key=cache_key)

            if response.status_code == httpx.codes.NOT_MODIFIED:
                return PollResponse(
                    items=[],
                    not_modified=True,
                    etag=self._etag_store.get(cache_key),
                    pages_fetched=pages_fetched,
                )

            pages_fetched += 1
            etag = response.headers.get("ETag")
            if etag:
                self._etag_store.set(cache_key, etag)
                if response_etag is None:
                    response_etag = etag

            payload = response.json()
            if self._shadow_log is not None:
                for item in payload:
                    self._shadow_log.log_event(
                        self._shadow_log_payload(path, item),
                        repo=f"{owner}/{repo}",
                        default_event_type=self._default_event_type(path),
                    )
            items.extend(model.model_validate(item) for item in payload)
            next_url = response.links.get("next", {}).get("url")
            request_params = None

        return PollResponse(
            items=items,
            not_modified=False,
            etag=response_etag,
            pages_fetched=pages_fetched,
        )

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None,
        cache_key: str,
    ) -> httpx.Response:
        for attempt in range(self.max_retries + 1):
            headers = dict(self._default_headers)
            etag = self._etag_store.get(cache_key)
            if etag:
                headers["If-None-Match"] = etag

            response = await self._client.get(url, params=params, headers=headers)
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return response
            if response.status_code not in (httpx.codes.FORBIDDEN, httpx.codes.TOO_MANY_REQUESTS):
                response.raise_for_status()
                return response

            if not self._is_rate_limited(response):
                response.raise_for_status()

            if attempt >= self.max_retries:
                response.raise_for_status()

            await self._sleep(self._backoff_delay(response, attempt))

        raise RuntimeError("Unreachable retry loop exit.")

    def _is_rate_limited(self, response: httpx.Response) -> bool:
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return True
        return response.headers.get("X-RateLimit-Remaining") == "0"

    def _backoff_delay(self, response: httpx.Response, attempt: int) -> float:
        delay = self.backoff_base_seconds * (2**attempt)

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass

        reset_at = response.headers.get("X-RateLimit-Reset")
        if reset_at:
            try:
                reset_delay = max(0.0, float(reset_at) - self._now())
                delay = max(delay, reset_delay)
            except ValueError:
                pass

        return delay

    @staticmethod
    def _cache_key(url: str, params: dict[str, str | int] | None) -> str:
        if not params:
            return url
        return f"{url}?{urlencode(sorted(params.items()))}"

    @staticmethod
    def _default_event_type(path: str) -> str:
        if path == "/issues/events":
            return "issue_event"
        if path == "/issues/comments":
            return "issue_comment"
        if path == "/pulls":
            return "pull_request"
        return "github_event"

    @staticmethod
    def _shadow_log_payload(path: str, item: Mapping[str, Any]) -> Mapping[str, Any]:
        if path != "/pulls":
            return item

        pull_request_id = item.get("id")
        updated_at = item.get("updated_at")
        created_at = item.get("created_at")
        event_type = "opened" if created_at and created_at == updated_at else "edited"
        event_suffix = updated_at or created_at or str(pull_request_id)
        return {
            **item,
            "id": f"pull_request:{pull_request_id}:{event_suffix}",
            "event": event_type,
            "created_at": updated_at or created_at,
            "pull_request": {"number": item.get("number")},
        }
