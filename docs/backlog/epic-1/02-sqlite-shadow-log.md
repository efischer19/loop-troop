# feat: SQLite Shadow Log Schema & Event Logger

## What do you want to build?

Design and implement the SQLite-backed shadow log that persists every polled GitHub event locally. This log serves as the single source of truth for event replay, deduplication, and debugging. Every event fetched by the GitHub client must be written to this log before any downstream processing occurs ("log-first" guarantee).

## Acceptance Criteria

- [ ] SQLite database schema with tables for: `raw_events` (immutable append-only log), `event_state` (processing status: `pending`, `dispatched`, `completed`, `failed`), and `daemon_checkpoints` (last-seen event IDs and ETags per endpoint).
- [ ] The `event_state` table includes a `dispatched_at` timestamp column to support zombie sweep detection (see Epic 1 Ticket 4).
- [ ] A `ShadowLog` class with methods: `log_event()`, `get_pending_events()`, `mark_dispatched()`, `mark_completed()`, `mark_failed()`, `get_checkpoint()`, `set_checkpoint()`.
- [ ] All writes use transactions to ensure atomicity.
- [ ] Events are deduplicated by GitHub event ID before insertion.
- [ ] Database file location is configurable via environment variable (`LOOP_TROOP_DB_PATH`), defaulting to `~/.loop-troop/shadow.db`.
- [ ] Schema migrations are handled via a simple version table (no heavy ORM — use raw SQL or `sqlite3` stdlib).
- [ ] Schema is extensible for the `llm_metrics` table (Epic 5) without breaking existing tables.
- [ ] Unit tests covering: log-first write, deduplication, state transitions, checkpoint persistence across restarts, dispatched_at timestamp recording.

## Implementation Notes (Optional)

Use Python's built-in `sqlite3` module — no SQLAlchemy. Keep the schema simple; the `raw_events` table should store the full JSON payload as a TEXT column alongside indexed columns for `event_id`, `event_type`, `repo`, `created_at`, and `processed_at`. The `daemon_checkpoints` table enables the daemon to resume from exactly where it left off after a crash. WAL mode is recommended for concurrent read/write performance.

The `dispatched_at` column is critical for the zombie sweep in Ticket 4 — it records when an event entered the `dispatched` state, enabling timeout-based recovery of stalled events.
