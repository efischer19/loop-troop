from datetime import UTC, datetime, timedelta

import pytest

from loop_troop.shadow_log import ShadowLog


def test_shadow_log_uses_env_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "shadow.db"
    monkeypatch.setenv("LOOP_TROOP_DB_PATH", str(db_path))

    shadow_log = ShadowLog()

    try:
        assert shadow_log.db_path == db_path
        assert db_path.exists()
    finally:
        shadow_log.close()


def test_log_event_deduplicates_and_creates_pending_state(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")

    try:
        event = {"id": 101, "event": "opened", "created_at": "2026-04-26T00:00:00Z"}

        assert shadow_log.log_event(event, repo="octo/repo") is True
        assert shadow_log.log_event(event, repo="octo/repo") is False

        pending = shadow_log.get_pending_events()
        assert [item.event_id for item in pending] == ["101"]
        assert pending[0].status == "pending"

        raw_count = shadow_log._connection.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        state_count = shadow_log._connection.execute("SELECT COUNT(*) FROM event_state").fetchone()[0]
        assert raw_count == 1
        assert state_count == 1
    finally:
        shadow_log.close()


def test_state_transitions_record_dispatched_at(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")

    try:
        shadow_log.log_event({"id": 202, "event": "closed"}, repo="octo/repo")

        shadow_log.mark_dispatched(202, dispatch_target="t2:qwen")
        dispatched_at, dispatch_target = shadow_log._connection.execute(
            "SELECT dispatched_at, dispatch_target FROM event_state WHERE event_id = ?",
            ("202",),
        ).fetchone()
        assert dispatched_at is not None
        assert dispatch_target == "t2:qwen"

        shadow_log.mark_completed(202)
        status, completed_dispatched_at, completed_target = shadow_log._connection.execute(
            "SELECT status, dispatched_at, dispatch_target FROM event_state WHERE event_id = ?",
            ("202",),
        ).fetchone()
        assert status == "completed"
        assert completed_dispatched_at is None
        assert completed_target is None

        shadow_log.mark_failed(202, error_details="worker crashed")
        status, failed_dispatched_at, failed_target, failed_error_details = shadow_log._connection.execute(
            "SELECT status, dispatched_at, dispatch_target, error_details FROM event_state WHERE event_id = ?",
            ("202",),
        ).fetchone()
        assert status == "failed"
        assert failed_dispatched_at is None
        assert failed_target is None
        assert failed_error_details == "worker crashed"
    finally:
        shadow_log.close()


def test_checkpoint_persists_across_restarts(tmp_path) -> None:
    db_path = tmp_path / "shadow.db"
    first = ShadowLog(db_path)
    try:
        first.set_checkpoint("repos/octo/repo/issues/events", last_event_id=88, etag='"etag-1"')
    finally:
        first.close()

    second = ShadowLog(db_path)
    try:
        checkpoint = second.get_checkpoint("repos/octo/repo/issues/events")
        assert checkpoint is not None
        assert checkpoint.last_event_id == "88"
        assert checkpoint.etag == '"etag-1"'
    finally:
        second.close()


def test_shadow_log_creates_versioned_schema(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")

    try:
        tables = {
            row[0]
            for row in shadow_log._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"schema_versions", "raw_events", "event_state", "daemon_checkpoints", "llm_metrics"} <= tables
        version = shadow_log._connection.execute(
            "SELECT MAX(version) FROM schema_versions"
        ).fetchone()[0]
        assert version == 4
    finally:
        shadow_log.close()


def test_shadow_log_sweeps_stale_dispatched_events(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")

    try:
        shadow_log.log_event({"id": 303, "event": "labeled", "issue": {"number": 33}}, repo="octo/repo")
        shadow_log.mark_dispatched(303, dispatch_target="t2:qwen")
        stale_at = datetime.now(UTC) - timedelta(minutes=20)
        shadow_log._connection.execute(
            "UPDATE event_state SET dispatched_at = ? WHERE event_id = ?",
            (stale_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "303"),
        )
        shadow_log._connection.commit()

        swept = shadow_log.sweep_dispatched_events(timeout_seconds=15 * 60)

        assert [event.event_id for event in swept] == ["303"]
        assert swept[0].dispatch_target == "t2:qwen"
        status, dispatched_at, dispatch_target = shadow_log._connection.execute(
            "SELECT status, dispatched_at, dispatch_target FROM event_state WHERE event_id = ?",
            ("303",),
        ).fetchone()
        assert status == "pending"
        assert dispatched_at is None
        assert dispatch_target is None
    finally:
        shadow_log.close()
