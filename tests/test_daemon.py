import argparse
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from loop_troop.architect import ArchitectWorker
from loop_troop.core.github_client import GitHubClient
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel
from loop_troop.core.schemas import (
    ArchitectPlan,
    ChecklistItem,
    FeaturePlan,
    ReviewVerdict,
    ReviewVerdictType,
    SubIssue,
)
from loop_troop.daemon import DaemonConfig, ShadowLogETagStore, SyncDaemon
from loop_troop.dispatcher import DispatchClassification, DispatchRoute, Dispatcher, WorkflowLabel
from loop_troop.reviewer import ReviewerWorker
from loop_troop.shadow_log import ShadowLog


class FakeClassifier:
    def classify(self, **kwargs) -> DispatchClassification:
        return DispatchClassification(
            route=kwargs["expected_route"],
            model_name="qwen2.5-coder:32b",
            reasoning="Test classifier route.",
        )


class FakeStructuredLLMClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)

    def complete_structured(self, **_kwargs):
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FailingArchitectWorker:
    async def handle_issue(self, **_kwargs) -> None:
        raise RuntimeError("architect worker exploded")


class FailingReviewerWorker:
    async def handle_pull_request(self, **_kwargs) -> None:
        raise RuntimeError("reviewer worker exploded")


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
        if request.url.path == "/repos/octo/repo/pulls":
            if request.headers.get("If-None-Match") == '"pulls-etag"':
                return httpx.Response(304)
            return httpx.Response(200, headers={"ETag": '"pulls-etag"'}, json=[])
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
        issue_checkpoint = reopened.get_checkpoint("repos/octo/repo/issues/events")
        assert issue_checkpoint is not None
        assert issue_checkpoint.last_event_id == "201"
        assert issue_checkpoint.etag == '"events-etag"'
        pulls_checkpoint = reopened.get_checkpoint("repos/octo/repo/pulls")
        assert pulls_checkpoint is not None
        assert pulls_checkpoint.last_event_id is None
        assert pulls_checkpoint.etag == '"pulls-etag"'
    finally:
        reopened.close()

    assert len(label_updates) == 2
    assert all(labels == [WorkflowLabel.READY.value] for _, labels in label_updates)
    warning_record = next(record for record in caplog.records if record.message == "Reset stale dispatched event")
    assert warning_record.structured_data["event_id"] == "200"
    assert warning_record.structured_data["dispatch_target"] == "t2:qwen-stale"


@pytest.mark.asyncio
async def test_sync_daemon_executes_architect_and_reviewer_lifecycle_with_mocked_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    db_path = tmp_path / "shadow.db"
    daemon_shadow_log = ShadowLog(db_path)

    issue_state: dict[int, GitHubIssue] = {
        1: GitHubIssue(
            number=1,
            state="open",
            title="Plan a feature slice",
            body="Need a micro-plan.",
            labels=[GitHubLabel(name=WorkflowLabel.NEEDS_PLANNING.value)],
        ),
        2: GitHubIssue(
            number=2,
            state="open",
            title="Ship the full feature",
            body="Need macro-planning.",
            labels=[GitHubLabel(name=WorkflowLabel.FEATURE.value)],
        ),
        3: GitHubIssue(
            number=3,
            state="open",
            title="feat: implement planner",
            body="Closes #1",
            labels=[GitHubLabel(name="backend")],
        ),
    }
    issue_comments: dict[int, list[GitHubIssueComment]] = {
        1: [GitHubIssueComment(id=11, body="Please include tests.", user={"login": "octocat"})],
        2: [GitHubIssueComment(id=12, body="Please decompose recursively if needed.", user={"login": "octocat"})],
    }
    pull_request_state = {
        3: {
            "id": 503,
            "number": 3,
            "state": "open",
            "title": "feat: implement planner",
            "body": "Closes #1",
            "labels": [{"name": "backend"}],
            "created_at": "2026-04-30T11:00:00Z",
            "updated_at": "2026-04-30T11:00:00Z",
            "head": {"sha": "abc123", "ref": "feature/review"},
        }
    }
    created_issues: list[dict[str, object]] = []
    created_reviews: list[dict[str, object]] = []
    next_issue_number = 100

    def sync_pull_request_labels() -> None:
        labels = [label.model_dump() for label in issue_state[3].labels]
        pull_request_state[3]["labels"] = labels

    def github_handler(request: httpx.Request) -> httpx.Response:
        nonlocal next_issue_number
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat", "id": 1})
        if request.url.path == "/repos/octo/repo/issues/events":
            return httpx.Response(
                200,
                headers={"ETag": '"events-etag"'},
                json=[
                    {
                        "id": 301,
                        "event": "labeled",
                        "created_at": "2026-04-30T12:00:00Z",
                        "issue": {"number": 1},
                    },
                    {
                        "id": 302,
                        "event": "labeled",
                        "created_at": "2026-04-30T12:01:00Z",
                        "issue": {"number": 2},
                    },
                ],
            )
        if request.url.path == "/repos/octo/repo/pulls":
            return httpx.Response(200, headers={"ETag": '"pulls-etag"'}, json=[pull_request_state[3]])
        if request.url.path.endswith("/commits/abc123/check-runs"):
            return httpx.Response(
                200,
                json={"check_runs": [{"id": 77, "name": "pytest", "status": "completed", "conclusion": "success"}]},
            )
        if request.url.path.endswith("/pulls/3/files"):
            return httpx.Response(
                200,
                json=[
                    {"filename": "src/loop_troop/architect.py"},
                    {"filename": "tests/test_daemon.py"},
                ],
            )
        if request.url.path.endswith("/pulls/3/reviews"):
            payload = json.loads(request.content.decode())
            created_reviews.append(payload)
            return httpx.Response(200, json={"id": 88, "state": payload["event"]})
        if request.url.path.endswith("/pulls/3"):
            if request.headers.get("Accept") == "application/vnd.github.diff":
                return httpx.Response(200, text="diff --git a/src/loop_troop/architect.py b/src/loop_troop/architect.py")
            sync_pull_request_labels()
            return httpx.Response(200, json=pull_request_state[3])
        if request.url.path.endswith("/issues/1/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=[comment.model_dump() for comment in issue_comments[1]])
            payload = json.loads(request.content.decode())
            comment = GitHubIssueComment(id=1000 + len(issue_comments[1]), body=payload["body"])
            issue_comments[1].append(comment)
            return httpx.Response(201, json=comment.model_dump())
        if request.url.path.endswith("/issues/2/comments"):
            if request.method == "GET":
                return httpx.Response(200, json=[comment.model_dump() for comment in issue_comments[2]])
            payload = json.loads(request.content.decode())
            comment = GitHubIssueComment(id=1000 + len(issue_comments[2]), body=payload["body"])
            issue_comments[2].append(comment)
            return httpx.Response(201, json=comment.model_dump())
        if request.url.path.endswith("/issues/1") and request.method == "GET":
            return httpx.Response(200, json=issue_state[1].model_dump())
        if request.url.path.endswith("/issues/2") and request.method == "GET":
            return httpx.Response(200, json=issue_state[2].model_dump())
        if request.url.path.endswith("/issues/3") and request.method == "GET":
            return httpx.Response(200, json=issue_state[3].model_dump())
        if request.method == "PUT" and request.url.path.endswith("/labels"):
            issue_number = int(request.url.path.split("/")[-2])
            payload = json.loads(request.content.decode())
            issue_state[issue_number] = GitHubIssue(
                **{
                    **issue_state[issue_number].model_dump(),
                    "labels": [{"name": label} for label in payload["labels"]],
                }
            )
            if issue_number == 3:
                sync_pull_request_labels()
            return httpx.Response(200, json=[{"name": label} for label in payload["labels"]])
        if request.method == "POST" and request.url.path.endswith("/issues"):
            payload = json.loads(request.content.decode())
            next_issue_number += 1
            created_issue = {
                "number": next_issue_number,
                "state": "open",
                "title": payload["title"],
                "body": payload["body"],
                "labels": [{"name": label} for label in payload.get("labels", [])],
            }
            created_issues.append(created_issue)
            return httpx.Response(201, json=created_issue)
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
    architect_worker = ArchitectWorker(
        github_client=github_client,
        llm_client=FakeStructuredLLMClient(
            [
                ArchitectPlan(
                    issue_number=1,
                    checklist_items=[
                        ChecklistItem(
                            description="Implement the daemon wiring.",
                            files_touched=["src/loop_troop/daemon.py"],
                            logical_steps=["Dispatch the Architect worker", "Persist completion state"],
                            requires_test=True,
                            test_instructions="Add a daemon integration test.",
                        )
                    ],
                    verification_strategy="Run daemon lifecycle tests.",
                ),
                FeaturePlan(
                    epic_issue_number=2,
                    sub_issues=[
                        SubIssue(title="Create child implementation issue", description="Build the worker wiring."),
                        SubIssue(
                            title="Add integration coverage",
                            description="Validate the full recursive lifecycle.",
                            depends_on=[1],
                            is_integration_test=True,
                        ),
                    ],
                ),
            ]
        ),
    )
    reviewer_worker = ReviewerWorker(
        github_client=github_client,
        llm_client=FakeStructuredLLMClient(
            [
                ReviewVerdict(
                    pr_number=3,
                    verdict=ReviewVerdictType.APPROVE,
                )
            ]
        ),
    )
    daemon = SyncDaemon(
        config=DaemonConfig(
            repo="octo/repo",
            db_path=str(db_path),
            repo_path=str(tmp_path),
            ollama_host="http://ollama.test",
            poll_interval_seconds=0.01,
            zombie_sweep_interval_seconds=60.0,
            zombie_timeout_seconds=60.0,
        ),
        github_client=github_client,
        shadow_log=daemon_shadow_log,
        dispatcher=dispatcher,
        architect_worker=architect_worker,
        reviewer_worker=reviewer_worker,
        ollama_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": []})
            if request.url.path == "/api/tags"
            else httpx.Response(404)
        ),
    )

    await daemon.run(max_cycles=1)

    reopened = ShadowLog(db_path)
    try:
        statuses = dict(
            reopened._connection.execute(
                "SELECT event_id, status FROM event_state ORDER BY id ASC"
            ).fetchall()
        )
        pr_event_id = next(event_id for event_id in statuses if event_id.startswith("pull_request:503:"))
        assert statuses["301"] == "completed"
        assert statuses["302"] == "completed"
        assert statuses[pr_event_id] == "completed"
    finally:
        reopened.close()

    assert issue_state[1].labels == [GitHubLabel(name=WorkflowLabel.READY.value)]
    assert "## Architect Plan" in issue_comments[1][-1].body
    assert issue_state[2].labels == [GitHubLabel(name=WorkflowLabel.EPIC_TRACKING.value)]
    assert len(created_issues) == 2
    assert created_issues[0]["labels"] == [{"name": WorkflowLabel.NEEDS_PLANNING.value}]
    assert created_issues[1]["labels"] == [{"name": WorkflowLabel.NEEDS_PLANNING.value}]
    assert "- [ ] #101: Create child implementation issue (Depends on: none)" in issue_comments[2][-1].body
    assert issue_state[3].labels == [GitHubLabel(name="backend"), GitHubLabel(name=WorkflowLabel.APPROVED.value)]
    assert created_reviews[0]["event"] == "APPROVE"


@pytest.mark.asyncio
async def test_sync_daemon_marks_architect_and_reviewer_failures_without_crashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GITHUB_PAT", "test-token")
    db_path = tmp_path / "shadow.db"
    daemon_shadow_log = ShadowLog(db_path)

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat", "id": 1})
        if request.url.path == "/repos/octo/repo/issues/events":
            return httpx.Response(
                200,
                headers={"ETag": '"events-etag"'},
                json=[
                    {
                        "id": 401,
                        "event": "labeled",
                        "created_at": "2026-04-30T12:00:00Z",
                        "issue": {"number": 1},
                    }
                ],
            )
        if request.url.path == "/repos/octo/repo/pulls":
            return httpx.Response(
                200,
                headers={"ETag": '"pulls-etag"'},
                json=[
                    {
                        "id": 601,
                        "number": 3,
                        "state": "open",
                        "title": "feat: failing review",
                        "body": "Closes #1",
                        "labels": [],
                        "created_at": "2026-04-30T12:00:00Z",
                        "updated_at": "2026-04-30T12:00:00Z",
                        "head": {"sha": "abc123", "ref": "feature/review"},
                    }
                ],
            )
        if request.method == "PUT" and request.url.path.endswith("/labels"):
            labels = json.loads(request.content.decode())["labels"]
            return httpx.Response(200, json=[{"name": label} for label in labels])
        if request.url.path.endswith("/issues/1"):
            return httpx.Response(
                200,
                json=GitHubIssue(
                    number=1,
                    state="open",
                    title="Fail architect",
                    labels=[GitHubLabel(name=WorkflowLabel.NEEDS_PLANNING.value)],
                ).model_dump(),
            )
        if request.url.path.endswith("/issues/3"):
            return httpx.Response(
                200,
                json=GitHubIssue(
                    number=3,
                    state="open",
                    title="Fail reviewer",
                    labels=[GitHubLabel(name="backend")],
                ).model_dump(),
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json=[])
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
            repo_path=str(tmp_path),
            ollama_host="http://ollama.test",
            poll_interval_seconds=0.01,
            zombie_sweep_interval_seconds=60.0,
            zombie_timeout_seconds=60.0,
        ),
        github_client=github_client,
        shadow_log=daemon_shadow_log,
        dispatcher=dispatcher,
        architect_worker=FailingArchitectWorker(),
        reviewer_worker=FailingReviewerWorker(),
        ollama_transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": []})
            if request.url.path == "/api/tags"
            else httpx.Response(404)
        ),
    )

    await daemon.run(max_cycles=1)

    reopened = ShadowLog(db_path)
    try:
        statuses = dict(
            reopened._connection.execute(
                "SELECT event_id, status FROM event_state ORDER BY id ASC"
            ).fetchall()
        )
        architect_event = reopened.get_event("401")
        reviewer_event_id = next(event_id for event_id in statuses if event_id.startswith("pull_request:601:"))
        reviewer_event = reopened.get_event(reviewer_event_id)

        assert statuses["401"] == "failed"
        assert architect_event is not None
        assert architect_event.error_details == "architect worker exploded"
        assert statuses[reviewer_event_id] == "failed"
        assert reviewer_event is not None
        assert reviewer_event.error_details == "reviewer worker exploded"
    finally:
        reopened.close()


def test_daemon_config_reads_toml_and_env_overrides(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "loop-troop.toml"
    config_path.write_text(
        "\n".join(
            [
                "[github]",
                'repo = "octo/from-file"',
                "",
                "[workspace]",
                'repo_path = "/tmp/from-file"',
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
    monkeypatch.setenv("LOOP_TROOP_REPO_PATH", "/tmp/from-env")
    monkeypatch.setenv("LOOP_TROOP_POLL_INTERVAL", "15")

    config = DaemonConfig.from_sources(args=argparse.Namespace(config=str(config_path), dry_run=True))

    assert config.repo == "octo/from-env"
    assert config.repo_path == "/tmp/from-env"
    assert config.poll_interval_seconds == 15
    assert config.log_level == "WARNING"
    assert config.dry_run is True
