import subprocess
from pathlib import Path

import pytest

from loop_troop.core import ADRLoader, ContextBudgetExceededError, WorkspaceViolationError
from loop_troop.core.schemas import ADRStatus


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True, text=True)


def _init_repo(path: Path, *, with_adr_dir: bool = True) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Fixture repo\n")
    if with_adr_dir:
        (path / "docs" / "architecture").mkdir(parents=True)

    _run_git(path, "init")
    _run_git(path, "config", "user.name", "LoopTroopTests")
    _run_git(path, "config", "user.email", "loop-troop@example.com")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-m", "Initial fixture")
    return path


def _commit_all(repo_path: Path, message: str) -> None:
    _run_git(repo_path, "add", ".")
    _run_git(repo_path, "commit", "-m", message)


def _write_fixture_adrs(repo_path: Path) -> None:
    adr_dir = repo_path / "docs" / "architecture"
    (adr_dir / "ADR-0001-use-adrs.md").write_text(
        """---
title: "ADR-0001: Use ADRs"
status: "Accepted"
date: "2025-07-14"
tags:
  - "process"
---

## Context

Project history needs durable architecture documentation.

## Decision

We will record significant architectural decisions as ADRs in version control.

## Consequences

Architectural rationale stays discoverable.
"""
    )
    (adr_dir / "ADR-0002-use-fastapi.md").write_text(
        """# ADR-0002: Use FastAPI

## Status

Accepted

## Context

The service needs a lightweight HTTP framework.

## Decision

We standardize on FastAPI for new HTTP services.

## Consequences

API handlers share one framework.
"""
    )
    (adr_dir / "ADR-0003-old-framework.md").write_text(
        """# ADR-0003: Retire Flask

## Status

Superseded by ADR-0002

## Decision

Flask is no longer the default framework for new services.
"""
    )
    (adr_dir / "ADR-0004-deprecated-tooling.md").write_text(
        """---
title: "ADR-0004: Legacy Tooling"
status: "Deprecated"
date: "2024-01-01"
---

## Decision

Legacy scaffolding is deprecated and should not be used for new projects.
"""
    )


def test_adr_loader_parses_frontmatter_and_inline_documents(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path / "fixture-repo")
    _write_fixture_adrs(repo_path)
    _commit_all(repo_path, "Add ADR fixtures")
    loader = ADRLoader(cache_dir=tmp_path / "cache", loop_troop_root=tmp_path / "loop-troop-root")

    documents = loader.load(repo_path, include_all=True)

    assert [document.id for document in documents] == [
        "ADR-0004",
        "ADR-0003",
        "ADR-0002",
        "ADR-0001",
    ]
    assert documents[0].status is ADRStatus.DEPRECATED
    assert documents[1].status is ADRStatus.SUPERSEDED
    assert documents[2].title == "Use FastAPI"
    assert documents[2].decision_summary == "We standardize on FastAPI for new HTTP services."
    assert documents[3].title == "Use ADRs"
    assert "## Decision" in documents[3].full_text


def test_adr_loader_filters_to_accepted_by_default_and_builds_context(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path / "fixture-repo")
    _write_fixture_adrs(repo_path)
    _commit_all(repo_path, "Add ADR fixtures")
    loader = ADRLoader(cache_dir=tmp_path / "cache", loop_troop_root=tmp_path / "loop-troop-root")

    documents = loader.load(repo_path)
    context = loader.build_context(repo_path)

    assert [document.id for document in documents] == ["ADR-0002", "ADR-0001"]
    assert "### ADR-0002: Use FastAPI" in context
    assert "### ADR-0001: Use ADRs" in context
    assert "ADR-0003" not in context


def test_adr_loader_raises_when_context_exceeds_token_budget(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path / "fixture-repo")
    _write_fixture_adrs(repo_path)
    _commit_all(repo_path, "Add ADR fixtures")
    loader = ADRLoader(
        token_budget=10,
        cache_dir=tmp_path / "cache",
        loop_troop_root=tmp_path / "loop-troop-root",
    )

    with pytest.raises(ContextBudgetExceededError, match="ADR context exceeds token budget"):
        loader.build_context(repo_path)


def test_adr_loader_handles_missing_adr_directory_gracefully(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path / "fixture-repo", with_adr_dir=False)
    loader = ADRLoader(cache_dir=tmp_path / "cache", loop_troop_root=tmp_path / "loop-troop-root")

    assert loader.load(repo_path) == []
    assert loader.build_context(repo_path) == ""


def test_adr_loader_reuses_cache_until_commit_sha_changes(tmp_path: Path) -> None:
    repo_path = _init_repo(tmp_path / "fixture-repo")
    _write_fixture_adrs(repo_path)
    _commit_all(repo_path, "Add ADR fixtures")
    loader = ADRLoader(cache_dir=tmp_path / "cache", loop_troop_root=tmp_path / "loop-troop-root")

    initial_documents = loader.load(repo_path)
    (repo_path / "docs" / "architecture" / "ADR-0005-new-adr.md").write_text(
        """# ADR-0005: Cached Change

## Status

Accepted

## Decision

This ADR should not appear until it is committed.
"""
    )

    cached_documents = loader.load(repo_path)
    _commit_all(repo_path, "Commit new ADR")
    refreshed_documents = loader.load(repo_path)

    assert [document.id for document in initial_documents] == ["ADR-0002", "ADR-0001"]
    assert [document.id for document in cached_documents] == ["ADR-0002", "ADR-0001"]
    assert [document.id for document in refreshed_documents] == ["ADR-0005", "ADR-0002", "ADR-0001"]


def test_adr_loader_rejects_repo_paths_inside_loop_troop_root(tmp_path: Path) -> None:
    loop_troop_root = tmp_path / "loop-troop-root"
    repo_path = _init_repo(loop_troop_root / "fixture-repo")
    _write_fixture_adrs(repo_path)
    _commit_all(repo_path, "Add ADR fixtures")
    loader = ADRLoader(cache_dir=tmp_path / "cache", loop_troop_root=loop_troop_root)

    with pytest.raises(WorkspaceViolationError):
        loader.load(repo_path)
