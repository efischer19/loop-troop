# feat: GitHub REST API Polling Client & Authentication

## What do you want to build?

Build a lightweight, authenticated GitHub REST API client that polls for issue events, PR events, and comment activity on configured target repositories. The client must support pagination, rate-limit awareness (respecting `X-RateLimit-Remaining` and `Retry-After` headers), and configurable poll intervals. Authentication is via a GitHub Personal Access Token (PAT) loaded from environment variables.

For the MVP, use a dedicated GitHub Machine Account (free, within GitHub TOS) with a Fine-Grained PAT scoped to target repositories. This provides clear visual separation of bot-authored PRs and comments from human activity.

## Acceptance Criteria

- [ ] A `GitHubClient` class that wraps `httpx` (async) for GitHub REST API v3 calls.
- [ ] Supports polling `/repos/{owner}/{repo}/issues/events`, `/repos/{owner}/{repo}/pulls`, and `/repos/{owner}/{repo}/issues/comments` endpoints.
- [ ] Reads `GITHUB_PAT` from environment variables (never hardcoded, never passed to any Docker container per ADR-0001).
- [ ] Implements exponential backoff on rate-limit responses (HTTP 403/429).
- [ ] Tracks `ETag` / `If-None-Match` headers to avoid redundant payload processing.
- [ ] Returns typed dataclass/Pydantic models (not raw dicts).
- [ ] Supports a Machine Account PAT as the recommended authentication method for MVP.
- [ ] Unit tests with mocked HTTP responses covering: normal polling, rate-limit backoff, pagination, and ETag caching.

## Implementation Notes (Optional)

Use `httpx.AsyncClient` for non-blocking I/O. Store the `ETag` per-endpoint in the SQLite shadow log (Ticket 2) to survive daemon restarts. Consider a thin abstraction over the endpoint URLs so adding new event sources later is trivial. Do NOT use PyGithub — we want minimal dependencies and full control over HTTP lifecycle.

The Machine Account approach is recommended over a personal PAT because: (1) bot activity is visually distinct in GitHub's UI, (2) token scope can be restricted to only the target repos, and (3) revoking the bot's access doesn't affect the developer's personal account.
