"""Async daemon entrypoint for polling and dispatching Loop Troop events."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

import httpx

from loop_troop.architect import ArchitectWorker
from loop_troop.coder import CoderWorker
from loop_troop.config import Config, DaemonConfig
from loop_troop.core.github_client import GitHubClient, GitHubIssueEvent
from loop_troop.dispatcher import Dispatcher, OllamaDispatcherClassifier, WorkflowLabel
from loop_troop.reviewer import ReviewerWorker
from loop_troop.shadow_log import Checkpoint, ShadowLog


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
        }
        structured_data = getattr(record, "structured_data", None)
        if isinstance(structured_data, dict):
            payload.update(structured_data)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())


def log_structured(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"structured_data": fields})

class ShadowLogETagStore:
    def __init__(self, shadow_log: ShadowLog) -> None:
        self._shadow_log = shadow_log

    def get(self, key: str) -> str | None:
        checkpoint = self._shadow_log.get_checkpoint(key)
        return checkpoint.etag if checkpoint else None

    def set(self, key: str, value: str) -> None:
        checkpoint = self._shadow_log.get_checkpoint(key)
        self._shadow_log.set_checkpoint(
            key,
            last_event_id=checkpoint.last_event_id if checkpoint else None,
            etag=value,
        )


class SyncDaemon:
    def __init__(
        self,
        *,
        config: DaemonConfig,
        github_client: GitHubClient,
        shadow_log: ShadowLog,
        dispatcher: Dispatcher,
        architect_worker: ArchitectWorker | None = None,
        coder_worker: CoderWorker | None = None,
        reviewer_worker: ReviewerWorker | None = None,
        ollama_transport: httpx.AsyncBaseTransport | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._github_client = github_client
        self._shadow_log = shadow_log
        self._dispatcher = dispatcher
        self._architect_worker = architect_worker or ArchitectWorker(github_client=github_client)
        self._coder_worker = coder_worker or CoderWorker(github_client=github_client)
        self._reviewer_worker = reviewer_worker or ReviewerWorker(github_client=github_client)
        self._ollama_transport = ollama_transport
        self._logger = logger or logging.getLogger("loop_troop.daemon")
        self._repo_path = Path(config.repo_path or os.getcwd())
        self._stop_event = asyncio.Event()

    async def run(self, *, max_cycles: int | None = None) -> None:
        self._install_signal_handlers()
        await self.startup_self_check()
        zombie_task = asyncio.create_task(self._zombie_sweep_loop())
        completed_cycles = 0
        try:
            while not self._stop_event.is_set():
                await self.run_cycle()
                completed_cycles += 1
                if max_cycles is not None and completed_cycles >= max_cycles:
                    self._stop_event.set()
                    break
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
        finally:
            zombie_task.cancel()
            try:
                await zombie_task
            except asyncio.CancelledError:
                pass
            await self._github_client.aclose()
            self._shadow_log.close()
            log_structured(self._logger, logging.INFO, "Daemon stopped cleanly")

    async def startup_self_check(self) -> None:
        user = await self._github_client.get_authenticated_user()
        async with httpx.AsyncClient(
            base_url=self._config.ollama_host,
            timeout=5.0,
            transport=self._ollama_transport,
        ) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
        self._shadow_log.verify_writable()
        log_structured(
            self._logger,
            logging.INFO,
            "Startup self-check completed",
            github_user=user.login,
            ollama_host=self._config.ollama_host,
            db_path=str(self._shadow_log.db_path),
        )

    async def run_cycle(self) -> None:
        owner, repo = _split_repo(self._config.repo)
        issue_events_response = await self._github_client.poll_issue_events(owner, repo)
        pull_requests_response = await self._github_client.poll_pull_requests(owner, repo)
        self._update_checkpoint(_issue_events_checkpoint_key(owner, repo), issue_events_response.items, issue_events_response.etag)
        self._update_checkpoint(_pulls_checkpoint_key(owner, repo), pull_requests_response.items, pull_requests_response.etag)
        log_structured(
            self._logger,
            logging.INFO,
            "Poll cycle completed",
            repo=self._config.repo,
            issue_events_polled=len(issue_events_response.items),
            issue_events_not_modified=issue_events_response.not_modified,
            pull_requests_polled=len(pull_requests_response.items),
            pull_requests_not_modified=pull_requests_response.not_modified,
            dry_run=self._config.dry_run,
        )

        if self._config.dry_run:
            log_structured(
                self._logger,
                logging.INFO,
                "Dry-run mode skipped dispatcher",
                pending_events=len(self._shadow_log.get_pending_events()),
            )
            return

        outcomes = await self._dispatcher.dispatch_pending_events()
        await self._run_dispatched_workers(outcomes)
        log_structured(
            self._logger,
            logging.INFO,
            "Dispatch cycle completed",
            outcomes=len(outcomes),
            dispatched=sum(1 for outcome in outcomes if outcome.status == "dispatched"),
            failed=sum(1 for outcome in outcomes if outcome.status == "failed"),
            blocked=sum(1 for outcome in outcomes if outcome.status == "blocked"),
            skipped=sum(1 for outcome in outcomes if outcome.status == "skipped"),
        )

    async def _run_dispatched_workers(self, outcomes: list[Any]) -> None:
        for outcome in outcomes:
            if outcome.status != "dispatched" or outcome.decision is None:
                continue
            label_name = outcome.decision.label_action.label_name
            if label_name not in {
                WorkflowLabel.NEEDS_PLANNING.value,
                WorkflowLabel.FEATURE.value,
                WorkflowLabel.READY.value,
                WorkflowLabel.NEEDS_REVIEW.value,
            }:
                continue

            event = self._shadow_log.get_event(outcome.event_id)
            if event is None:
                continue

            owner, repo = _split_repo(event.repo)
            issue_number = self._issue_number_from_event(event)
            try:
                if label_name in {
                    WorkflowLabel.NEEDS_PLANNING.value,
                    WorkflowLabel.FEATURE.value,
                }:
                    await self._architect_worker.handle_issue(
                        owner=owner,
                        repo=repo,
                        issue_number=issue_number,
                        repo_path=self._repo_path,
                    )
                elif label_name == WorkflowLabel.READY.value:
                    await self._coder_worker.handle_issue(
                        owner=owner,
                        repo=repo,
                        issue_number=issue_number,
                        repo_path=self._repo_path,
                        target_execution_profile=outcome.decision.target_profile,
                        ghost_run=outcome.decision.ghost_run,
                    )
                else:
                    await self._reviewer_worker.handle_pull_request(
                        owner=owner,
                        repo=repo,
                        pull_number=issue_number,
                        repo_path=self._repo_path,
                    )
            except Exception as exc:
                self._shadow_log.mark_failed(event.event_id, error_details=str(exc))
                log_structured(
                    self._logger,
                    logging.ERROR,
                    "Worker execution failed",
                    event_id=event.event_id,
                    issue_number=issue_number,
                    label_name=label_name,
                    error=str(exc),
                )
                continue

            self._shadow_log.mark_completed(event.event_id)
            log_structured(
                self._logger,
                logging.INFO,
                "Worker execution completed",
                event_id=event.event_id,
                issue_number=issue_number,
                label_name=label_name,
            )

    async def _zombie_sweep_loop(self) -> None:
        while not self._stop_event.is_set():
            for swept_event in self._shadow_log.sweep_dispatched_events(
                timeout_seconds=self._config.zombie_timeout_seconds
            ):
                log_structured(
                    self._logger,
                    logging.WARNING,
                    "Reset stale dispatched event",
                    event_id=swept_event.event_id,
                    repo=swept_event.repo,
                    stuck_seconds=round(swept_event.stuck_seconds, 3),
                    dispatch_target=swept_event.dispatch_target,
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._config.zombie_sweep_interval_seconds,
                )
            except TimeoutError:
                continue

    def request_shutdown(self, source: str) -> None:
        if self._stop_event.is_set():
            return
        log_structured(self._logger, logging.INFO, "Shutdown requested", source=source)
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown, signum.name)
            except NotImplementedError:
                continue

    def _update_checkpoint(
        self,
        checkpoint_key: str,
        events: list[GitHubIssueEvent] | list[Any],
        etag: str | None,
    ) -> None:
        current: Checkpoint | None = self._shadow_log.get_checkpoint(checkpoint_key)
        last_event_id = current.last_event_id if current else None
        if events:
            last_event_id = str(max(event.id for event in events))
        if etag is None and current is None and last_event_id is None:
            return
        self._shadow_log.set_checkpoint(checkpoint_key, last_event_id=last_event_id, etag=etag)

    @staticmethod
    def _issue_number_from_event(event) -> int:
        issue = event.payload.get("issue")
        if isinstance(issue, dict) and issue.get("number") is not None:
            return int(issue["number"])
        pull_request = event.payload.get("pull_request")
        if isinstance(pull_request, dict) and pull_request.get("number") is not None:
            return int(pull_request["number"])
        if event.payload.get("number") is not None:
            return int(event.payload["number"])
        raise ValueError(f"Unable to determine issue number from event {event.event_id}.")


async def run_daemon(
    config: Config,
    *,
    ollama_transport: httpx.AsyncBaseTransport | None = None,
    max_cycles: int | None = None,
) -> None:
    daemon_config = config.to_daemon_config()
    shadow_log = ShadowLog(daemon_config.db_path)
    github_client = GitHubClient(
        config=config,
        base_url=daemon_config.github_base_url,
        poll_interval_seconds=daemon_config.poll_interval_seconds,
        etag_store=ShadowLogETagStore(shadow_log),
        shadow_log=shadow_log,
    )
    dispatcher = Dispatcher(
        shadow_log=shadow_log,
        github_client=github_client,
        classifier=OllamaDispatcherClassifier(),
    )
    daemon = SyncDaemon(
        config=daemon_config,
        github_client=github_client,
        shadow_log=shadow_log,
        dispatcher=dispatcher,
        ollama_transport=ollama_transport,
    )
    await daemon.run(max_cycles=max_cycles)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Loop Troop sync daemon.")
    parser.add_argument("--config", help="Path to a TOML config file.")
    parser.add_argument("--dry-run", action="store_true", help="Poll and log events without dispatching work.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = Config.from_sources(
        config_path=args.config,
        dry_run=bool(args.dry_run),
        require_repo=True,
        require_auth=True,
    )
    configure_logging(config.log_level)
    asyncio.run(run_daemon(config))
    return 0


def _split_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def _checkpoint_key(owner: str, repo: str) -> str:
    return f"repos/{owner}/{repo}/issues/events"


def _issue_events_checkpoint_key(owner: str, repo: str) -> str:
    return _checkpoint_key(owner, repo)


def _pulls_checkpoint_key(owner: str, repo: str) -> str:
    return f"repos/{owner}/{repo}/pulls"
