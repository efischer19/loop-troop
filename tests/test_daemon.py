import argparse
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from loop_troop.core.github_client import GitHubClient
from loop_troop.core.github_client import GitHubIssue, GitHubLabel
from loop_troop.daemon import DaemonConfig, ShadowLogETagStore, SyncDaemon
from loop_troop.dispatcher import DispatchClassification, DispatchRoute, Dispatcher, WorkflowLabel
from loop_troop.shadow_log import ShadowLog


class FakeClassifier:
    def classify(self, **kwargs) -> DispatchClassification:
        return DispatchClassification(
            route=kwargs["expected_route"],
            model_name="qwen2.5-coder:32b",
            reasoning="Test classifier route.",
        )


@pytest.mark.asyncio
async def test_sync_daemon_runs_pipeline_and_recovers_zombies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    db_path = tmp_path / "shadow.db"
    initial_shadow_log = ShadowLog(db_path)
    initial_shadow_log.log_event(
        {"id": 200, "event": "labeled", "issue": {"number": 8}},
        repo="octo/repo",
    )
    initial_shadow_log.mark_dispatched(200, dispatch_target="t2:qwen-stale")
    stale_at = datetime.now(UTC) - timedelta(minutes=30)
    initial_shadow_log._connection.execute(
        "UPDATE event_state SET dispatched_at = ? WHERE event_id = ?",
        (stale_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "200"),
    )
    initial_shadow_log._connection.commit()
    initial_shadow_log.close()

    label_updates: list[tuple[str, list[str]]] = []
    daemon_shadow_log = ShadowLog(db_path)

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat", "id": 1})
        if request.url.path == "/repos/octo/repo/issues/events":
            if request.headers.get("If-None-Match") == '"events-etag"':
                return httpx.Response(304)
            return httpx.Response(
                200,
                headers={"ETag": '"events-etag"'},
                json=[
                    {
                        "id": 201,
                        "event": "labeled",
                        "created_at": "2026-04-28T13:00:00Z",
                        "issue": {"number": 7},
                    }
                ],
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "PUT" and request.url.path.endswith("/labels"):
            label_updates.append((request.url.path, json.loads(request.content.decode())["labels"]))
            return httpx.Response(200, json=[{"name": WorkflowLabel.READY.value}])
        if request.url.path.endswith("/issues/7"):
            return httpx.Response(
                200,
                json=GitHubIssue(
                    number=7,
                    state="open",
                    title="Fresh event",
                    labels=[GitHubLabel(name=WorkflowLabel.READY.value)],
                ).model_dump(),
            )
        if request.url.path.endswith("/issues/8"):
            return httpx.Response(
                200,
                json=GitHubIssue(
                    number=8,
                    state="open",
                    title="Recovered zombie",
                    labels=[GitHubLabel(name=WorkflowLabel.READY.value)],
                ).model_dump(),
            )
        raise AssertionError(f"Unexpected GitHub request: {request.method} {request.url}")

    github_client = GitHubClient(
        base_url="https://api.github.com",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(github_handler),
            base_url="https://api.github.com",
        ),
        etag_store=ShadowLogETagStore(daemon_shadow_log),
        shadow_log=daemon_shadow_log,
    )
    dispatcher = Dispatcher(
        shadow_log=daemon_shadow_log,
        github_client=github_client,
        classifier=FakeClassifier(),
    )
    daemon = SyncDaemon(
        config=DaemonConfig(
            repo="octo/repo",
            db_path=str(db_path),
            ollama_host="http://ollama.test",
            poll_interval_seconds=0.01,
            zombie_sweep_interval_seconds=0.01,
            zombie_timeout_seconds=60.0,
        ),
        github_client=github_client,
        shadow_log=daemon_shadow_log,
        dispatcher=dispatcher,
        ollama_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": []})
            if request.url.path == "/api/tags"
            else httpx.Response(404)
        ),
    )

    caplog.set_level(logging.WARNING, logger="loop_troop.daemon")

    await daemon.run(max_cycles=2)

    reopened = ShadowLog(db_path)
    try:
        assert reopened.get_pending_events() == []
        statuses = dict(
            reopened._connection.execute(
                "SELECT event_id, status FROM event_state ORDER BY event_id"
            ).fetchall()
        )
        assert statuses == {"200": "dispatched", "201": "dispatched"}
        checkpoint = reopened.get_checkpoint("repos/octo/repo/issues/events")
        assert checkpoint is not None
        assert checkpoint.last_event_id == "201"
        assert checkpoint.etag == '"events-etag"'
    finally:
        reopened.close()

    assert len(label_updates) == 2
    assert all(labels == [WorkflowLabel.READY.value] for _, labels in label_updates)
    warning_record = next(record for record in caplog.records if record.message == "Reset stale dispatched event")
    assert warning_record.structured_data["event_id"] == "200"
    assert warning_record.structured_data["dispatch_target"] == "t2:qwen-stale"


def test_daemon_config_reads_toml_and_env_overrides(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "loop-troop.toml"
    config_path.write_text(
        "\n".join(
            [
                "[github]",
                'repo = "octo/from-file"',
                "",
                "[daemon]",
                "poll_interval_seconds = 45",
                "",
                "[logging]",
                'level = "WARNING"',
            ]
        )
    )
    monkeypatch.setenv("LOOP_TROOP_REPO", "octo/from-env")
    monkeypatch.setenv("LOOP_TROOP_POLL_INTERVAL", "15")

    config = DaemonConfig.from_sources(args=argparse.Namespace(config=str(config_path), dry_run=True))

    assert config.repo == "octo/from-env"
    assert config.poll_interval_seconds == 15
    assert config.log_level == "WARNING"
    assert config.dry_run is True
