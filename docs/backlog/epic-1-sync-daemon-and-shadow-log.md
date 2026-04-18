# Epic 1: The Native Sync Daemon & Shadow Log

> Building the native Python polling script, GitHub API wrapper, SQLite event logger, and the 8B dispatcher logic.

This epic establishes the foundational event-processing pipeline. The Sync Daemon is the heartbeat of Loop Troop — it polls GitHub, logs every event to SQLite for replay, and dispatches work to the appropriate cognitive tier.

---

### [Epic 1] Ticket 1: GitHub REST API Polling Client & Authentication
**Priority:** High
**Description:**
Build a lightweight, authenticated GitHub REST API client that polls for issue events, PR events, and comment activity on configured target repositories. The client must support pagination, rate-limit awareness (respecting `X-RateLimit-Remaining` and `Retry-After` headers), and configurable poll intervals. Authentication is via a GitHub Personal Access Token (PAT) loaded from environment variables.

**Acceptance Criteria:**
* [ ] A `GitHubClient` class that wraps `httpx` (async) for GitHub REST API v3 calls.
* [ ] Supports polling `/repos/{owner}/{repo}/issues/events`, `/repos/{owner}/{repo}/pulls`, and `/repos/{owner}/{repo}/issues/comments` endpoints.
* [ ] Reads `GITHUB_PAT` from environment variables (never hardcoded, never passed to any Docker container).
* [ ] Implements exponential backoff on rate-limit responses (HTTP 403/429).
* [ ] Tracks `ETag` / `If-None-Match` headers to avoid redundant payload processing.
* [ ] Returns typed dataclass/Pydantic models (not raw dicts).
* [ ] Unit tests with mocked HTTP responses covering: normal polling, rate-limit backoff, pagination, and ETag caching.

**Implementation Notes (Tech Lead hints):**
Use `httpx.AsyncClient` for non-blocking I/O. Store the `ETag` per-endpoint in the SQLite shadow log (Ticket 2) to survive daemon restarts. Consider a thin abstraction over the endpoint URLs so adding new event sources later is trivial. Do NOT use PyGithub — we want minimal dependencies and full control over HTTP lifecycle.

---

### [Epic 1] Ticket 2: SQLite Shadow Log Schema & Event Logger
**Priority:** High
**Description:**
Design and implement the SQLite-backed shadow log that persists every polled GitHub event locally. This log serves as the single source of truth for event replay, deduplication, and debugging. Every event fetched by the GitHub client (Ticket 1) must be written to this log before any downstream processing occurs ("log-first" guarantee).

**Acceptance Criteria:**
* [ ] SQLite database schema with tables for: `raw_events` (immutable append-only log), `event_state` (processing status: `pending`, `dispatched`, `completed`, `failed`), and `daemon_checkpoints` (last-seen event IDs and ETags per endpoint).
* [ ] A `ShadowLog` class with methods: `log_event()`, `get_pending_events()`, `mark_dispatched()`, `mark_completed()`, `mark_failed()`, `get_checkpoint()`, `set_checkpoint()`.
* [ ] All writes use transactions to ensure atomicity.
* [ ] Events are deduplicated by GitHub event ID before insertion.
* [ ] Database file location is configurable via environment variable (`LOOP_TROOP_DB_PATH`), defaulting to `~/.loop-troop/shadow.db`.
* [ ] Schema migrations are handled via a simple version table (no heavy ORM — use raw SQL or `sqlite3` stdlib).
* [ ] Unit tests covering: log-first write, deduplication, state transitions, checkpoint persistence across restarts.

**Implementation Notes (Tech Lead hints):**
Use Python's built-in `sqlite3` module — no SQLAlchemy. Keep the schema simple; the `raw_events` table should store the full JSON payload as a TEXT column alongside indexed columns for `event_id`, `event_type`, `repo`, `created_at`, and `processed_at`. The `daemon_checkpoints` table enables the daemon to resume from exactly where it left off after a crash. WAL mode is recommended for concurrent read/write performance.

---

### [Epic 1] Ticket 3: Tier 1 (8B) Dispatcher — Label Waterfall Router
**Priority:** High
**Description:**
Implement the Tier 1 dispatcher that reads pending events from the shadow log and routes them to the appropriate worker tier using a "Label Waterfall" pattern. The dispatcher uses an 8B model (via Ollama) to classify events and decide on label transitions. Labels on GitHub issues drive the state machine: `loop: needs-planning` → Architect (Tier 3), `loop: ready` → Coder (Tier 2), `loop: needs-review` → Reviewer (Tier 3).

**Acceptance Criteria:**
* [ ] A `Dispatcher` class that reads `pending` events from the `ShadowLog`.
* [ ] Uses Instructor + Pydantic to call the 8B model (via Ollama) for event classification, producing a structured `DispatchDecision` (target tier, label action, reasoning).
* [ ] Applies label changes to GitHub issues via the `GitHubClient` (Ticket 1).
* [ ] Follows a strict label state machine: only valid transitions are allowed (e.g., cannot jump from `loop: needs-planning` directly to `loop: done`).
* [ ] Marks events as `dispatched` in the shadow log after successful label application.
* [ ] Handles Ollama inference failures gracefully (retry with backoff, mark event as `failed` after 3 attempts).
* [ ] The dispatcher MUST NOT execute any code or spawn Docker containers — it only reads, classifies, and labels.
* [ ] Unit tests with mocked Ollama responses covering: valid routing, invalid label transitions (rejected), Ollama timeout handling.

**Implementation Notes (Tech Lead hints):**
The label waterfall is the core state machine. Define the valid transitions as a simple dict/enum, not in the LLM prompt. The LLM's job is to *classify the event type*, not to decide the state machine transitions — those are deterministic. Use `instructor.from_openai()` pointed at the Ollama-compatible endpoint. Keep the 8B prompt minimal: event payload + "What type of event is this?" → `DispatchDecision` schema.

---

### [Epic 1] Ticket 4: Sync Daemon Main Loop & Graceful Shutdown
**Priority:** Medium
**Description:**
Wire together the GitHub client, shadow log, and dispatcher into a single long-running daemon process with a clean main loop, signal handling, and graceful shutdown. The daemon should be launchable via a single CLI command and configurable via environment variables and/or a TOML config file.

**Acceptance Criteria:**
* [ ] A `main()` entrypoint that initializes the `GitHubClient`, `ShadowLog`, and `Dispatcher`, then enters the poll-dispatch loop.
* [ ] Configurable poll interval (default: 30 seconds) via `LOOP_TROOP_POLL_INTERVAL` or config file.
* [ ] Handles `SIGINT` and `SIGTERM` for graceful shutdown: finishes current dispatch cycle, flushes shadow log, then exits cleanly.
* [ ] Structured logging (JSON or key-value) to stdout with configurable log level.
* [ ] Startup self-check: verifies GitHub PAT validity, Ollama reachability, and SQLite writability before entering the loop.
* [ ] Supports a `--dry-run` flag that polls and logs events but does not apply label changes or dispatch work.
* [ ] Integration test that runs the daemon for 2-3 poll cycles against mocked GitHub/Ollama endpoints and verifies events flow through the full pipeline.

**Implementation Notes (Tech Lead hints):**
Use `asyncio` for the main loop. Signal handling in asyncio requires `loop.add_signal_handler()`. For config, consider a simple TOML file (`tomllib` is stdlib in 3.11+) with env var overrides. The `--dry-run` flag is critical for safe local testing. Use Python's `logging` module with a JSON formatter for structured logs.
