"""Unit tests for InnerLoop — Build/Test Cycle with Red-Green TDD Pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loop_troop.coder import (
    ErrorSummary,
    InnerLoop,
    InnerLoopResult,
    ParsedChecklistItem,
)
from loop_troop.core.schemas import CodePatch, FileChange
from loop_troop.docker_sandbox import SandboxResult


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSandbox:
    """Fake DockerSandbox that returns pre-baked SandboxResults."""

    def __init__(self, results: list[SandboxResult]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def run(self, command: list[str]) -> SandboxResult:
        self.calls.append(list(command))
        return self._results.pop(0)


class FakeLLMClient:
    """Fake StructuredLLMClient returning pre-baked objects."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ok(stdout: str = "", stderr: str = "", duration: float = 0.1) -> SandboxResult:
    return SandboxResult(
        exit_code=0, stdout=stdout, stderr=stderr, duration_seconds=duration, timed_out=False
    )


def _fail(stdout: str = "", stderr: str = "tests failed", duration: float = 0.1) -> SandboxResult:
    return SandboxResult(
        exit_code=1, stdout=stdout, stderr=stderr, duration_seconds=duration, timed_out=False
    )


def _timeout(duration: float = 30.0) -> SandboxResult:
    return SandboxResult(
        exit_code=-1, stdout="", stderr="", duration_seconds=duration, timed_out=True
    )


def _standard_item() -> ParsedChecklistItem:
    return ParsedChecklistItem(
        comment_id=1,
        comment_body="",
        item_index=1,
        line_index=0,
        description="Implement the feature",
        files_touched=("src/app.py",),
        requires_test=False,
        test_instructions=None,
    )


def _tdd_item() -> ParsedChecklistItem:
    return ParsedChecklistItem(
        comment_id=1,
        comment_body="",
        item_index=1,
        line_index=0,
        description="Add foo() function",
        files_touched=("src/app.py", "tests/test_app.py"),
        requires_test=True,
        test_instructions="Write a test that calls foo() and asserts it returns 42.",
    )


def _make_patch(
    *,
    test_file: bool = False,
    impl_file: bool = True,
    test_content: str = "def test_foo(): assert foo() == 42",
    impl_content: str = "def foo(): return 42",
) -> CodePatch:
    files: list[FileChange] = []
    if test_file:
        files.append(FileChange(path="tests/test_app.py", content=test_content))
    if impl_file:
        files.append(FileChange(path="src/app.py", content=impl_content))
    return CodePatch(
        issue_number=1,
        checklist_item_index=1,
        branch_name="loop/issue-1-item-1",
        files_changed=files,
        test_command="python -m pytest tests/test_app.py",
        commit_message="feat: add foo",
    )


def _pre_create_files(tmp_path: Path, patch: CodePatch) -> None:
    """Write all patch files to tmp_path so InnerLoop can read/modify them."""
    for fc in patch.files_changed:
        path = tmp_path / fc.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fc.content)


# ---------------------------------------------------------------------------
# Standard mode — first-attempt pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_mode_first_attempt_pass(tmp_path: Path) -> None:
    sandbox = FakeSandbox([_ok(stdout="1 passed")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is True
    assert result.mode == "standard"
    assert result.attempts == 1
    assert result.first_attempt_passed is True
    assert result.tdd_mode is False
    assert result.tautological_test_rejections == 0
    assert result.final_status == "pass"
    assert result.total_sandbox_time_seconds >= 0.0
    assert len(sandbox.calls) == 1


# ---------------------------------------------------------------------------
# Standard mode — iterative fix (first attempt fails, second passes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_mode_iterative_fix(tmp_path: Path) -> None:
    fixed_patch = _make_patch(impl_content="def foo(): return 42  # fixed")
    error_extraction = ErrorSummary(
        relevant_lines=["AssertionError: 0 != 42"],
        error_type="AssertionError",
        root_cause="foo() returns 0 instead of 42",
        suggested_fix_area="src/app.py",
    )
    llm = FakeLLMClient([error_extraction, fixed_patch])
    sandbox = FakeSandbox([_fail(stderr="AssertionError"), _ok(stdout="1 passed")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, llm_client=llm, max_iterations=3)
    patch = _make_patch()
    item = _standard_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is True
    assert result.attempts == 2
    assert result.first_attempt_passed is False
    assert result.final_status == "pass"
    # 8B extraction was called first, then fix code generation
    assert len(llm.calls) == 2
    assert llm.calls[0]["response_model"] is ErrorSummary
    assert llm.calls[1]["response_model"] is CodePatch
    # Fix files were written to disk
    assert (tmp_path / "src" / "app.py").read_text() == "def foo(): return 42  # fixed"


# ---------------------------------------------------------------------------
# Standard mode — max-iteration exhaustion (all attempts fail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_mode_max_iteration_exhaustion(tmp_path: Path) -> None:
    error_extraction_1 = ErrorSummary(
        relevant_lines=["fail 1"],
        error_type="AssertionError",
        root_cause="wrong value",
        suggested_fix_area="src/app.py",
    )
    error_extraction_2 = ErrorSummary(
        relevant_lines=["fail 2"],
        error_type="AssertionError",
        root_cause="still wrong",
        suggested_fix_area="src/app.py",
    )
    fix_patch_1 = _make_patch(impl_content="def foo(): return 1")
    fix_patch_2 = _make_patch(impl_content="def foo(): return 2")
    # Order: extraction1, fix1, extraction2, fix2 — last run still fails, no fix generated
    llm = FakeLLMClient([error_extraction_1, fix_patch_1, error_extraction_2, fix_patch_2])
    sandbox = FakeSandbox(
        [
            _fail(stderr="fail 1"),
            _fail(stderr="fail 2"),
            _fail(stderr="fail 3"),
        ]
    )
    inner_loop = InnerLoop(docker_sandbox=sandbox, llm_client=llm, max_iterations=3)
    patch = _make_patch()
    item = _standard_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.attempts == 3
    assert result.first_attempt_passed is False
    assert result.final_status == "fail"
    assert result.failure_summary is not None
    # All 3 sandbox calls used
    assert len(sandbox.calls) == 3


# ---------------------------------------------------------------------------
# Sandbox timeout during inner loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_timeout_during_inner_loop(tmp_path: Path) -> None:
    sandbox = FakeSandbox([_timeout(duration=30.0)])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.failure_summary is not None
    assert "timed out" in result.failure_summary.lower()
    assert result.total_sandbox_time_seconds == pytest.approx(30.0)
    assert result.final_status == "fail"


# ---------------------------------------------------------------------------
# TDD Red phase — tautological test rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tdd_red_phase_tautological_rejection(tmp_path: Path) -> None:
    """Phase 1 accidentally passes (tests succeed without implementation) — reject it."""
    # Sandbox returns exit_code=0 for Phase 1 (tautological!)
    sandbox = FakeSandbox([_ok(stdout="1 passed")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch(test_file=True, impl_file=True)
    item = _tdd_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.tdd_mode is True
    assert result.tautological_test_rejections == 1
    assert result.final_status == "fail"
    assert "stricter test" in (result.failure_summary or "")
    # Only one sandbox call (Phase 1); never reached Phase 2
    assert len(sandbox.calls) == 1
    # Impl files restored to original content after Phase 1 check
    assert (tmp_path / "src" / "app.py").read_text() == "def foo(): return 42"


# ---------------------------------------------------------------------------
# TDD Green phase — standard retry (Phase 2 fails once then passes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tdd_green_phase_standard_retry(tmp_path: Path) -> None:
    """Phase 1 correctly fails (Red), Phase 2 fails first then passes on retry."""
    fixed_patch = _make_patch(
        test_file=True,
        impl_file=True,
        impl_content="def foo(): return 42  # fixed",
    )
    error_extraction = ErrorSummary(
        relevant_lines=["AssertionError: 0 != 42"],
        error_type="AssertionError",
        root_cause="incomplete implementation",
        suggested_fix_area="src/app.py",
    )
    llm = FakeLLMClient([error_extraction, fixed_patch])
    sandbox = FakeSandbox(
        [
            _fail(stderr="ImportError: no module"),  # Phase 1 Red — tests fail without impl ✓
            _fail(stderr="AssertionError"),           # Phase 2 attempt 1 — impl wrong
            _ok(stdout="1 passed"),                   # Phase 2 attempt 2 — impl fixed ✓
        ]
    )
    inner_loop = InnerLoop(docker_sandbox=sandbox, llm_client=llm, max_iterations=3)
    patch = _make_patch(test_file=True, impl_file=True)
    item = _tdd_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is True
    assert result.tdd_mode is True
    assert result.tautological_test_rejections == 0
    assert result.attempts == 2  # Two Green phase attempts
    assert result.first_attempt_passed is False
    assert result.final_status == "pass"
    # Sandbox was called three times: Phase1 + Green attempt 1 + Green attempt 2
    assert len(sandbox.calls) == 3
    # 8B extraction + fix generation
    assert len(llm.calls) == 2
    assert llm.calls[0]["response_model"] is ErrorSummary
    assert llm.calls[1]["response_model"] is CodePatch


# ---------------------------------------------------------------------------
# TDD — timeout during Red phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tdd_timeout_during_red_phase(tmp_path: Path) -> None:
    sandbox = FakeSandbox([_timeout(duration=15.0)])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch(test_file=True, impl_file=True)
    item = _tdd_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.tdd_mode is True
    assert "timed out" in (result.failure_summary or "").lower()
    assert result.total_sandbox_time_seconds == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# TDD — timeout during Green phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tdd_timeout_during_green_phase(tmp_path: Path) -> None:
    sandbox = FakeSandbox(
        [
            _fail(stderr="ImportError"),  # Phase 1 Red — good
            _timeout(duration=20.0),      # Phase 2 times out
        ]
    )
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch(test_file=True, impl_file=True)
    item = _tdd_item()
    _pre_create_files(tmp_path, patch)

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.tdd_mode is True
    assert "timed out" in (result.failure_summary or "").lower()
    assert result.total_sandbox_time_seconds > 0.0


# ---------------------------------------------------------------------------
# 8B error extraction subflow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_8b_error_extraction_subflow(tmp_path: Path) -> None:
    """Failing sandbox output is passed to the 8B model; the extracted summary
    (not the raw dump) ends up in the InnerLoopResult.failure_summary."""
    raw_output = "FAILED tests/test_app.py::test_foo\n" + "noise " * 200
    extracted = ErrorSummary(
        relevant_lines=["FAILED tests/test_app.py::test_foo - AssertionError"],
        error_type="AssertionError",
        root_cause="foo() returns 0 instead of 42",
        suggested_fix_area="src/app.py",
    )
    # No fix patch — only one iteration so we just extract errors and return failure.
    llm = FakeLLMClient([extracted])
    sandbox = FakeSandbox([_fail(stdout=raw_output, stderr="")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, llm_client=llm, max_iterations=1)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    # Verify 8B model was called with ErrorSummary as the response model
    assert len(llm.calls) == 1
    assert llm.calls[0]["response_model"] is ErrorSummary
    # Extracted summary is used, not the raw dump
    assert result.failure_summary is not None
    assert "AssertionError" in result.failure_summary
    assert "foo() returns 0" in result.failure_summary
    # Raw noise should not appear verbatim in the summary
    assert "noise noise noise" not in result.failure_summary


# ---------------------------------------------------------------------------
# 8B extraction — fallback when LLM raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_8b_extraction_falls_back_to_raw_output_on_llm_error(tmp_path: Path) -> None:
    llm = FakeLLMClient([RuntimeError("LLM unavailable")])
    sandbox = FakeSandbox([_fail(stderr="raw error text")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, llm_client=llm, max_iterations=1)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    # Falls back to raw output
    assert result.failure_summary == "raw error text"


# ---------------------------------------------------------------------------
# Standard mode — no LLM client: single-shot (no fix generation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_standard_mode_no_llm_client_single_shot(tmp_path: Path) -> None:
    """Without an LLM client the inner loop only runs once and returns failure."""
    sandbox = FakeSandbox([_fail(stderr="failed")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.attempts == 1
    # Only one sandbox call — no fix retries without LLM
    assert len(sandbox.calls) == 1


# ---------------------------------------------------------------------------
# Subprocess fallback (no docker_sandbox provided)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subprocess_fallback_success(tmp_path: Path) -> None:
    """InnerLoop uses the subprocess runner when no DockerSandbox is provided."""
    import subprocess

    calls: list[list[str]] = []

    def fake_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="1 passed", stderr="")

    inner_loop = InnerLoop(runner=fake_runner, max_iterations=1)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_subprocess_fallback_timeout(tmp_path: Path) -> None:
    """Subprocess TimeoutExpired is surfaced as a timed-out SandboxResult."""
    import subprocess

    def fake_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 5, output=b"partial", stderr=b"err")

    inner_loop = InnerLoop(runner=fake_runner, max_iterations=1)
    patch = _make_patch()
    item = _standard_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is False
    assert result.failure_summary is not None
    assert "timed out" in result.failure_summary.lower()


# ---------------------------------------------------------------------------
# TDD — patch with only test files (no impl files to empty for Phase 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tdd_no_impl_files_skips_red_phase(tmp_path: Path) -> None:
    """When code_patch has no impl files Phase 1 is skipped and Phase 2 runs directly."""
    sandbox = FakeSandbox([_ok(stdout="1 passed")])
    inner_loop = InnerLoop(docker_sandbox=sandbox, max_iterations=3)
    # Patch has only a test file — no impl files to empty
    patch = _make_patch(test_file=True, impl_file=False)
    item = _tdd_item()

    result = await inner_loop.run(repo_path=tmp_path, checklist_item=item, code_patch=patch)

    assert result.success is True
    assert result.tdd_mode is True
    assert result.tautological_test_rejections == 0
    assert len(sandbox.calls) == 1


# ---------------------------------------------------------------------------
# InnerLoopResult metrics are all present
# ---------------------------------------------------------------------------


def test_inner_loop_result_default_fields() -> None:
    result = InnerLoopResult(success=True, mode="standard")
    assert result.attempts == 1
    assert result.first_attempt_passed is False
    assert result.total_sandbox_time_seconds == 0.0
    assert result.tdd_mode is False
    assert result.tautological_test_rejections == 0
    assert result.final_status == "pass"
    assert result.final_code_patch is None
    assert result.failure_summary is None


# ---------------------------------------------------------------------------
# _is_test_file helper
# ---------------------------------------------------------------------------


def test_is_test_file_standard_patterns() -> None:
    assert InnerLoop._is_test_file("test_app.py") is True
    assert InnerLoop._is_test_file("app_test.py") is True
    assert InnerLoop._is_test_file("tests/test_foo.py") is True
    assert InnerLoop._is_test_file("test/conftest.py") is True
    # Language-agnostic: _test. suffix works for any extension
    assert InnerLoop._is_test_file("app_test.ts") is True
    assert InnerLoop._is_test_file("app_test.js") is True
    assert InnerLoop._is_test_file("src/app.py") is False
    assert InnerLoop._is_test_file("src/testing_utils.py") is False


# ---------------------------------------------------------------------------
# _partition_files helper
# ---------------------------------------------------------------------------


def test_partition_files_splits_correctly() -> None:
    patch = CodePatch(
        issue_number=1,
        checklist_item_index=1,
        branch_name="b",
        files_changed=[
            FileChange(path="tests/test_app.py", content=""),
            FileChange(path="src/app.py", content=""),
            FileChange(path="test_utils.py", content=""),
        ],
        test_command="pytest",
        commit_message="feat",
    )
    test_files, impl_files = InnerLoop._partition_files(patch)
    assert len(test_files) == 2
    assert len(impl_files) == 1
    assert impl_files[0].path == "src/app.py"


# ---------------------------------------------------------------------------
# ErrorSummary schema
# ---------------------------------------------------------------------------


def test_error_summary_schema() -> None:
    summary = ErrorSummary(
        relevant_lines=["line 1", "line 2"],
        error_type="AssertionError",
        root_cause="wrong return value",
        suggested_fix_area="src/app.py",
    )
    assert summary.error_type == "AssertionError"
    assert len(summary.relevant_lines) == 2
