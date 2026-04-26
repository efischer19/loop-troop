# feat: GitHub App Authentication Transition

## What do you want to build?

Transition the authentication mechanism from a raw Personal Access Token (PAT) to a GitHub App installation flow. While the MVP uses a Machine Account PAT (simple and effective for single-user/small-team use), a GitHub App provides a more scalable, secure, and developer-friendly authentication model for open-source deployment — making it trivial for other developers to install and use Loop Troop on their own repositories.

## Acceptance Criteria

- [ ] A `GitHubAppAuth` class that handles the GitHub App authentication flow: JWT generation → installation token exchange → authenticated API calls.
- [ ] The `GitHubClient` (Epic 1 Ticket 1) is updated to accept either a PAT or a GitHub App configuration, with a unified interface so downstream code doesn't need to know which auth method is in use.
- [ ] GitHub App configuration via environment variables: `LOOP_TROOP_APP_ID`, `LOOP_TROOP_APP_PRIVATE_KEY_PATH`, `LOOP_TROOP_APP_INSTALLATION_ID`.
- [ ] Installation tokens are automatically refreshed before expiry (GitHub App tokens expire after 1 hour).
- [ ] The `Config` class (Epic 5 Ticket 5) supports both auth modes, with clear error messages if neither is configured.
- [ ] Documentation in the quickstart guide explaining both auth options: PAT (simple, for personal use) and GitHub App (recommended for teams/open-source).
- [ ] Neither the PAT nor the App private key is ever passed to Docker containers or included in LLM prompts (per ADR-0001).
- [ ] Unit tests covering: JWT generation, token refresh, fallback to PAT when App config is absent, credential isolation validation.

## Implementation Notes (Optional)

Use `PyJWT` for JWT generation (required for GitHub App auth). The flow is: (1) generate a JWT signed with the App's private key, (2) exchange it for an installation token via `POST /app/installations/{installation_id}/access_tokens`, (3) use the installation token for all API calls. The installation token expires after 1 hour, so implement a lazy refresh (check expiry before each API call).

The GitHub App approach is better for open-source because: (1) users install the App on their repos with a single click, (2) permissions are managed at the App level (not per-user), (3) rate limits are higher (5,000 requests/hour vs. 5,000 for PATs), and (4) the bot identity is clearly distinguished in the GitHub UI.
