"""Unit tests for ConflictResolver — Git Conflict Resolution Subflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from loop_troop.coder import (
    ConflictResolver,
    ConflictResolverOutcome,
    InnerLoopResult,
    ParsedChecklistItem,
)
from loop_troop.core.github_client import GitHubIssueComment, GitHubLabel, GitHubPullRequest
from loop_troop.core.schemas import ConflictResolution, ResolvedFile
from loop_troop.core.workspace_manager import WorkspaceManager
from loop_troop.dispatcher import WorkflowLabel


# ---------------------------------------------------------------------------
# Helpers — real git operations
# ---------------------------------------------------------------------------


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True, text=True)


def _init_remote_repo(path: Path) -> Path:
    """Create a bare-ish remote repo with the Loop Troop template structure."""
    path.mkdir(parents=True)
    (path / "Makefile").write_text("test:\n\t@echo test\n")
    (path / "Dockerfile").write_text("FROM scratch\n")
    (path / "docs" / "architecture").mkdir(parents=True)
    (path / "docs" / "architecture" / "ADR-0001.md").write_text("# ADR\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("def greet(): return 'hello'\n")

    _run_git(path, "init")
    _run_git(path, "config", "user.name", "LoopTroopTests")
    _run_git(path, "config", "user.email", "loop-troop@example.com")
    _run_git(path, "config", "receive.denyCurrentBranch", "updateInstead")
    _run_git(path, "branch", "-M", "main")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-m", "Initial commit")
    return path


def _setup_conflict_workspace(
    tmp_path: Path,
    *,
    feature_content: str = "def greet(): return 'hello feature'\n",
    main_content: str = "def greet(): return 'hello main'\n",
    extra_files: dict[str, tuple[str, str]] | None = None,
) -> tuple[Path, Path, WorkspaceManager, str]:
    """Set up a workspace with a merge conflict ready to be resolved.

    Returns (remote_path, workspace_path, manager, feature_branch_name).

    *extra_files* maps ``path`` → ``(feature_content, main_content)`` for
    additional conflicting files.
    """
    remote = _init_remote_repo(tmp_path / "remote")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    workspace = manager.clone_or_update(str(remote))

    # Configure git identity in the workspace (cloned repos inherit remote config
    # but not user identity on some CI systems).
    _run_git(workspace, "config", "user.name", "LoopTroopTests")
    _run_git(workspace, "config", "user.email", "loop-troop@example.com")

    feature_branch = "feature/conflict-test"

    # Feature branch — make a change that will conflict with main.
    _run_git(workspace, "checkout", "-b", feature_branch)
    (workspace / "src" / "app.py").write_text(feature_content)
    if extra_files:
        for rel_path, (feat_c, _) in extra_files.items():
            full = workspace / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(feat_c)
    _run_git(workspace, "add", ".")
    _run_git(workspace, "commit", "-m", "Feature change")

    # Remote main — make a conflicting change directly in the remote.
    (remote / "src" / "app.py").write_text(main_content)
    if extra_files:
        for rel_path, (_, main_c) in extra_files.items():
            full = remote / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(main_c)
    _run_git(remote, "add", ".")
    _run_git(remote, "commit", "-m", "Main update from other coder")

    return remote, workspace, manager, feature_branch


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLLMClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeInnerLoop:
    def __init__(self, results: list[InnerLoopResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    async def run(self, *, repo_path, checklist_item, code_patch) -> InnerLoopResult:
        self.calls.append(
            {
                "repo_path": str(repo_path),
                "description": checklist_item.description,
                "test_command": code_patch.test_command,
            }
        )
        return self._results.pop(0)


class FakeContextHydrator:
    def __init__(self, context: str = "hydrated context") -> None:
        self._context = context
        self.calls: list[dict[str, object]] = []

    def hydrate(self, *, repo_path, issue_context: str, adr_context: str, focus_files=None, **_kw) -> str:
        self.calls.append(
            {
                "repo_path": str(repo_path),
                "issue_context": issue_context,
                "focus_files": list(focus_files or []),
            }
        )
        return self._context


class FakeGitHubClient:
    def __init__(
        self,
        pull_request: GitHubPullRequest,
        comments: list[GitHubIssueComment],
    ) -> None:
        self._pull_request = pull_request
        self._comments = comments
        self.replaced_labels: list[list[str]] = []

    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> GitHubPullRequest:
        return self._pull_request

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]:
        return self._comments

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]:
        self.replaced_labels.append(labels)
        return labels


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _architect_comment() -> GitHubIssueComment:
    return GitHubIssueComment(
        id=11,
        body=(
            "## Architect Plan\n"
            "- [ ] Update the greeting function\n"
            "  - Files: `src/app.py`\n"
            "  - Tests required: No\n"
        ),
    )


def _pr(
    feature_branch: str,
    *,
    extra_labels: list[str] | None = None,
) -> GitHubPullRequest:
    labels: list[GitHubLabel] = [GitHubLabel(name=WorkflowLabel.MERGE_CONFLICT.value)]
    for name in extra_labels or []:
        labels.append(GitHubLabel(name=name))
    return GitHubPullRequest(
        number=1,
        id=1,
        state="open",
        title="feat: update greet",
        labels=labels,
        head={"sha": "abc123", "ref": feature_branch},
    )


# ---------------------------------------------------------------------------
# Test: simple single-file conflict resolution — tests pass → needs-review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_conflict_resolution_success(tmp_path: Path) -> None:
    """A single conflicting file is resolved by the LLM; tests pass; label updated."""
    _remote, workspace, manager, feature_branch = _setup_conflict_workspace(tmp_path)

    github_client = FakeGitHubClient(_pr(feature_branch), [_architect_comment()])
    resolution = ConflictResolution(
        resolved_files=[ResolvedFile(path="src/app.py", content="def greet(): return 'hello resolved'\n")],
        resolution_rationale="Merged feature and main changes",
    )
    llm = FakeLLMClient([resolution])
    inner_loop = FakeInnerLoop([InnerLoopResult(success=True, mode="standard")])
    hydrator = FakeContextHydrator()

    resolver = ConflictResolver(
        github_client=github_client,
        llm_client=llm,
        context_hydrator=hydrator,
        workspace_manager=manager,
        inner_loop=inner_loop,
    )

    outcome = await resolver.resolve(
        owner="octo",
        repo="repo",
        pr_number=1,
        issue_number=42,
        repo_path=workspace,
        base_branch="main",
        test_command="make test",
    )

    assert outcome.target_label == WorkflowLabel.NEEDS_REVIEW
    assert outcome.conflicts_resolved == 1
    assert outcome.pr_number == 1
    assert outcome.issue_number == 42
    assert outcome.branch_name == feature_branch

    # Resolved content written to disk
    assert (workspace / "src" / "app.py").read_text() == "def greet(): return 'hello resolved'\n"

    # LLM was called once with a ConflictResolution response model
    assert len(llm.calls) == 1
    assert llm.calls[0]["response_model"] is ConflictResolution

    # InnerLoop was called once
    assert len(inner_loop.calls) == 1
    assert inner_loop.calls[0]["test_command"] == "make test"

    # Label updated to needs-review (merge-conflict removed)
    assert len(github_client.replaced_labels) == 1
    applied_labels = github_client.replaced_labels[0]
    assert WorkflowLabel.NEEDS_REVIEW.value in applied_labels
    assert WorkflowLabel.MERGE_CONFLICT.value not in applied_labels


# ---------------------------------------------------------------------------
# Test: multi-file conflict resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_file_conflict_resolution(tmp_path: Path) -> None:
    """Multiple conflicting files are all resolved and tests pass."""
    _remote, workspace, manager, feature_branch = _setup_conflict_workspace(
        tmp_path,
        extra_files={
            "src/utils.py": (
                "def helper(): return 'feature helper'\n",
                "def helper(): return 'main helper'\n",
            )
        },
    )

    github_client = FakeGitHubClient(_pr(feature_branch), [_architect_comment()])
    resolution = ConflictResolution(
        resolved_files=[
            ResolvedFile(path="src/app.py", content="def greet(): return 'hello resolved'\n"),
            ResolvedFile(path="src/utils.py", content="def helper(): return 'resolved helper'\n"),
        ],
        resolution_rationale="Merged both files",
    )
    llm = FakeLLMClient([resolution])
    inner_loop = FakeInnerLoop([InnerLoopResult(success=True, mode="standard")])
    hydrator = FakeContextHydrator()

    resolver = ConflictResolver(
        github_client=github_client,
        llm_client=llm,
        context_hydrator=hydrator,
        workspace_manager=manager,
        inner_loop=inner_loop,
    )

    outcome = await resolver.resolve(
        owner="octo",
        repo="repo",
        pr_number=1,
        issue_number=42,
        repo_path=workspace,
        base_branch="main",
    )

    assert outcome.target_label == WorkflowLabel.NEEDS_REVIEW
    assert outcome.conflicts_resolved == 2

    assert (workspace / "src" / "app.py").read_text() == "def greet(): return 'hello resolved'\n"
    assert (workspace / "src" / "utils.py").read_text() == "def helper(): return 'resolved helper'\n"

    # Hydrator received both conflicting files
    assert len(hydrator.calls) == 1
    assert sorted(hydrator.calls[0]["focus_files"]) == ["src/app.py", "src/utils.py"]


# ---------------------------------------------------------------------------
# Test: resolution that breaks tests → escalates to needs-help
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_fails_tests_escalates_to_needs_help(tmp_path: Path) -> None:
    """If InnerLoop reports failure after resolution, escalate to loop: needs-help."""
    _remote, workspace, manager, feature_branch = _setup_conflict_workspace(tmp_path)

    github_client = FakeGitHubClient(_pr(feature_branch), [_architect_comment()])
    resolution = ConflictResolution(
        resolved_files=[ResolvedFile(path="src/app.py", content="def greet(): return None  # broken\n")],
        resolution_rationale="Attempted resolution",
    )
    llm = FakeLLMClient([resolution])
    inner_loop = FakeInnerLoop(
        [InnerLoopResult(success=False, mode="standard", failure_summary="AssertionError: expected str")]
    )
    hydrator = FakeContextHydrator()

    resolver = ConflictResolver(
        github_client=github_client,
        llm_client=llm,
        context_hydrator=hydrator,
        workspace_manager=manager,
        inner_loop=inner_loop,
    )

    outcome = await resolver.resolve(
        owner="octo",
        repo="repo",
        pr_number=1,
        issue_number=42,
        repo_path=workspace,
        base_branch="main",
    )

    assert outcome.target_label == WorkflowLabel.NEEDS_HELP
    assert outcome.conflicts_resolved == 1

    # Label escalated to needs-help
    assert len(github_client.replaced_labels) == 1
    applied_labels = github_client.replaced_labels[0]
    assert WorkflowLabel.NEEDS_HELP.value in applied_labels
    assert WorkflowLabel.MERGE_CONFLICT.value not in applied_labels


# ---------------------------------------------------------------------------
# Test: conflict prompt format
# ---------------------------------------------------------------------------


def test_build_conflict_prompt_format() -> None:
    """The conflict prompt clearly presents both versions and the intended behavior."""
    checklist_item = ParsedChecklistItem(
        comment_id=1,
        comment_body="",
        item_index=1,
        line_index=0,
        description="Update the greeting function",
        files_touched=("src/app.py",),
        requires_test=False,
        test_instructions=None,
    )
    file_contexts = [
        ("src/app.py", "def greet(): return 'ours'\n", "def greet(): return 'theirs'\n"),
    ]

    prompt = ConflictResolver._build_conflict_prompt(
        file_contexts=file_contexts,
        checklist_item=checklist_item,
    )

    assert "Intended behavior: Update the greeting function" in prompt
    assert "Version A (ours):" in prompt
    assert "def greet(): return 'ours'" in prompt
    assert "Version B (theirs):" in prompt
    assert "def greet(): return 'theirs'" in prompt
    assert "src/app.py" in prompt
    assert "Produce the merged file content." in prompt


# ---------------------------------------------------------------------------
# Test: _detect_conflicts returns conflicting file paths
# ---------------------------------------------------------------------------


def test_detect_conflicts_returns_conflicting_files(tmp_path: Path) -> None:
    """_detect_conflicts correctly identifies files in U (unmerged) state."""
    _remote, workspace, manager, _branch = _setup_conflict_workspace(tmp_path)

    # Trigger the merge conflict manually
    subprocess.run(
        ["git", "fetch", "origin", "main"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "merge", "origin/main"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,  # Expected to fail with conflicts
    )

    resolver = ConflictResolver(
        github_client=FakeGitHubClient(_pr(_branch), []),
    )
    conflict_files = resolver._detect_conflicts(workspace)

    assert conflict_files == ["src/app.py"]


# ---------------------------------------------------------------------------
# Test: _read_conflict_version reads ours and theirs from git index
# ---------------------------------------------------------------------------


def test_read_conflict_versions(tmp_path: Path) -> None:
    """_read_conflict_version correctly reads stage 2 (ours) and stage 3 (theirs)."""
    _remote, workspace, manager, _branch = _setup_conflict_workspace(
        tmp_path,
        feature_content="def greet(): return 'feature'\n",
        main_content="def greet(): return 'main'\n",
    )

    subprocess.run(
        ["git", "fetch", "origin", "main"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "merge", "origin/main"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )

    resolver = ConflictResolver(
        github_client=FakeGitHubClient(_pr(_branch), []),
    )

    ours = resolver._read_conflict_version(workspace, "src/app.py", stage=2)
    theirs = resolver._read_conflict_version(workspace, "src/app.py", stage=3)

    assert "feature" in ours
    assert "main" in theirs


# ---------------------------------------------------------------------------
# Test: clean merge (no conflicts) → push and relabel as needs-review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_merge_relabels_as_needs_review(tmp_path: Path) -> None:
    """If the merge succeeds without conflicts the PR is relabelled needs-review."""
    remote = _init_remote_repo(tmp_path / "remote")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    workspace = manager.clone_or_update(str(remote))

    # Configure git identity in the cloned workspace.
    _run_git(workspace, "config", "user.name", "LoopTroopTests")
    _run_git(workspace, "config", "user.email", "loop-troop@example.com")

    feature_branch = "feature/no-conflict"
    _run_git(workspace, "checkout", "-b", feature_branch)
    # Touch a different file than what main will change — no conflict.
    (workspace / "README.md").write_text("# Updated readme\n")
    _run_git(workspace, "add", ".")
    _run_git(workspace, "commit", "-m", "Readme update")

    # Remote main adds an unrelated commit (different file).
    (remote / "Makefile").write_text("test:\n\t@echo all-good\n")
    _run_git(remote, "add", ".")
    _run_git(remote, "commit", "-m", "Makefile update")

    github_client = FakeGitHubClient(_pr(feature_branch), [_architect_comment()])
    llm = FakeLLMClient([])
    inner_loop = FakeInnerLoop([])

    resolver = ConflictResolver(
        github_client=github_client,
        llm_client=llm,
        workspace_manager=manager,
        inner_loop=inner_loop,
    )

    outcome = await resolver.resolve(
        owner="octo",
        repo="repo",
        pr_number=1,
        issue_number=42,
        repo_path=workspace,
        base_branch="main",
    )

    assert outcome.target_label == WorkflowLabel.NEEDS_REVIEW
    assert outcome.conflicts_resolved == 0

    # LLM and InnerLoop not called — clean merge requires no AI intervention.
    assert len(llm.calls) == 0
    assert len(inner_loop.calls) == 0

    applied_labels = github_client.replaced_labels[0]
    assert WorkflowLabel.NEEDS_REVIEW.value in applied_labels
    assert WorkflowLabel.MERGE_CONFLICT.value not in applied_labels


# ---------------------------------------------------------------------------
# Test: MERGE_CONFLICT label exists in WorkflowLabel
# ---------------------------------------------------------------------------


def test_merge_conflict_label_in_workflow_label() -> None:
    """WorkflowLabel.MERGE_CONFLICT is defined with the expected value."""
    assert WorkflowLabel.MERGE_CONFLICT.value == "loop: merge-conflict"
    assert WorkflowLabel.MERGE_CONFLICT in WorkflowLabel._value2member_map_.values()
