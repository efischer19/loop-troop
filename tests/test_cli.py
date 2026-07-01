import argparse

import httpx

from loop_troop.cli import parse_args, run_replay
from loop_troop.shadow_log import ShadowLog


def test_parse_args_replay_command() -> None:
    args = parse_args(["replay", "--issue", "42", "--model", "qwen2.5-coder:32b", "--dry-run"])

    assert args.command == "replay"
    assert args.issue == 42
    assert args.model == "qwen2.5-coder:32b"
    assert args.dry_run is True


def test_run_replay_injects_synthetic_ghost_run_event(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("LOOP_TROOP_REPO", "octo/repo")
    monkeypatch.setenv("LOOP_TROOP_DB_PATH", str(tmp_path / "shadow.db"))

    def client_factory(**kwargs):
        return httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"models": [{"name": "qwen2.5-coder:32b"}, {"name": "llama3.2:latest"}]},
                )
            ),
            **kwargs,
        )

    payload = run_replay(
        argparse.Namespace(
            command="replay",
            issue=42,
            model="qwen2.5-coder:32b",
            config=None,
            ollama_host="http://ollama.test",
            dry_run=False,
        ),
        client_factory=client_factory,
    )

    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        pending = shadow_log.get_pending_events()

    assert payload["repo"] == "octo/repo"
    assert [event.event_id for event in pending] == [payload["event_id"]]
    assert pending[0].event_type == "labeled"
    assert pending[0].payload["label"]["name"] == "loop: ready"
    assert pending[0].payload["dispatch_decision"]["ghost_run"] is True
    assert pending[0].payload["dispatch_decision"]["bake_off"] is True
    assert pending[0].payload["dispatch_decision"]["target_profile"]["model_name"] == "qwen2.5-coder:32b"
