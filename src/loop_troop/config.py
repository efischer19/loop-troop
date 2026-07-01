"""Central Loop Troop configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATH = Path("loop-troop.toml")
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_ZOMBIE_SWEEP_INTERVAL_SECONDS = 300.0
DEFAULT_ZOMBIE_TIMEOUT_SECONDS = 900.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_GITHUB_BASE_URL = "https://api.github.com"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_API_KEY = "ollama"
_TIER_MODEL_FIELDS = {
    "T1": "t1_model",
    "T2": "t2_model",
    "T3": "t3_model",
}


class AuthMode(StrEnum):
    PAT = "pat"
    GITHUB_APP = "github_app"


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    repo: str
    db_path: str | None = None
    repo_path: str | None = None
    github_base_url: str = DEFAULT_GITHUB_BASE_URL
    ollama_host: str = DEFAULT_OLLAMA_HOST
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    zombie_sweep_interval_seconds: float = DEFAULT_ZOMBIE_SWEEP_INTERVAL_SECONDS
    zombie_timeout_seconds: float = DEFAULT_ZOMBIE_TIMEOUT_SECONDS
    log_level: str = DEFAULT_LOG_LEVEL
    dry_run: bool = False

    @classmethod
    def from_sources(
        cls,
        *,
        args: Any,
        environ: Mapping[str, str] | None = None,
    ) -> DaemonConfig:
        return Config.from_sources(
            config_path=getattr(args, "config", None),
            environ=environ,
            dry_run=bool(getattr(args, "dry_run", False)),
        ).to_daemon_config()


class Config(BaseSettings):
    """Developer-facing configuration surface for Loop Troop."""

    model_config = SettingsConfigDict(extra="ignore")

    repo: str | None = None
    db_path: str | None = None
    repo_path: str | None = None
    github_base_url: str = DEFAULT_GITHUB_BASE_URL
    github_pat: SecretStr | None = None
    github_app_id: int | None = None
    github_app_private_key_path: str | None = None
    github_app_installation_id: int | None = None
    ollama_host: str = DEFAULT_OLLAMA_HOST
    ollama_api_key: SecretStr = Field(default_factory=lambda: SecretStr(DEFAULT_OLLAMA_API_KEY))
    poll_interval_seconds: float = Field(default=DEFAULT_POLL_INTERVAL_SECONDS, gt=0)
    zombie_sweep_interval_seconds: float = Field(default=DEFAULT_ZOMBIE_SWEEP_INTERVAL_SECONDS, gt=0)
    zombie_timeout_seconds: float = Field(default=DEFAULT_ZOMBIE_TIMEOUT_SECONDS, gt=0)
    log_level: str = DEFAULT_LOG_LEVEL
    t1_model: str | None = None
    t2_model: str | None = None
    t3_model: str | None = None
    dry_run: bool = False

    @property
    def auth_mode(self) -> AuthMode | None:
        if self._has_complete_app_auth():
            return AuthMode.GITHUB_APP
        if self.github_pat_value is not None:
            return AuthMode.PAT
        return None

    @property
    def github_pat_value(self) -> str | None:
        if self.github_pat is None:
            return None
        return self.github_pat.get_secret_value()

    @property
    def ollama_api_key_value(self) -> str:
        return self.ollama_api_key.get_secret_value()

    @property
    def github_app_private_key_file(self) -> Path | None:
        if self.github_app_private_key_path is None:
            return None
        return Path(self.github_app_private_key_path).expanduser()

    @classmethod
    def from_sources(
        cls,
        *,
        config_path: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        dry_run: bool = False,
        require_repo: bool = False,
        require_auth: bool = False,
    ) -> Config:
        env = dict(os.environ if environ is None else environ)
        discovered_path = _discover_config_path(config_path)
        file_config = _load_config(discovered_path) if discovered_path is not None else {}
        payload = {
            **_file_values(file_config),
            **_env_values(env),
            "dry_run": dry_run,
        }
        config = cls.model_validate(payload)
        config.validate_runtime(require_repo=require_repo, require_auth=require_auth)
        return config

    def validate_runtime(self, *, require_repo: bool = False, require_auth: bool = False) -> None:
        errors: list[str] = []

        if require_repo and not self.repo:
            errors.append("Configure LOOP_TROOP_REPO or [github].repo.")

        app_values = (
            self.github_app_id,
            self.github_app_private_key_path,
            self.github_app_installation_id,
        )
        if any(value is not None for value in app_values) and not self._has_complete_app_auth():
            errors.append(
                "GitHub App auth requires LOOP_TROOP_APP_ID, "
                "LOOP_TROOP_APP_PRIVATE_KEY_PATH, and LOOP_TROOP_APP_INSTALLATION_ID together."
            )

        if require_auth and self.auth_mode is None:
            errors.append(
                "Configure either GITHUB_PAT for Personal Access Token auth, or "
                "LOOP_TROOP_APP_ID, LOOP_TROOP_APP_PRIVATE_KEY_PATH, and "
                "LOOP_TROOP_APP_INSTALLATION_ID for GitHub App auth."
            )

        app_key_path = self.github_app_private_key_file
        if self.auth_mode is AuthMode.GITHUB_APP and app_key_path is not None and not app_key_path.is_file():
            errors.append(f"GitHub App private key file does not exist: {app_key_path}")

        if errors:
            raise ValueError("Invalid Loop Troop configuration:\n- " + "\n- ".join(errors))

    def to_daemon_config(self) -> DaemonConfig:
        if not self.repo:
            raise ValueError("Config.repo must be set before creating a DaemonConfig.")
        return DaemonConfig(
            repo=self.repo,
            db_path=self.db_path,
            repo_path=self.repo_path,
            github_base_url=self.github_base_url,
            ollama_host=self.ollama_host.rstrip("/"),
            poll_interval_seconds=self.poll_interval_seconds,
            zombie_sweep_interval_seconds=self.zombie_sweep_interval_seconds,
            zombie_timeout_seconds=self.zombie_timeout_seconds,
            log_level=self.log_level,
            dry_run=self.dry_run,
        )

    def model_for_tier(self, tier: str) -> str | None:
        field_name = _TIER_MODEL_FIELDS.get(tier)
        return getattr(self, field_name) if field_name is not None else None

    def _has_complete_app_auth(self) -> bool:
        return all(
            value is not None
            for value in (
                self.github_app_id,
                self.github_app_private_key_path,
                self.github_app_installation_id,
            )
        )


def _discover_config_path(config_path: str | os.PathLike[str] | None) -> Path | None:
    if config_path is not None:
        return Path(config_path)
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    return None


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Config file {path} must contain a TOML table at the top level.")
    return payload


def _env_values(env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for env_key, field_name in _ENV_TO_FIELD.items():
        value = env.get(env_key)
        if value:
            values[field_name] = value
    return values


def _file_values(file_config: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field_name, paths in _FILE_TO_FIELD.items():
        value = _first_file_value(file_config, paths)
        if value is not None:
            values[field_name] = value
    return values


def _first_file_value(file_config: Mapping[str, Any], paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        value = _nested_value(file_config, path)
        if value is not None:
            return value
    return None


def _nested_value(payload: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


_ENV_TO_FIELD = {
    "LOOP_TROOP_REPO": "repo",
    "LOOP_TROOP_DB_PATH": "db_path",
    "LOOP_TROOP_REPO_PATH": "repo_path",
    "LOOP_TROOP_GITHUB_BASE_URL": "github_base_url",
    "GITHUB_PAT": "github_pat",
    "LOOP_TROOP_APP_ID": "github_app_id",
    "LOOP_TROOP_APP_PRIVATE_KEY_PATH": "github_app_private_key_path",
    "LOOP_TROOP_APP_INSTALLATION_ID": "github_app_installation_id",
    "OLLAMA_HOST": "ollama_host",
    "OLLAMA_API_KEY": "ollama_api_key",
    "LOOP_TROOP_POLL_INTERVAL": "poll_interval_seconds",
    "LOOP_TROOP_ZOMBIE_SWEEP_INTERVAL": "zombie_sweep_interval_seconds",
    "LOOP_TROOP_ZOMBIE_TIMEOUT": "zombie_timeout_seconds",
    "LOOP_TROOP_LOG_LEVEL": "log_level",
    "LOOP_TROOP_T1_MODEL": "t1_model",
    "LOOP_TROOP_T2_MODEL": "t2_model",
    "LOOP_TROOP_T3_MODEL": "t3_model",
}

_FILE_TO_FIELD = {
    "repo": (("github", "repo"),),
    "db_path": (("shadow_log", "db_path"),),
    "repo_path": (("workspace", "repo_path"),),
    "github_base_url": (("github", "base_url"),),
    "github_pat": (("github", "pat"),),
    "github_app_id": (("github", "app_id"), ("github", "app", "id")),
    "github_app_private_key_path": (
        ("github", "app_private_key_path"),
        ("github", "app", "private_key_path"),
    ),
    "github_app_installation_id": (
        ("github", "app_installation_id"),
        ("github", "app", "installation_id"),
    ),
    "ollama_host": (("ollama", "host"),),
    "ollama_api_key": (("ollama", "api_key"),),
    "poll_interval_seconds": (("daemon", "poll_interval_seconds"),),
    "zombie_sweep_interval_seconds": (("daemon", "zombie_sweep_interval_seconds"),),
    "zombie_timeout_seconds": (("daemon", "zombie_timeout_seconds"),),
    "log_level": (("logging", "level"),),
    "t1_model": (("models", "t1_model"), ("models", "t1")),
    "t2_model": (("models", "t2_model"), ("models", "t2")),
    "t3_model": (("models", "t3_model"), ("models", "t3")),
}
