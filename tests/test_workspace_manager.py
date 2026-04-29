import subprocess
from pathlib import Path

import pytest

from loop_troop.core import (
    TemplateValidationError,
    WorkspaceManager,
    WorkspaceUpdateError,
    WorkspaceViolationError,
)


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True, text=True)


def _init_repo(path: Path, *, valid_template: bool = True) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Fixture repo\n")

    if valid_template:
        (path / "Makefile").write_text("test:\n\t@echo test\n")
        (path / "Dockerfile").write_text("FROM scratch\n")
        (path / "docs" / "architecture").mkdir(parents=True)
        (path / "docs" / "architecture" / "ADR-0001.md").write_text("# ADR\n")

    _run_git(path, "init")
    _run_git(path, "config", "user.name", "LoopTroopTests")
    _run_git(path, "config", "user.email", "loop-troop@example.com")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-m", "Initial fixture")
    return path


def test_workspace_manager_clones_repo_into_workspace(tmp_path: Path) -> None:
    source_repo = _init_repo(tmp_path / "source-repo")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    original_cwd = Path.cwd()

    cloned_repo = manager.clone_or_update(str(source_repo))

    assert cloned_repo == (tmp_path / "workspaces" / "source-repo").resolve()
    assert (cloned_repo / ".loop-troop-workspace").read_text() == "managed-by=loop-troop\n"
    assert (cloned_repo / "Makefile").is_file()
    assert (cloned_repo / "Dockerfile").is_file()
    assert (cloned_repo / "docs" / "architecture").is_dir()
    assert Path.cwd() == original_cwd


def test_workspace_manager_updates_existing_clone(tmp_path: Path) -> None:
    source_repo = _init_repo(tmp_path / "source-repo")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    cloned_repo = manager.clone_or_update(str(source_repo))

    (source_repo / "CHANGELOG.md").write_text("updated\n")
    _run_git(source_repo, "add", "CHANGELOG.md")
    _run_git(source_repo, "commit", "-m", "Add changelog")

    updated_repo = manager.clone_or_update(str(source_repo))

    assert updated_repo == cloned_repo
    assert (updated_repo / "CHANGELOG.md").read_text() == "updated\n"


def test_workspace_manager_raises_clear_error_for_non_fast_forward_updates(tmp_path: Path) -> None:
    source_repo = _init_repo(tmp_path / "source-repo")
    workspace_base = tmp_path / "workspaces"
    WorkspaceManager(workspace_base=workspace_base).clone_or_update(str(source_repo))

    def runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            stderr="fatal: Not possible to fast-forward, aborting.\n",
        )

    manager = WorkspaceManager(workspace_base=workspace_base, runner=runner)

    with pytest.raises(WorkspaceUpdateError, match="fast-forward pull"):
        manager.clone_or_update(str(source_repo))


def test_workspace_manager_rejects_path_traversal(tmp_path: Path) -> None:
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    external_repo = _init_repo(tmp_path / "external-repo")

    with pytest.raises(WorkspaceViolationError):
        manager.checkout_branch(manager.workspace_base / ".." / external_repo.name, "main")


def test_workspace_manager_rejects_symlinked_repo_paths(tmp_path: Path) -> None:
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    external_repo = _init_repo(tmp_path / "external-repo")
    symlink_path = manager.workspace_base / "external-repo"
    symlink_path.symlink_to(external_repo, target_is_directory=True)

    with pytest.raises(WorkspaceViolationError):
        manager.checkout_branch(symlink_path, "main")


def test_workspace_manager_validates_template_structure(tmp_path: Path) -> None:
    invalid_repo = _init_repo(tmp_path / "invalid-repo", valid_template=False)
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")

    with pytest.raises(TemplateValidationError):
        manager.clone_or_update(str(invalid_repo))

    assert not (manager.workspace_base / "invalid-repo").exists()


def test_workspace_manager_manages_branches_and_cleanup(tmp_path: Path) -> None:
    source_repo = _init_repo(tmp_path / "source-repo")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    cloned_repo = manager.clone_or_update(str(source_repo))
    initial_branch = _run_git(cloned_repo, "branch", "--show-current").stdout.strip()

    manager.create_branch(cloned_repo, "feature/test-workspace")
    assert _run_git(cloned_repo, "branch", "--show-current").stdout.strip() == "feature/test-workspace"

    manager.checkout_branch(cloned_repo, initial_branch)
    assert _run_git(cloned_repo, "branch", "--show-current").stdout.strip() == initial_branch

    manager.cleanup(cloned_repo)
    assert not cloned_repo.exists()
