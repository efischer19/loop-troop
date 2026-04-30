"""Ghost Run CLI — replay a GitHub issue against a different Ollama model.

This module provides the ``loop-troop replay`` subcommand.  It injects a
synthetic ``loop: ready`` event directly into the local SQLite shadow log,
bypassing the GitHub polling client.  The standard sync daemon picks the
event up on the next tick and processes it through the normal Coder →
Reviewer pipeline with the following modifications:

* The branch name includes a slugified form of the model name (e.g.
  ``loop/issue-42-qwen2.5-coder-32b-item-1``) to avoid collisions with the
  primary implementation branch.
* The PR is opened as a **Draft** with a ``[BAKE-OFF]`` title prefix so the
  Reviewer knows not to merge it automatically.
* The ``ghost_run`` flag in the event payload signals all downstream workers
  to behave in bake-off mode.

Usage::

    loop-troop replay --issue 42 --model qwen2.5-coder:32b --repo owner/repo
    loop-troop replay --issue 42 --model qwen2.5-coder:32b --repo owner/repo --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from loop_troop.core.llm_client import DEFAULT_OLLAMA_HOST
from loop_troop.shadow_log import ShadowLog


def build_synthetic_event(
    *,
    issue_number: int,
    repo: str,
    model: str,
) -> dict[str, Any]:
    """Return a synthetic ghost-run event payload suitable for SQLite injection.

    The event mimics a GitHub ``labeled`` webhook with a ``loop: ready`` label
    and carries two extra fields that the daemon uses to activate bake-off mode:

    * ``ghost_run`` — boolean flag consumed by the dispatcher.
    * ``ghost_model`` — the Ollama model name the Coder should use.
    """
    event_id = f"ghost-{issue_number}-{uuid.uuid4().hex[:8]}"
    return {
        "id": event_id,
        "event": "labeled",
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issue": {
            "number": issue_number,
        },
        "label": {"name": "loop: ready"},
        "ghost_run": True,
        "ghost_model": model,
    }


def validate_model(model: str, *, ollama_host: str = DEFAULT_OLLAMA_HOST) -> bool:
    """Return ``True`` if *model* is available on the local Ollama instance.

    Makes a single ``GET /api/tags`` request.  Returns ``False`` on any
    network error so the caller can surface a user-friendly message instead
    of an unhandled exception.
    """
    try:
        response = httpx.get(f"{ollama_host}/api/tags", timeout=5.0)
        response.raise_for_status()
        models = response.json().get("models", [])
        return any(m.get("name") == model for m in models)
    except httpx.HTTPError:
        return False


def inject_ghost_run(
    *,
    issue_number: int,
    repo: str,
    model: str,
    db_path: str | None = None,
    dry_run: bool = False,
    ollama_host: str = DEFAULT_OLLAMA_HOST,
) -> dict[str, Any]:
    """Validate *model* and inject a synthetic ghost-run event into the shadow log.

    Returns the synthetic event payload regardless of *dry_run*.

    When *dry_run* is ``True`` the shadow log database is **not** modified —
    the return value can be printed for inspection.

    Raises :exc:`ValueError` when *model* is not available on Ollama.
    """
    if not validate_model(model, ollama_host=ollama_host):
        raise ValueError(
            f"Model '{model}' is not available on Ollama at {ollama_host}. "
            "Run `ollama pull <model>` or check `ollama list`."
        )

    event = build_synthetic_event(issue_number=issue_number, repo=repo, model=model)

    if not dry_run:
        with ShadowLog(db_path) as shadow_log:
            shadow_log.log_event(event, repo=repo, default_event_type="labeled")

    return event


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-troop",
        description="Loop Troop CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser(
        "replay",
        help="Replay a GitHub issue against a different Ollama model (Ghost Run).",
    )
    replay_parser.add_argument(
        "--issue",
        type=int,
        required=True,
        metavar="NUMBER",
        help="GitHub issue number to replay.",
    )
    replay_parser.add_argument(
        "--model",
        required=True,
        metavar="MODEL",
        help="Ollama model name to use (e.g. qwen2.5-coder:32b).",
    )
    replay_parser.add_argument(
        "--repo",
        metavar="OWNER/REPO",
        help="Target repository in owner/repo format. Defaults to LOOP_TROOP_REPO.",
    )
    replay_parser.add_argument(
        "--db-path",
        metavar="PATH",
        help="Path to the SQLite shadow log. Defaults to ~/.loop-troop/shadow.db.",
    )
    replay_parser.add_argument(
        "--ollama-host",
        default=DEFAULT_OLLAMA_HOST,
        metavar="URL",
        help="Ollama base URL.",
    )
    replay_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the event that would be injected without writing to SQLite.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.command == "replay":
        repo = args.repo or os.environ.get("LOOP_TROOP_REPO")
        if not repo:
            print(
                "Error: --repo or LOOP_TROOP_REPO environment variable is required.",
                file=sys.stderr,
            )
            return 1

        try:
            event = inject_ghost_run(
                issue_number=args.issue,
                repo=repo,
                model=args.model,
                db_path=args.db_path,
                dry_run=args.dry_run,
                ollama_host=args.ollama_host,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            print("DRY RUN — would inject the following event:")
            print(json.dumps(event, indent=2))
        else:
            print(f"Ghost run injected for issue #{args.issue} with model '{args.model}'.")
            print(f"Event ID: {event['id']}")
            print("The daemon will pick up this event on the next poll cycle.")

        return 0

    return 1
