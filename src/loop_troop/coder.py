"""Tier 2 coder worker for checklist-driven code generation."""

from __future__ import annotations

import inspect
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from loop_troop.core.adr_loader import ADRLoader
from loop_troop.core.context_hydrator import ContextHydrator
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel, GitHubPullRequest
from loop_troop.core.llm_client import LLMClient
from loop_troop.core.schemas import CodePatch, ConflictResolution, FileChange, TargetExecutionProfile
from loop_troop.core.workspace_manager import WorkspaceManager
from loop_troop.docker_sandbox import DockerSandbox, SandboxResult
from loop_troop.execution import WorkerTier

from .dispatcher import WorkflowLabel

CODER_PROMPT = (
    "You are the Tier 2 Loop Troop coder. Complete exactly one checklist item. "
    "Return only a valid CodePatch. Use the provided branch name exactly. "
    "Keep the patch scoped to the selected checklist item and provide full file contents in files_changed."
)
ARCHITECT_PLAN_HEADING = "## Architect Plan"
CHECKLIST_ITEM_PATTERN = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX!])\]\s+(?P<text>.+)$")
FILES_PATTERN = re.compile(r"^\s*-\s*Files:\s*(?P<files>.+)$")
TESTS_PATTERN = re.compile(r"^\s*-\s*Tests required:\s*(?P<tests>.+)$")
CHECKLIST_STATE_PATTERN = re.compile(r"^(\s*[-*]\s*\[)[ xX!](\]\s+.+)$")

# 8B error extraction: truncate raw output to ~500 tokens before sending to the small model,
# then use its ~200-token summary in the 35B fix prompt.
_ERROR_EXTRACTION_INPUT_MAX_CHARS = 2000
_ERROR_SUMMARY_MAX_CHARS = 800

_TAUTOLOGICAL_TEST_MESSAGE = (
    "This test passes without implementation — write a stricter test that validates "
    "the actual behavior described in the test instructions."
)


class CoderGitHubClient(Protocol):
    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue: ...

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]: ...

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        *,
        body: str,
    ) -> GitHubIssueComment: ...

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]: ...

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
    ) -> GitHubPullRequest: ...


class StructuredLLMClient(Protocol):
    def complete_structured(self, **kwargs: Any) -> Any: ...


class ErrorSummary(BaseModel):
    """Structured error summary produced by the 8B extraction subflow."""

    relevant_lines: list[str]
    error_type: str
    root_cause: str
    suggested_fix_area: str


@dataclass(frozen=True, slots=True)
class ParsedChecklistItem:
    comment_id: int
    comment_body: str
    item_index: int
    line_index: int
    description: str
    files_touched: tuple[str, ...]
    requires_test: bool
    test_instructions: str | None


@dataclass(frozen=True, slots=True)
class InnerLoopResult:
    success: bool
    mode: str
    failure_summary: str | None = None
    attempts: int = 1
    first_attempt_passed: bool = False
    total_sandbox_time_seconds: float = 0.0
    tdd_mode: bool = False
    tautological_test_rejections: int = 0
    final_status: str = "pass"
    final_code_patch: CodePatch | None = None


@dataclass(frozen=True, slots=True)
class CoderOutcome:
    issue_number: int
    checklist_item_index: int
    branch_name: str
    target_label: WorkflowLabel
    pr_number: int | None = None
    attempts: int = 1


class InnerLoop:
    """Orchestrates the generate-test-fix cycle inside the Docker sandbox.

    Supports two execution modes:
    - **Standard mode** (``requires_test: false``): apply code, run tests, retry with
      LLM-generated fixes up to ``max_iterations`` times.
    - **TDD mode** (``requires_test: true``): two-phase Red-Green pipeline.
      Phase 1 asserts the test *fails* without implementation (catching tautological
      tests).  Phase 2 asserts the test *passes* with the full implementation, retrying
      up to ``max_iterations`` times.

    When the sandbox returns a failing test output an **8B model subflow** extracts
    the relevant error lines before they are fed back into the fix prompt for the 35B
    model.
    """

    # Directory names that indicate a path is a test directory.
    _TEST_DIRS: frozenset[str] = frozenset({"tests", "test"})
    # Filename prefixes / suffixes that identify test files regardless of extension.
    _TEST_FILENAME_PREFIX: str = "test_"
    _TEST_FILENAME_SUFFIX: str = "_test."

    def __init__(
        self,
        *,
        docker_sandbox: DockerSandbox | None = None,
        llm_client: StructuredLLMClient | None = None,
        max_iterations: int = 3,
        runner: Any = subprocess.run,
        error_extraction_model_override: str | None = None,
        fix_model_override: str | None = None,
    ) -> None:
        self._docker_sandbox = docker_sandbox
        self._llm_client = llm_client
        self._max_iterations = max_iterations
        self._runner = runner
        self._error_extraction_model_override = error_extraction_model_override
        self._fix_model_override = fix_model_override

    async def run(
        self,
        *,
        repo_path: str | Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        """Run the inner loop for the given checklist item and initial code patch."""
        repo = Path(repo_path)
        if checklist_item.requires_test:
            return await self._run_tdd(
                repo_path=repo,
                checklist_item=checklist_item,
                code_patch=code_patch,
            )
        return await self._run_standard(
            repo_path=repo,
            checklist_item=checklist_item,
            code_patch=code_patch,
        )

    # ------------------------------------------------------------------
    # Standard mode
    # ------------------------------------------------------------------

    async def _run_standard(
        self,
        *,
        repo_path: Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        current_patch = code_patch
        total_time = 0.0
        last_error_summary: str = "Build/test cycle failed."

        for attempt in range(1, self._max_iterations + 1):
            if attempt > 1:
                self._apply_files(repo_path, current_patch)

            result = self._execute_test(repo_path, current_patch.test_command)
            total_time += result.duration_seconds

            if result.timed_out:
                return InnerLoopResult(
                    success=False,
                    mode="standard",
                    failure_summary="Sandbox timed out.",
                    attempts=attempt,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=False,
                    tautological_test_rejections=0,
                    final_status="fail",
                    final_code_patch=current_patch,
                )

            if result.exit_code == 0:
                return InnerLoopResult(
                    success=True,
                    mode="standard",
                    attempts=attempt,
                    first_attempt_passed=(attempt == 1),
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=False,
                    tautological_test_rejections=0,
                    final_status="pass",
                    final_code_patch=current_patch,
                )

            last_error_summary = await self._extract_error_summary(result)

            if attempt < self._max_iterations and self._llm_client is not None:
                try:
                    current_patch = self._generate_fix_patch(
                        checklist_item=checklist_item,
                        error_summary=last_error_summary,
                        code_patch=current_patch,
                    )
                except Exception:
                    return InnerLoopResult(
                        success=False,
                        mode="standard",
                        failure_summary=last_error_summary,
                        attempts=attempt,
                        first_attempt_passed=False,
                        total_sandbox_time_seconds=total_time,
                        tdd_mode=False,
                        tautological_test_rejections=0,
                        final_status="fail",
                        final_code_patch=current_patch,
                    )
            else:
                # No LLM client or last iteration — cannot generate a fix.
                return InnerLoopResult(
                    success=False,
                    mode="standard",
                    failure_summary=last_error_summary,
                    attempts=attempt,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=False,
                    tautological_test_rejections=0,
                    final_status="fail",
                    final_code_patch=current_patch,
                )

        # Should never reach here; all paths above return.
        return InnerLoopResult(  # pragma: no cover
            success=False,
            mode="standard",
            failure_summary=last_error_summary,
            attempts=self._max_iterations,
            first_attempt_passed=False,
            total_sandbox_time_seconds=total_time,
            tdd_mode=False,
            tautological_test_rejections=0,
            final_status="fail",
            final_code_patch=current_patch,
        )

    # ------------------------------------------------------------------
    # TDD mode
    # ------------------------------------------------------------------

    async def _run_tdd(
        self,
        *,
        repo_path: Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        test_files, impl_files = self._partition_files(code_patch)
        total_time = 0.0

        # ------------------------------------------------------------------
        # Phase 1 (Red): run tests with impl files emptied — they MUST fail.
        # ------------------------------------------------------------------
        if impl_files:
            for fc in impl_files:
                path = repo_path / fc.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("")

            phase1 = self._execute_test(repo_path, code_patch.test_command)
            total_time += phase1.duration_seconds

            # Restore impl files before doing anything else.
            for fc in impl_files:
                (repo_path / fc.path).write_text(fc.content)

            if phase1.timed_out:
                return InnerLoopResult(
                    success=False,
                    mode="tdd",
                    failure_summary="Sandbox timed out during TDD Red phase.",
                    attempts=1,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=True,
                    tautological_test_rejections=0,
                    final_status="fail",
                    final_code_patch=code_patch,
                )

            if phase1.exit_code == 0:
                # Test passes without implementation — tautological test.
                return InnerLoopResult(
                    success=False,
                    mode="tdd",
                    failure_summary=_TAUTOLOGICAL_TEST_MESSAGE,
                    attempts=1,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=True,
                    tautological_test_rejections=1,
                    final_status="fail",
                    final_code_patch=code_patch,
                )

        # ------------------------------------------------------------------
        # Phase 2 (Green): run tests with the full implementation — they MUST pass.
        # ------------------------------------------------------------------
        current_patch = code_patch
        last_error_summary: str = "Build/test cycle failed."
        for attempt in range(1, self._max_iterations + 1):
            if attempt > 1:
                self._apply_files(repo_path, current_patch)

            phase2 = self._execute_test(repo_path, current_patch.test_command)
            total_time += phase2.duration_seconds

            if phase2.timed_out:
                return InnerLoopResult(
                    success=False,
                    mode="tdd",
                    failure_summary="Sandbox timed out during TDD Green phase.",
                    attempts=attempt,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=True,
                    tautological_test_rejections=0,
                    final_status="fail",
                    final_code_patch=current_patch,
                )

            if phase2.exit_code == 0:
                return InnerLoopResult(
                    success=True,
                    mode="tdd",
                    attempts=attempt,
                    first_attempt_passed=(attempt == 1),
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=True,
                    tautological_test_rejections=0,
                    final_status="pass",
                    final_code_patch=current_patch,
                )

            last_error_summary = await self._extract_error_summary(phase2)

            if attempt < self._max_iterations and self._llm_client is not None:
                try:
                    current_patch = self._generate_fix_patch(
                        checklist_item=checklist_item,
                        error_summary=last_error_summary,
                        code_patch=current_patch,
                    )
                except Exception:
                    return InnerLoopResult(
                        success=False,
                        mode="tdd",
                        failure_summary=last_error_summary,
                        attempts=attempt,
                        first_attempt_passed=False,
                        total_sandbox_time_seconds=total_time,
                        tdd_mode=True,
                        tautological_test_rejections=0,
                        final_status="fail",
                        final_code_patch=current_patch,
                    )
            else:
                # No LLM client or last iteration — cannot generate a fix.
                return InnerLoopResult(
                    success=False,
                    mode="tdd",
                    failure_summary=last_error_summary,
                    attempts=attempt,
                    first_attempt_passed=False,
                    total_sandbox_time_seconds=total_time,
                    tdd_mode=True,
                    tautological_test_rejections=0,
                    final_status="fail",
                    final_code_patch=current_patch,
                )

        # Should never reach here; all paths above return.
        return InnerLoopResult(  # pragma: no cover
            success=False,
            mode="tdd",
            failure_summary=last_error_summary,
            attempts=self._max_iterations,
            first_attempt_passed=False,
            total_sandbox_time_seconds=total_time,
            tdd_mode=True,
            tautological_test_rejections=0,
            final_status="fail",
            final_code_patch=current_patch,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _execute_test(self, repo_path: Path, test_command: str) -> SandboxResult:
        """Run *test_command* via the Docker sandbox (or subprocess fallback)."""
        args = shlex.split(test_command)
        if self._docker_sandbox is not None:
            return self._docker_sandbox.run(args)

        start = time.monotonic()
        try:
            completed = self._runner(
                args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
                env={},
            )
            duration = time.monotonic() - start
            return SandboxResult(
                exit_code=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                duration_seconds=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            stdout = exc.stdout
            stderr = exc.stderr
            return SandboxResult(
                exit_code=-1,
                stdout=(stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout or ""),
                stderr=(stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr or ""),
                duration_seconds=duration,
                timed_out=True,
            )

    async def _extract_error_summary(self, result: SandboxResult) -> str:
        """Use the 8B model to extract relevant error lines from raw sandbox output."""
        raw = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()

        if not self._llm_client or not raw:
            return raw[:_ERROR_EXTRACTION_INPUT_MAX_CHARS] or "Build/test cycle failed."

        truncated = raw[:_ERROR_EXTRACTION_INPUT_MAX_CHARS]
        try:
            extracted: ErrorSummary = self._llm_client.complete_structured(
                tier=WorkerTier.T1,
                response_model=ErrorSummary,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are an error analysis assistant. "
                            "Extract the key error information from the test output."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Test output:\n\n{truncated}\n\nExtract the error summary.",
                    },
                ],
                model_override=self._error_extraction_model_override,
                temperature=0,
            )
            lines = [
                f"Error type: {extracted.error_type}",
                f"Root cause: {extracted.root_cause}",
                f"Suggested fix area: {extracted.suggested_fix_area}",
            ]
            if extracted.relevant_lines:
                lines.append("Relevant lines:")
                lines.extend(f"  {line}" for line in extracted.relevant_lines[:10])
            return "\n".join(lines)[:_ERROR_SUMMARY_MAX_CHARS]
        except Exception:
            return truncated or "Build/test cycle failed."

    def _generate_fix_patch(
        self,
        *,
        checklist_item: ParsedChecklistItem,
        error_summary: str,
        code_patch: CodePatch,
    ) -> CodePatch:
        """Call the 35B model to generate a corrected code patch."""
        files_listing = "\n\n".join(
            f"=== {fc.path} ===\n{fc.content}" for fc in code_patch.files_changed
        )
        files_scope = ", ".join(checklist_item.files_touched) or "all files in the patch"
        fix_prompt = (
            f"Your previous code failed. Here is the error summary:\n"
            f"{error_summary[:_ERROR_SUMMARY_MAX_CHARS]}\n\n"
            f"Here is your code:\n{files_listing}\n\n"
            f"Fix the code to pass the tests. "
            f"Only modify the files listed in the checklist item: {files_scope}."
        )
        return self._llm_client.complete_structured(  # type: ignore[union-attr]
            tier=WorkerTier.T2,
            response_model=CodePatch,
            messages=[
                {"role": "system", "content": CODER_PROMPT},
                {"role": "user", "content": fix_prompt},
            ],
            model_override=self._fix_model_override,
            temperature=0,
        )

    @staticmethod
    def _apply_files(repo_path: Path, code_patch: CodePatch) -> None:
        """Write all files from *code_patch* to *repo_path*."""
        for fc in code_patch.files_changed:
            path = repo_path / fc.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fc.content)

    @staticmethod
    def _is_test_file(path: str) -> bool:
        """Return True if *path* follows a standard test-file naming convention.

        Matches ``test_*`` prefixes, ``*_test.*`` suffixes (language-agnostic), and
        files nested under a ``tests/`` or ``test/`` directory.
        """
        parts = path.replace("\\", "/").split("/")
        filename = parts[-1]
        return (
            filename.startswith(InnerLoop._TEST_FILENAME_PREFIX)
            or (InnerLoop._TEST_FILENAME_SUFFIX in filename)
            or any(part in InnerLoop._TEST_DIRS for part in parts[:-1])
        )

    @staticmethod
    def _partition_files(
        code_patch: CodePatch,
    ) -> tuple[list[Any], list[Any]]:
        """Split *code_patch.files_changed* into (test_files, impl_files)."""
        test_files = []
        impl_files = []
        for fc in code_patch.files_changed:
            if InnerLoop._is_test_file(fc.path):
                test_files.append(fc)
            else:
                impl_files.append(fc)
        return test_files, impl_files


class PRManager:
    def __init__(self, *, github_client: CoderGitHubClient) -> None:
        self._github_client = github_client

    async def open_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: list[str],
    ) -> GitHubPullRequest:
        pull_request = await self._github_client.create_pull_request(
            owner,
            repo,
            title=title,
            body=body,
            head=head,
            base=base,
        )
        await self._github_client.replace_issue_labels(owner, repo, pull_request.number, labels=labels)
        return pull_request


class CoderWorker:
    def __init__(
        self,
        *,
        github_client: CoderGitHubClient,
        llm_client: StructuredLLMClient | None = None,
        context_hydrator: ContextHydrator | None = None,
        adr_loader: ADRLoader | None = None,
        workspace_manager: WorkspaceManager | None = None,
        inner_loop: InnerLoop | None = None,
        pr_manager: PRManager | None = None,
        validation_retries: int = 3,
        max_retries: int = 3,
    ) -> None:
        self._github_client = github_client
        self._llm_client = llm_client or LLMClient()
        self._context_hydrator = context_hydrator or ContextHydrator()
        self._adr_loader = adr_loader or ADRLoader()
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._inner_loop = inner_loop or InnerLoop()
        self._pr_manager = pr_manager or PRManager(github_client=github_client)
        self._validation_retries = validation_retries
        self._max_retries = max_retries

    async def handle_issue(
        self,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        repo_path: str | Path,
        target_execution_profile: TargetExecutionProfile | None = None,
    ) -> CoderOutcome:
        issue = await self._github_client.get_issue(owner, repo, issue_number)
        if self._workflow_label(issue) is not WorkflowLabel.READY:
            raise ValueError(f"Issue #{issue_number} does not have the Coder label.")

        comments = await self._github_client.list_issue_comments(owner, repo, issue_number)
        checklist_item = self._first_unchecked_item(comments)
        branch_name = f"loop/issue-{issue.number}-item-{checklist_item.item_index}"
        base_branch = self._workspace_manager.current_branch(repo_path)
        self._workspace_manager.create_branch(repo_path, branch_name)

        issue_context = self._format_issue_context(issue=issue, checklist_item=checklist_item)
        adr_context = self._adr_loader.build_context(repo_path)
        hydrated_context = self._context_hydrator.hydrate(
            repo_path=repo_path,
            issue_context=issue_context,
            adr_context=adr_context,
            focus_files=list(checklist_item.files_touched),
        )

        last_failure: str | None = None
        for attempt in range(1, self._max_retries + 1):
            code_patch = self._generate_code_patch(
                issue=issue,
                checklist_item=checklist_item,
                hydrated_context=hydrated_context,
                branch_name=branch_name,
                model_override=target_execution_profile.model_name if target_execution_profile else None,
                failure_feedback=last_failure,
            )
            self._validate_code_patch(
                code_patch=code_patch,
                issue_number=issue.number,
                checklist_item_index=checklist_item.item_index,
            )
            self._apply_code_patch(repo_path=repo_path, code_patch=code_patch)
            self._workspace_manager.commit_all(repo_path, code_patch.commit_message)

            inner_loop_result = await self._run_inner_loop(
                repo_path=repo_path,
                checklist_item=checklist_item,
                code_patch=code_patch,
            )
            if inner_loop_result.success:
                # If InnerLoop generated a fix internally (attempts > 1), apply and
                # commit the final patch so the branch reflects the fixed state.
                final_patch = inner_loop_result.final_code_patch
                if final_patch is not None and inner_loop_result.attempts > 1:
                    self._apply_code_patch(repo_path=repo_path, code_patch=final_patch)
                    self._workspace_manager.commit_all(repo_path, final_patch.commit_message)

                self._workspace_manager.push_branch(repo_path, branch_name)
                pull_request = await self._pr_manager.open_pull_request(
                    owner=owner,
                    repo=repo,
                    title=code_patch.commit_message,
                    body=self._render_pull_request_body(issue=issue, checklist_item=checklist_item),
                    head=branch_name,
                    base=base_branch,
                    labels=[WorkflowLabel.NEEDS_REVIEW.value],
                )
                await self._github_client.update_issue_comment(
                    owner,
                    repo,
                    checklist_item.comment_id,
                    body=self._set_checklist_state(checklist_item.comment_body, checklist_item.line_index, "x"),
                )
                return CoderOutcome(
                    issue_number=issue.number,
                    checklist_item_index=checklist_item.item_index,
                    branch_name=branch_name,
                    target_label=WorkflowLabel.NEEDS_REVIEW,
                    pr_number=pull_request.number,
                    attempts=attempt,
                )

            last_failure = inner_loop_result.failure_summary or "Build/test cycle failed."

        await self._github_client.update_issue_comment(
            owner,
            repo,
            checklist_item.comment_id,
            body=self._set_checklist_state(checklist_item.comment_body, checklist_item.line_index, "!"),
        )
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            issue.number,
            labels=self._updated_labels(issue.labels, WorkflowLabel.NEEDS_HELP),
        )
        return CoderOutcome(
            issue_number=issue.number,
            checklist_item_index=checklist_item.item_index,
            branch_name=branch_name,
            target_label=WorkflowLabel.NEEDS_HELP,
            attempts=self._max_retries,
        )

    def _generate_code_patch(
        self,
        *,
        issue: GitHubIssue,
        checklist_item: ParsedChecklistItem,
        hydrated_context: str,
        branch_name: str,
        model_override: str | None,
        failure_feedback: str | None,
    ) -> CodePatch:
        messages = [
            {"role": "system", "content": CODER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Repository issue: #{issue.number}\n"
                    f"Checklist item index: {checklist_item.item_index}\n"
                    f"Branch name: {branch_name}\n"
                    "Return a CodePatch for this single checklist item.\n\n"
                    f"{hydrated_context}"
                ),
            },
        ]
        if failure_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous implementation failed the build/test cycle. "
                        f"Address this failure and return a corrected CodePatch:\n{failure_feedback}"
                    ),
                }
            )
        return self._complete_with_validation_feedback(messages=messages, model_override=model_override)

    def _complete_with_validation_feedback(
        self,
        *,
        messages: list[dict[str, str]],
        model_override: str | None,
    ) -> CodePatch:
        retry_messages = list(messages)
        for attempt in range(self._validation_retries):
            try:
                return self._llm_client.complete_structured(
                    tier=WorkerTier.T2,
                    response_model=CodePatch,
                    messages=retry_messages,
                    model_override=model_override,
                    temperature=0,
                )
            except ValidationError as exc:
                if attempt >= self._validation_retries - 1:
                    raise
                retry_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed schema validation. "
                            f"Fix these errors and return a corrected JSON object only:\n{exc}"
                        ),
                    },
                ]
        raise RuntimeError("Validation retry loop exhausted unexpectedly.")

    async def _run_inner_loop(
        self,
        *,
        repo_path: str | Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        result = self._inner_loop.run(
            repo_path=repo_path,
            checklist_item=checklist_item,
            code_patch=code_patch,
        )
        if inspect.isawaitable(result):
            resolved = await result
            if not isinstance(resolved, InnerLoopResult):
                raise TypeError("InnerLoop.run must return InnerLoopResult.")
            return resolved
        if not isinstance(result, InnerLoopResult):
            raise TypeError("InnerLoop.run must return InnerLoopResult.")
        return result

    def _apply_code_patch(self, *, repo_path: str | Path, code_patch: CodePatch) -> None:
        if not code_patch.files_changed:
            raise ValueError("CodePatch must include at least one file change.")
        for file_change in code_patch.files_changed:
            self._workspace_manager.write_file(repo_path, file_change.path, file_change.content)

    @staticmethod
    def _validate_code_patch(
        *,
        code_patch: CodePatch,
        issue_number: int,
        checklist_item_index: int,
    ) -> None:
        if code_patch.issue_number != issue_number:
            raise ValueError(
                f"Code patch referenced issue #{code_patch.issue_number}, expected #{issue_number}."
            )
        if code_patch.checklist_item_index != checklist_item_index:
            raise ValueError(
                "Code patch referenced checklist item "
                f"#{code_patch.checklist_item_index}, expected #{checklist_item_index}."
            )

    @staticmethod
    def _format_issue_context(*, issue: GitHubIssue, checklist_item: ParsedChecklistItem) -> str:
        lines = [
            f"Issue #{issue.number}: {issue.title or ''}".rstrip(),
            "",
            "Selected Checklist Item:",
            f"- {checklist_item.description}",
        ]
        if checklist_item.files_touched:
            lines.extend(["Files:", *[f"- `{path}`" for path in checklist_item.files_touched]])
        if checklist_item.requires_test and checklist_item.test_instructions:
            lines.extend(["Tests required:", checklist_item.test_instructions])
        else:
            lines.extend(["Tests required:", "No"])
        lines.extend(["", "Issue Body:", (issue.body or "").strip() or "(empty)"])
        return "\n".join(lines)

    @staticmethod
    def _render_pull_request_body(
        *,
        issue: GitHubIssue,
        checklist_item: ParsedChecklistItem,
    ) -> str:
        return "\n".join(
            [
                f"Closes #{issue.number}",
                "",
                f"Checklist item {checklist_item.item_index}: {checklist_item.description}",
            ]
        )

    @staticmethod
    def _first_unchecked_item(comments: list[GitHubIssueComment]) -> ParsedChecklistItem:
        for comment in reversed(comments):
            parsed = CoderWorker._parse_architect_checklist_comment(comment)
            if parsed is not None:
                return parsed
        raise ValueError("No unchecked Architect checklist item found.")

    @staticmethod
    def _parse_architect_checklist_comment(comment: GitHubIssueComment) -> ParsedChecklistItem | None:
        body = comment.body or ""
        if ARCHITECT_PLAN_HEADING not in body:
            return None

        lines = body.splitlines()
        item_index = 0
        items: list[dict[str, Any]] = []
        current_item: dict[str, Any] | None = None
        for line_index, line in enumerate(lines):
            checklist_match = CHECKLIST_ITEM_PATTERN.match(line)
            if checklist_match:
                item_index += 1
                current_item = {
                    "comment_id": comment.id,
                    "comment_body": body,
                    "item_index": item_index,
                    "line_index": line_index,
                    "description": checklist_match.group("text").strip(),
                    "state": checklist_match.group("state"),
                    "files_touched": [],
                    "requires_test": False,
                    "test_instructions": None,
                }
                items.append(current_item)
                continue
            if current_item is None:
                continue

            files_match = FILES_PATTERN.match(line)
            if files_match:
                current_item["files_touched"] = CoderWorker._parse_files(files_match.group("files"))
                continue

            tests_match = TESTS_PATTERN.match(line)
            if tests_match:
                test_value = tests_match.group("tests").strip()
                current_item["requires_test"] = test_value.lower() != "no"
                current_item["test_instructions"] = None if test_value.lower() == "no" else test_value

        for item in items:
            if item["state"] == " ":
                return ParsedChecklistItem(
                    comment_id=item["comment_id"],
                    comment_body=item["comment_body"],
                    item_index=item["item_index"],
                    line_index=item["line_index"],
                    description=item["description"],
                    files_touched=tuple(item["files_touched"]),
                    requires_test=bool(item["requires_test"]),
                    test_instructions=item["test_instructions"],
                )
        return None

    @staticmethod
    def _parse_files(raw_files: str) -> list[str]:
        matches = re.findall(r"`([^`]+)`", raw_files)
        if matches:
            return matches
        return [item.strip() for item in raw_files.split(",") if item.strip()]

    @staticmethod
    def _set_checklist_state(comment_body: str, line_index: int, state: str) -> str:
        lines = comment_body.splitlines()
        lines[line_index] = CHECKLIST_STATE_PATTERN.sub(rf"\1{state}\2", lines[line_index], count=1)
        return "\n".join(lines)

    @staticmethod
    def _workflow_label(issue: GitHubIssue) -> WorkflowLabel | None:
        for label in issue.labels:
            if label.name in WorkflowLabel._value2member_map_:
                return WorkflowLabel(label.name)
        return None

    @staticmethod
    def _updated_labels(labels: list[GitHubLabel], target_label: WorkflowLabel) -> list[str]:
        updated = [label.name for label in labels if label.name not in WorkflowLabel._value2member_map_]
        updated.append(target_label.value)
        return updated


CONFLICT_RESOLUTION_PROMPT = (
    "You are the Loop Troop conflict resolver. "
    "A git merge has produced conflicts in one or more files. "
    "Produce resolved file contents that correctly integrate both versions, "
    "preserving the intended behavior described in the checklist item. "
    "Return only a valid ConflictResolution with full resolved file contents."
)

_CONFLICT_RESOLUTION_COMMIT_MSG = "fix: resolve merge conflicts"


class ConflictResolverGitHubClient(Protocol):
    async def get_pull_request(self, owner: str, repo: str, pr_number: int) -> GitHubPullRequest: ...

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]: ...

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class ConflictResolverOutcome:
    pr_number: int
    issue_number: int
    branch_name: str
    target_label: WorkflowLabel
    conflicts_resolved: int = 0


class ConflictResolver:
    """Detects and resolves git merge conflicts for a feature branch.

    Triggered when a PR is labelled ``loop: merge-conflict``.  The resolver:

    1. Performs ``git fetch`` + ``git merge <base_branch>`` in the workspace.
    2. If the merge is clean it pushes and relabels the PR as ``loop: needs-review``.
    3. For conflicting files it calls the 35B model with both file versions and the
       original checklist item to produce a :class:`ConflictResolution`.
    4. Applies the resolved files, commits the merge, and re-runs tests via
       :class:`InnerLoop`.
    5. On test success: pushes and removes ``loop: merge-conflict``.
       On test failure: escalates to ``loop: needs-help``.

    All git operations are executed with ``cwd=<repo_path>`` — ``os.chdir()`` is
    never called.
    """

    def __init__(
        self,
        *,
        github_client: ConflictResolverGitHubClient,
        llm_client: StructuredLLMClient | None = None,
        context_hydrator: ContextHydrator | None = None,
        workspace_manager: WorkspaceManager | None = None,
        inner_loop: InnerLoop | None = None,
        runner: Any = subprocess.run,
        model_override: str | None = None,
    ) -> None:
        self._github_client = github_client
        self._llm_client = llm_client or LLMClient()
        self._context_hydrator = context_hydrator or ContextHydrator()
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._inner_loop = inner_loop or InnerLoop()
        self._runner = runner
        self._model_override = model_override

    async def resolve(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        issue_number: int,
        repo_path: str | Path,
        base_branch: str = "main",
        test_command: str = "make test",
    ) -> ConflictResolverOutcome:
        """Resolve merge conflicts in *repo_path* and push the result.

        Parameters
        ----------
        owner:
            Repository owner (GitHub organisation or user).
        repo:
            Repository name.
        pr_number:
            Pull-request number labelled ``loop: merge-conflict``.
        issue_number:
            The tracking issue number that owns the checklist comments.
        repo_path:
            Absolute path to the managed workspace directory.
        base_branch:
            The branch to merge into the feature branch (default: ``"main"``).
        test_command:
            Shell command used to verify the resolved code (default: ``"make test"``).
        """
        workspace = Path(repo_path)

        pull_request = await self._github_client.get_pull_request(owner, repo, pr_number)
        if pull_request.head and pull_request.head.ref:
            branch_name = pull_request.head.ref
        else:
            branch_name = base_branch

        comments = await self._github_client.list_issue_comments(owner, repo, issue_number)
        checklist_item = CoderWorker._first_unchecked_item(comments)

        # ------------------------------------------------------------------
        # Attempt the merge; detect conflicts.
        # ------------------------------------------------------------------
        self._run_git(["git", "fetch", "origin", base_branch], cwd=workspace)
        merge_result = self._runner(
            ["git", "merge", f"origin/{base_branch}"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )

        if merge_result.returncode == 0:
            # Clean merge — push and relabel.
            self._workspace_manager.push_branch(workspace, branch_name)
            labels = self._updated_labels(pull_request.labels, WorkflowLabel.NEEDS_REVIEW)
            await self._github_client.replace_issue_labels(owner, repo, pr_number, labels=labels)
            return ConflictResolverOutcome(
                pr_number=pr_number,
                issue_number=issue_number,
                branch_name=branch_name,
                target_label=WorkflowLabel.NEEDS_REVIEW,
                conflicts_resolved=0,
            )

        # ------------------------------------------------------------------
        # Identify conflicting files.
        # ------------------------------------------------------------------
        conflict_files = self._detect_conflicts(workspace)
        if not conflict_files:
            # Merge failed for a non-conflict reason — abort and escalate.
            self._runner(
                ["git", "merge", "--abort"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            labels = self._updated_labels(pull_request.labels, WorkflowLabel.NEEDS_HELP)
            await self._github_client.replace_issue_labels(owner, repo, pr_number, labels=labels)
            return ConflictResolverOutcome(
                pr_number=pr_number,
                issue_number=issue_number,
                branch_name=branch_name,
                target_label=WorkflowLabel.NEEDS_HELP,
                conflicts_resolved=0,
            )

        # ------------------------------------------------------------------
        # Hydrate context and call the 35B model for a ConflictResolution.
        # ------------------------------------------------------------------
        file_contexts = [
            (
                f,
                self._read_conflict_version(workspace, f, stage=2),
                self._read_conflict_version(workspace, f, stage=3),
            )
            for f in conflict_files
        ]
        conflict_prompt = self._build_conflict_prompt(
            file_contexts=file_contexts,
            checklist_item=checklist_item,
        )
        hydrated_context = self._context_hydrator.hydrate(
            repo_path=workspace,
            issue_context=conflict_prompt,
            adr_context="",
            focus_files=conflict_files,
        )

        resolution: ConflictResolution = self._llm_client.complete_structured(
            tier=WorkerTier.T2,
            response_model=ConflictResolution,
            messages=[
                {"role": "system", "content": CONFLICT_RESOLUTION_PROMPT},
                {"role": "user", "content": hydrated_context},
            ],
            model_override=self._model_override,
            temperature=0,
        )

        # ------------------------------------------------------------------
        # Apply resolved files and commit the merge.
        # ------------------------------------------------------------------
        for resolved_file in resolution.resolved_files:
            file_path = workspace / resolved_file.path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(resolved_file.content)

        self._workspace_manager.commit_all(workspace, _CONFLICT_RESOLUTION_COMMIT_MSG)

        # ------------------------------------------------------------------
        # Re-run tests via InnerLoop.
        # ------------------------------------------------------------------
        resolved_file_changes = [
            FileChange(path=rf.path, content=rf.content) for rf in resolution.resolved_files
        ]
        conflict_patch = CodePatch(
            issue_number=issue_number,
            checklist_item_index=checklist_item.item_index,
            branch_name=branch_name,
            files_changed=resolved_file_changes,
            test_command=test_command,
            commit_message=_CONFLICT_RESOLUTION_COMMIT_MSG,
        )

        inner_loop_result = await self._run_inner_loop(
            repo_path=workspace,
            checklist_item=checklist_item,
            code_patch=conflict_patch,
        )

        if inner_loop_result.success:
            self._workspace_manager.push_branch(workspace, branch_name)
            labels = self._updated_labels(pull_request.labels, WorkflowLabel.NEEDS_REVIEW)
            await self._github_client.replace_issue_labels(owner, repo, pr_number, labels=labels)
            return ConflictResolverOutcome(
                pr_number=pr_number,
                issue_number=issue_number,
                branch_name=branch_name,
                target_label=WorkflowLabel.NEEDS_REVIEW,
                conflicts_resolved=len(conflict_files),
            )

        # Tests failed — escalate.
        labels = self._updated_labels(pull_request.labels, WorkflowLabel.NEEDS_HELP)
        await self._github_client.replace_issue_labels(owner, repo, pr_number, labels=labels)
        return ConflictResolverOutcome(
            pr_number=pr_number,
            issue_number=issue_number,
            branch_name=branch_name,
            target_label=WorkflowLabel.NEEDS_HELP,
            conflicts_resolved=len(conflict_files),
        )

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _detect_conflicts(self, repo_path: Path) -> list[str]:
        """Return a list of paths with unresolved merge conflicts."""
        result = self._runner(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return [f for f in result.stdout.strip().splitlines() if f]

    def _read_conflict_version(self, repo_path: Path, path: str, *, stage: int) -> str:
        """Read a specific conflict stage (2=ours, 3=theirs) from the git index."""
        result = self._runner(
            ["git", "show", f":{stage}:{path}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""

    def _run_git(self, args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return self._runner(args, cwd=cwd, capture_output=True, text=True, check=True)

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_conflict_prompt(
        *,
        file_contexts: list[tuple[str, str, str]],
        checklist_item: ParsedChecklistItem,
    ) -> str:
        """Format a conflict resolution prompt for the 35B model.

        *file_contexts* is a list of ``(path, ours_content, theirs_content)`` tuples.
        """
        parts: list[str] = [
            f"Intended behavior: {checklist_item.description}",
            "",
        ]
        for path, ours, theirs in file_contexts:
            parts += [
                f"=== {path} ===",
                f"Version A (ours):\n{ours}",
                f"Version B (theirs):\n{theirs}",
                "Produce the merged file content.",
                "",
            ]
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _updated_labels(labels: list[GitHubLabel], target_label: WorkflowLabel) -> list[str]:
        updated = [label.name for label in labels if label.name not in WorkflowLabel._value2member_map_]
        updated.append(target_label.value)
        return updated

    # ------------------------------------------------------------------
    # Async InnerLoop bridge (mirrors CoderWorker._run_inner_loop)
    # ------------------------------------------------------------------

    async def _run_inner_loop(
        self,
        *,
        repo_path: Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        result = self._inner_loop.run(
            repo_path=repo_path,
            checklist_item=checklist_item,
            code_patch=code_patch,
        )
        if inspect.isawaitable(result):
            resolved = await result
            if not isinstance(resolved, InnerLoopResult):
                raise TypeError("InnerLoop.run must return InnerLoopResult.")
            return resolved
        if not isinstance(result, InnerLoopResult):
            raise TypeError("InnerLoop.run must return InnerLoopResult.")
        return result


__all__ = [
    "CoderOutcome",
    "CoderWorker",
    "ConflictResolver",
    "ConflictResolverGitHubClient",
    "ConflictResolverOutcome",
    "ErrorSummary",
    "InnerLoop",
    "InnerLoopResult",
    "PRManager",
    "ParsedChecklistItem",
]
