from pathlib import Path

import pytest

from loop_troop.config import AuthMode, Config


def _clear_loop_troop_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "LOOP_TROOP_REPO",
        "LOOP_TROOP_REPO_PATH",
        "LOOP_TROOP_DB_PATH",
        "LOOP_TROOP_GITHUB_BASE_URL",
        "GITHUB_PAT",
        "LOOP_TROOP_APP_ID",
        "LOOP_TROOP_APP_PRIVATE_KEY_PATH",
        "LOOP_TROOP_APP_INSTALLATION_ID",
        "OLLAMA_HOST",
        "OLLAMA_API_KEY",
        "LOOP_TROOP_POLL_INTERVAL",
        "LOOP_TROOP_ZOMBIE_SWEEP_INTERVAL",
        "LOOP_TROOP_ZOMBIE_TIMEOUT",
        "LOOP_TROOP_LOG_LEVEL",
        "LOOP_TROOP_T1_MODEL",
        "LOOP_TROOP_T2_MODEL",
        "LOOP_TROOP_T3_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_config_loads_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_loop_troop_env(monkeypatch)
    monkeypatch.setenv("LOOP_TROOP_REPO", "octo/env-repo")
    monkeypatch.setenv("LOOP_TROOP_REPO_PATH", "/tmp/workspace")
    monkeypatch.setenv("LOOP_TROOP_DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setenv("GITHUB_PAT", "github_pat_test_token")
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.env:11434")
    monkeypatch.setenv("LOOP_TROOP_POLL_INTERVAL", "15")
    monkeypatch.setenv("LOOP_TROOP_T1_MODEL", "qwen:7b")

    config = Config.from_sources(require_repo=True, require_auth=True)

    assert config.repo == "octo/env-repo"
    assert config.repo_path == "/tmp/workspace"
    assert config.db_path == str(tmp_path / "shadow.db")
    assert config.github_pat_value == "github_pat_test_token"
    assert config.ollama_host == "http://ollama.env:11434"
    assert config.poll_interval_seconds == 15
    assert config.t1_model == "qwen:7b"
    assert config.auth_mode is AuthMode.PAT


def test_config_loads_from_toml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_loop_troop_env(monkeypatch)
    key_path = tmp_path / "loop-troop-app.pem"
    key_path.write_text("-----BEGIN PRIVATE KEY-----\nplaceholder\n-----END PRIVATE KEY-----\n")
    config_path = tmp_path / "loop-troop.toml"
    config_path.write_text(
        "\n".join(
            [
                "[github]",
                'repo = "octo/file-repo"',
                "",
                "[github.app]",
                "id = 123",
                f'private_key_path = "{key_path}"',
                "installation_id = 456",
                "",
                "[shadow_log]",
                'db_path = "/tmp/file-shadow.db"',
                "",
                "[ollama]",
                'host = "http://ollama.file:11434"',
                "",
                "[models]",
                't2 = "qwen:14b"',
                "",
                "[daemon]",
                "poll_interval_seconds = 45",
                "",
                "[logging]",
                'level = "WARNING"',
            ]
        )
    )

    config = Config.from_sources(config_path=config_path, require_repo=True, require_auth=True)

    assert config.repo == "octo/file-repo"
    assert config.github_app_id == 123
    assert config.github_app_installation_id == 456
    assert config.github_app_private_key_file == key_path
    assert config.db_path == "/tmp/file-shadow.db"
    assert config.ollama_host == "http://ollama.file:11434"
    assert config.t2_model == "qwen:14b"
    assert config.poll_interval_seconds == 45
    assert config.log_level == "WARNING"
    assert config.auth_mode is AuthMode.GITHUB_APP


def test_config_validation_errors_when_repo_and_auth_are_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_loop_troop_env(monkeypatch)

    with pytest.raises(ValueError, match="Configure LOOP_TROOP_REPO or \\[github\\]\\.repo"):
        Config.from_sources(require_repo=True, require_auth=True)


@pytest.mark.parametrize(
    ("env_values", "expected_mode"),
    [
        ({"GITHUB_PAT": "github_pat_example"}, AuthMode.PAT),
        (
            {
                "LOOP_TROOP_APP_ID": "321",
                "LOOP_TROOP_APP_PRIVATE_KEY_PATH": "__KEY_PATH__",
                "LOOP_TROOP_APP_INSTALLATION_ID": "654",
            },
            AuthMode.GITHUB_APP,
        ),
    ],
)
def test_config_detects_auth_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_values: dict[str, str],
    expected_mode: AuthMode,
) -> None:
    _clear_loop_troop_env(monkeypatch)
    monkeypatch.setenv("LOOP_TROOP_REPO", "octo/repo")
    key_path = tmp_path / "app.pem"
    key_path.write_text("placeholder")

    for key, value in env_values.items():
        monkeypatch.setenv(key, str(key_path) if value == "__KEY_PATH__" else value)

    config = Config.from_sources(require_repo=True, require_auth=True)

    assert config.auth_mode is expected_mode
