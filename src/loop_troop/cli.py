"""Top-level Loop Troop CLI."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any
from collections.abc import Callable

import httpx

from loop_troop.config import Config, DEFAULT_OLLAMA_HOST
from loop_troop.core.schemas import (
    DispatchDecision,
    DispatchLabelAction,
    EventType,
    LabelActionType,
    TargetExecutionProfile,
)
from loop_troop.dispatcher import WorkflowLabel
from loop_troop.execution import WorkerTier
from loop_troop.shadow_log import ShadowLog


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="loop-troop", description="Loop Troop developer CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay = subparsers.add_parser("replay", help="Inject a synthetic ghost-run event.")
    replay.add_argument("--issue", type=int, required=True, help="GitHub issue number to replay.")
    replay.add_argument("--model", required=True, help="Ollama model name to use for the replay.")
    replay.add_argument("--config", help="Path to a TOML config file.")
    replay.add_argument(
        "--ollama-host",
        default=None,
        help=f"Ollama host base URL (defaults to {DEFAULT_OLLAMA_HOST!r} or config/env).",
    )
    replay.add_argument("--dry-run", action="store_true", help="Show the synthetic event without writing it.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "replay":
        run_replay(args)
        return 0
    raise ValueError(f"Unknown command: {args.command}")


def run_replay(
    args: argparse.Namespace,
    *,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> dict[str, Any]:
    config = Config.from_sources(config_path=args.config, require_repo=True)
    ollama_host = (args.ollama_host or config.ollama_host or DEFAULT_OLLAMA_HOST).rstrip("/")
    _validate_model_available(args.model, ollama_host=ollama_host, client_factory=client_factory)

    event_id = _replay_event_id(issue_number=args.issue, model_name=args.model)
    dispatch_decision = DispatchDecision(
        event_id=event_id,
        event_type=EventType.LABELED,
        target_profile=TargetExecutionProfile(
            tier=WorkerTier.T2,
            model_name=args.model,
            reasoning="Ghost-run replay requested from the CLI.",
        ),
        label_action=DispatchLabelAction(
            action=LabelActionType.ADD,
            label_name=WorkflowLabel.READY.value,
        ),
        reasoning="Synthetic loop: ready event injected for a local ghost run.",
        bake_off=True,
        ghost_run=True,
    )

    payload = {
        "repo": config.repo,
        "issue_number": args.issue,
        "event_id": event_id,
        "dispatch_decision": dispatch_decision.model_dump(mode="json"),
    }
    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return payload

    with ShadowLog(config.db_path) as shadow_log:
        shadow_log.inject_replay_event(
            repo=config.repo,
            event_id=event_id,
            issue_number=args.issue,
            dispatch_decision=dispatch_decision,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _validate_model_available(
    model_name: str,
    *,
    ollama_host: str,
    client_factory: Callable[..., httpx.Client],
) -> None:
    with client_factory(base_url=ollama_host, timeout=5.0) as client:
        response = client.get("/api/tags")
        response.raise_for_status()
    payload = response.json()
    models = payload.get("models", []) if isinstance(payload, dict) else []
    available = {item.get("name") for item in models if isinstance(item, dict) and item.get("name")}
    if model_name not in available:
        raise ValueError(f"Ollama model {model_name!r} is not available at {ollama_host}.")


def _replay_event_id(*, issue_number: int, model_name: str, now: datetime | None = None) -> str:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return f"ghost-run:{issue_number}:{model_name}:{timestamp}"
