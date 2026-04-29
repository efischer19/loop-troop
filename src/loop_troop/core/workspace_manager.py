"""Workspace management utilities for target repository isolation."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from .context_hydrator import WorkspaceViolationError


class TemplateValidationError(ValueError):
    """Raised when a target repository does not match the expected Loop Troop template."""


class WorkspaceUpdateError(RuntimeError):
    """Raised when an existing managed workspace cannot be updated automatically."""


class WorkspaceManager:
    """Manage isolated target repository workspaces outside the Loop Troop source tree."""

    _SENTINEL_FILENAME = ".loop-troop-workspace"

    def __init__(
        self,
        *,
        workspace_base: str | Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.workspace_base = Path(workspace_base or Path.home() / ".loop-troop" / "workspaces").expanduser().resolve()
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        self._runner = runner

    def clone_or_update(self, repo_url: str) -> Path:
        target_path = self._target_path_for_repo(repo_url)
        cloned = False

        if target_path.exists():
            resolved_target_path = self._validate_workspace_path(target_path)
            self._ensure_managed_workspace(resolved_target_path)
            try:
                self._run_git(["git", "pull", "--ff-only"], cwd=resolved_target_path)
            except subprocess.CalledProcessError as error:
                raise WorkspaceUpdateError(
                    f"Could not update managed workspace at {resolved_target_path} with a fast-forward pull: "
                    f"{error.stderr.strip()}"
                ) from error
        else:
            cloned = True
            self._run_git(["git", "clone", repo_url, str(target_path)], cwd=self.workspace_base)

        try:
            resolved_target_path = self._validate_workspace_path(target_path)
            self._validate_template_structure(resolved_target_path)
        except Exception:
            if cloned and target_path.exists():
                shutil.rmtree(target_path)
            raise

        self._write_sentinel(resolved_target_path)
        return resolved_target_path

    def checkout_branch(self, repo_path: str | Path, branch: str) -> None:
        resolved_repo_path = self._validate_managed_workspace(repo_path)
        self._run_git(["git", "checkout", branch], cwd=resolved_repo_path)

    def create_branch(self, repo_path: str | Path, branch: str) -> None:
        resolved_repo_path = self._validate_managed_workspace(repo_path)
        self._run_git(["git", "checkout", "-b", branch], cwd=resolved_repo_path)

    def cleanup(self, repo_path: str | Path) -> None:
        resolved_repo_path = self._validate_managed_workspace(repo_path)
        shutil.rmtree(resolved_repo_path)

    def _target_path_for_repo(self, repo_url: str) -> Path:
        repo_name = self._repo_name_from_url(repo_url)
        return self._validate_workspace_path(self.workspace_base / repo_name, must_exist=False)

    def _validate_managed_workspace(self, repo_path: str | Path) -> Path:
        resolved_repo_path = self._validate_workspace_path(repo_path)
        self._ensure_managed_workspace(resolved_repo_path)
        return resolved_repo_path

    def _validate_workspace_path(self, repo_path: str | Path, *, must_exist: bool = True) -> Path:
        candidate_path = Path(repo_path).expanduser()
        if not candidate_path.is_absolute():
            candidate_path = self.workspace_base / candidate_path

        resolved_repo_path = candidate_path.resolve()
        if not resolved_repo_path.is_relative_to(self.workspace_base):
            raise WorkspaceViolationError(
                f"Workspace path must remain within the managed workspace base directory: {self.workspace_base}"
            )
        if must_exist and not resolved_repo_path.exists():
            raise FileNotFoundError(f"Workspace path does not exist: {resolved_repo_path}")
        if must_exist and not resolved_repo_path.is_dir():
            raise NotADirectoryError(f"Workspace path is not a directory: {resolved_repo_path}")
        return resolved_repo_path

    def _ensure_managed_workspace(self, repo_path: Path) -> None:
        sentinel_path = repo_path / self._SENTINEL_FILENAME
        if not sentinel_path.is_file():
            raise WorkspaceViolationError(f"Workspace path is not managed by Loop Troop: {repo_path}")

    def _validate_template_structure(self, repo_path: Path) -> None:
        missing_paths: list[str] = []
        if not (repo_path / "Makefile").is_file():
            missing_paths.append("Makefile")
        if not (repo_path / "Dockerfile").is_file():
            missing_paths.append("Dockerfile")
        if not (repo_path / "docs" / "architecture").is_dir():
            missing_paths.append("docs/architecture/")
        if missing_paths:
            raise TemplateValidationError(
                "Repository is missing required Loop Troop template paths: " + ", ".join(missing_paths)
            )

    def _write_sentinel(self, repo_path: Path) -> None:
        (repo_path / self._SENTINEL_FILENAME).write_text("managed-by=loop-troop\n")

    def _run_git(self, command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return self._runner(command, cwd=cwd, check=True, capture_output=True, text=True)

    @staticmethod
    def _repo_name_from_url(repo_url: str) -> str:
        normalized_url = repo_url.rstrip("/")
        if normalized_url.startswith("git@") and ":" in normalized_url:
            repo_path = normalized_url.rsplit(":", maxsplit=1)[1]
        else:
            parsed_url = urlparse(normalized_url)
            repo_path = parsed_url.path if parsed_url.scheme else normalized_url

        repo_name = Path(repo_path).name
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        if not repo_name:
            raise ValueError(f"Could not derive repository name from URL: {repo_url}")
        return repo_name
