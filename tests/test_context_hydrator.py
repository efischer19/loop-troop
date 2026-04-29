import subprocess
from pathlib import Path

import pytest

from loop_troop.core import ContextBudgetExceededError, ContextHydrator, WorkspaceViolationError


def _init_fixture_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Fixture repo\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('hello world')\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "LoopTroopTests"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "loop-troop@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "Initial fixture"], cwd=path, check=True, capture_output=True, text=True)
    return path


def _completed_process(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def test_context_hydrator_full_hydration(tmp_path: Path) -> None:
    repo_path = _init_fixture_repo(tmp_path / "fixture-repo")
    repomix_output = "FILE: src/app.py\nprint('hello world')\n"

    def runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed_process("fixture-sha\n")
        assert command == ["npx", "repomix", "--stdout"]
        assert kwargs["cwd"] == repo_path
        return _completed_process(repomix_output)

    hydrator = ContextHydrator(
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
        runner=runner,
    )

    hydrated = hydrator.hydrate(
        repo_path=repo_path,
        issue_context="Issue body",
        adr_context="ADR context",
    )

    assert hydrated == "\n\n".join(
        [
            "## GitHub Issue / Checklist",
            "Issue body",
            "## ADR Context",
            "ADR context",
            "## Repomix Codebase Context",
            repomix_output,
        ]
    )


def test_context_hydrator_passes_focus_files_to_repomix(tmp_path: Path) -> None:
    repo_path = _init_fixture_repo(tmp_path / "fixture-repo")
    seen_commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        seen_commands.append(command)
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return _completed_process("fixture-sha\n")
        return _completed_process("focused output")

    hydrator = ContextHydrator(
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
        runner=runner,
    )

    hydrator.hydrate(
        repo_path=repo_path,
        issue_context="Issue body",
        adr_context="ADR context",
        focus_files=["tests/test_app.py", "src/app.py", "src/app.py"],
    )

    assert seen_commands == [
        ["git", "rev-parse", "HEAD"],
        ["npx", "repomix", "--stdout", "--include", "src/app.py,tests/test_app.py"],
    ]


def test_context_hydrator_truncates_only_codebase_layer(tmp_path: Path) -> None:
    repo_path = _init_fixture_repo(tmp_path / "fixture-repo")
    hydrator = ContextHydrator(
        max_tokens=7,
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
        runner=lambda command, **_kwargs: _completed_process("sha\n")
        if command[:3] == ["git", "rev-parse", "HEAD"]
        else _completed_process("alpha beta gamma delta epsilon zeta"),
    )

    hydrated = hydrator.hydrate(
        repo_path=repo_path,
        issue_context="issue",
        adr_context="adr",
        issue_tokens=1,
        adr_tokens=1,
    )

    assert "## GitHub Issue / Checklist\n\nissue" in hydrated
    assert "## ADR Context\n\nadr" in hydrated
    assert "alpha beta gamma delta" in hydrated
    assert "[TRUNCATED]" in hydrated
    assert "epsilon" not in hydrated


def test_context_hydrator_raises_when_required_context_exceeds_budget(tmp_path: Path) -> None:
    repo_path = _init_fixture_repo(tmp_path / "fixture-repo")
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return _completed_process("fixture-sha\n")

    hydrator = ContextHydrator(
        max_tokens=4,
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
        runner=runner,
    )

    with pytest.raises(ContextBudgetExceededError):
        hydrator.hydrate(
            repo_path=repo_path,
            issue_context="issue too large",
            adr_context="adr",
            issue_tokens=3,
            adr_tokens=1,
        )

    assert calls == []


def test_context_hydrator_uses_cache_by_commit_and_focus(tmp_path: Path) -> None:
    repo_path = _init_fixture_repo(tmp_path / "fixture-repo")
    repomix_calls = 0

    def runner(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        nonlocal repomix_calls
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            completed = subprocess.run(command, cwd=kwargs["cwd"], check=True, capture_output=True, text=True)
            return _completed_process(completed.stdout)
        repomix_calls += 1
        return _completed_process(f"repomix-run-{repomix_calls}")

    hydrator = ContextHydrator(
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
        runner=runner,
    )

    first = hydrator.hydrate(repo_path=repo_path, issue_context="issue", adr_context="adr")
    second = hydrator.hydrate(repo_path=repo_path, issue_context="issue", adr_context="adr")

    (repo_path / "CHANGELOG.md").write_text("updated\n")
    subprocess.run(
        ["git", "add", "CHANGELOG.md"],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "commit", "-m", "Add changelog"], cwd=repo_path, check=True, capture_output=True, text=True)

    third = hydrator.hydrate(repo_path=repo_path, issue_context="issue", adr_context="adr")
    fourth = hydrator.hydrate(
        repo_path=repo_path,
        issue_context="issue",
        adr_context="adr",
        focus_files=["src/app.py"],
    )

    assert first == second
    assert "repomix-run-1" in first
    assert "repomix-run-2" in third
    assert "repomix-run-3" in fourth
    assert repomix_calls == 3


def test_context_hydrator_rejects_paths_inside_loop_troop_root(tmp_path: Path) -> None:
    loop_troop_root = tmp_path / "loop-troop-root"
    nested_repo = _init_fixture_repo(loop_troop_root / "nested-repo")
    hydrator = ContextHydrator(
        cache_dir=tmp_path / "cache",
        loop_troop_root=loop_troop_root,
        runner=lambda *_args, **_kwargs: _completed_process("unused"),
    )

    with pytest.raises(WorkspaceViolationError):
        hydrator.hydrate(
            repo_path=nested_repo,
            issue_context="issue",
            adr_context="adr",
        )
