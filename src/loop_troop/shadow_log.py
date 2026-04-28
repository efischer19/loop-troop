"""SQLite-backed shadow log for polled GitHub events."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

DEFAULT_DB_PATH = Path("~/.loop-troop/shadow.db").expanduser()


@dataclass(frozen=True, slots=True)
class LoggedEvent:
    event_id: str
    event_type: str
    repo: str
    created_at: str | None
    processed_at: str
    payload: dict[str, Any]
    status: str
    dispatched_at: str | None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    endpoint: str
    last_event_id: str | None
    etag: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class SweptEvent:
    event_id: str
    repo: str
    dispatched_at: str
    dispatch_target: str | None
    stuck_seconds: float


class ShadowLog:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        configured_path = db_path or os.getenv("LOOP_TROOP_DB_PATH")
        self.db_path = Path(configured_path).expanduser() if configured_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def close(self) -> None:
        self._connection.commit()
        self._connection.close()

    def __enter__(self) -> ShadowLog:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def log_event(
        self,
        event: Mapping[str, Any],
        *,
        repo: str,
        default_event_type: str = "github_event",
    ) -> bool:
        event_id = str(event["id"])
        event_type = str(event.get("event") or event.get("type") or default_event_type)
        created_at = self._string_or_none(event.get("created_at"))
        payload = json.dumps(event, sort_keys=True)

        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT OR IGNORE INTO raw_events (
                    event_id,
                    event_type,
                    repo,
                    created_at,
                    payload
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, event_type, repo, created_at, payload),
            )
            if cursor.rowcount == 0:
                return False

            self._connection.execute(
                """
                INSERT INTO event_state (event_id, status)
                VALUES (?, 'pending')
                """,
                (event_id,),
            )

        return True

    def get_pending_events(self) -> list[LoggedEvent]:
        rows = self._connection.execute(
            """
            SELECT
                raw_events.event_id,
                raw_events.event_type,
                raw_events.repo,
                raw_events.created_at,
                raw_events.processed_at,
                raw_events.payload,
                event_state.status,
                event_state.dispatched_at
            FROM raw_events
            INNER JOIN event_state ON event_state.event_id = raw_events.event_id
            WHERE event_state.status = 'pending'
            ORDER BY raw_events.id ASC
            """
        ).fetchall()
        return [self._logged_event_from_row(row) for row in rows]

    def mark_dispatched(self, event_id: str | int, *, dispatch_target: str | None = None) -> None:
        self._update_state(
            str(event_id),
            status="dispatched",
            dispatched_at=True,
            dispatch_target=dispatch_target,
        )

    def mark_completed(self, event_id: str | int) -> None:
        self._update_state(str(event_id), status="completed", dispatched_at=False)

    def mark_failed(self, event_id: str | int) -> None:
        self._update_state(str(event_id), status="failed", dispatched_at=False)

    def sweep_dispatched_events(
        self,
        *,
        timeout_seconds: float,
        now: datetime | None = None,
    ) -> list[SweptEvent]:
        reference = now or datetime.now(UTC)
        threshold = self._format_timestamp(reference.timestamp() - timeout_seconds)
        rows = self._connection.execute(
            """
            SELECT
                event_state.event_id,
                raw_events.repo,
                event_state.dispatched_at,
                event_state.dispatch_target
            FROM event_state
            INNER JOIN raw_events ON raw_events.event_id = event_state.event_id
            WHERE event_state.status = 'dispatched'
              AND event_state.dispatched_at IS NOT NULL
              AND event_state.dispatched_at < ?
            ORDER BY event_state.dispatched_at ASC
            """,
            (threshold,),
        ).fetchall()
        swept_events = [
            SweptEvent(
                event_id=row["event_id"],
                repo=row["repo"],
                dispatched_at=row["dispatched_at"],
                dispatch_target=row["dispatch_target"],
                stuck_seconds=max(
                    0.0,
                    reference.timestamp() - self._parse_timestamp(row["dispatched_at"]).timestamp(),
                ),
            )
            for row in rows
        ]
        if not swept_events:
            return []

        with self._connection:
            self._connection.executemany(
                """
                UPDATE event_state
                SET
                    status = 'pending',
                    dispatched_at = NULL,
                    dispatch_target = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE event_id = ?
                """,
                [(event.event_id,) for event in swept_events],
            )
        return swept_events

    def verify_writable(self) -> None:
        with self._connection:
            self._connection.execute("SAVEPOINT shadow_log_healthcheck")
            try:
                self._connection.execute(
                    """
                    INSERT INTO daemon_checkpoints (endpoint, last_event_id, etag)
                    VALUES ('__healthcheck__', NULL, NULL)
                    ON CONFLICT(endpoint) DO UPDATE SET
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    """
                )
            finally:
                self._connection.execute("ROLLBACK TO SAVEPOINT shadow_log_healthcheck")
                self._connection.execute("RELEASE SAVEPOINT shadow_log_healthcheck")

    def get_checkpoint(self, endpoint: str) -> Checkpoint | None:
        row = self._connection.execute(
            """
            SELECT endpoint, last_event_id, etag, updated_at
            FROM daemon_checkpoints
            WHERE endpoint = ?
            """,
            (endpoint,),
        ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            endpoint=row["endpoint"],
            last_event_id=row["last_event_id"],
            etag=row["etag"],
            updated_at=row["updated_at"],
        )

    def set_checkpoint(
        self,
        endpoint: str,
        *,
        last_event_id: str | int | None,
        etag: str | None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO daemon_checkpoints (endpoint, last_event_id, etag)
                VALUES (?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    etag = excluded.etag,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (endpoint, None if last_event_id is None else str(last_event_id), etag),
            )

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version INTEGER PRIMARY KEY
                )
                """
            )
            current_version = self._connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_versions"
            ).fetchone()[0]
            if current_version == 0:
                self._connection.executescript(
                    """
                    CREATE TABLE raw_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        repo TEXT NOT NULL,
                        created_at TEXT,
                        payload TEXT NOT NULL,
                        processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    );

                    CREATE INDEX idx_raw_events_event_type ON raw_events(event_type);
                    CREATE INDEX idx_raw_events_repo ON raw_events(repo);
                    CREATE INDEX idx_raw_events_created_at ON raw_events(created_at);
                    CREATE INDEX idx_raw_events_processed_at ON raw_events(processed_at);

                    CREATE TABLE event_state (
                        event_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (status IN ('pending', 'dispatched', 'completed', 'failed')),
                        dispatched_at TEXT,
                        dispatch_target TEXT,
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                        FOREIGN KEY(event_id) REFERENCES raw_events(event_id) ON DELETE CASCADE
                    );

                    CREATE INDEX idx_event_state_status ON event_state(status);
                    CREATE INDEX idx_event_state_dispatched_at ON event_state(dispatched_at);

                    CREATE TABLE daemon_checkpoints (
                        endpoint TEXT PRIMARY KEY,
                        last_event_id TEXT,
                        etag TEXT,
                        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    );
                    """
                )
                self._connection.execute("INSERT INTO schema_versions(version) VALUES (2)")
                current_version = 2

            if current_version < 2:
                self._connection.execute("ALTER TABLE event_state ADD COLUMN dispatch_target TEXT")
                self._connection.execute("INSERT INTO schema_versions(version) VALUES (2)")

            if current_version >= 2:
                return

    def _update_state(
        self,
        event_id: str,
        *,
        status: str,
        dispatched_at: bool,
        dispatch_target: str | None = None,
    ) -> None:
        with self._connection:
            if dispatched_at:
                cursor = self._connection.execute(
                    """
                    UPDATE event_state
                    SET
                        status = ?,
                        dispatched_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                        dispatch_target = ?,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ?
                    """,
                    (status, dispatch_target, event_id),
                )
            else:
                cursor = self._connection.execute(
                    """
                    UPDATE event_state
                    SET
                        status = ?,
                        dispatched_at = NULL,
                        dispatch_target = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                    WHERE event_id = ?
                    """,
                    (status, event_id),
                )
        if cursor.rowcount == 0:
            raise KeyError(f"Unknown event_id: {event_id}")

    @staticmethod
    def _logged_event_from_row(row: sqlite3.Row) -> LoggedEvent:
        return LoggedEvent(
            event_id=row["event_id"],
            event_type=row["event_type"],
            repo=row["repo"],
            created_at=row["created_at"],
            processed_at=row["processed_at"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            dispatched_at=row["dispatched_at"],
        )

    @staticmethod
    def _string_or_none(value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _format_timestamp(value: float) -> str:
        return datetime.fromtimestamp(value, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
