"""Unit tests for DockerSandbox."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from loop_troop.docker_sandbox import (
    DockerSocketMountError,
    DockerSandbox,
    SandboxConfig,
    SandboxResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sandbox(
    tmp_path: Path,
    *,
    responses: list[subprocess.CompletedProcess[str]] | None = None,
    timeout_after: int | None = None,
    config: SandboxConfig | None = None,
) -> DockerSandbox:
    """Return a DockerSandbox backed by a fake subprocess runner."""
    _responses = list(responses or [])

    def fake_runner(cmd, **kwargs):
        if timeout_after is not None:
            raise subprocess.TimeoutExpired(cmd, timeout_after, output=b"partial out", stderr=b"partial err")
        if not _responses:
            raise AssertionError(f"Unexpected subprocess call: {cmd}")
        return _responses.pop(0)

    return DockerSandbox(
        repo_path=tmp_path,
        image_tag="test-image:latest",
        config=config,
        runner=fake_runner,
    )


def _ok(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# SandboxResult
# ---------------------------------------------------------------------------

def test_sandbox_result_fields() -> None:
    result = SandboxResult(
        exit_code=0,
        stdout="hello",
        stderr="",
        duration_seconds=1.5,
        timed_out=False,
    )
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""
    assert result.duration_seconds == 1.5
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Successful execution
# ---------------------------------------------------------------------------

def test_run_returns_structured_result_on_success(tmp_path: Path) -> None:
    sandbox = _make_sandbox(tmp_path, responses=[_ok(stdout="output\n", stderr="warn\n")])

    result = sandbox.run(["echo", "hello"])

    assert isinstance(result, SandboxResult)
    assert result.exit_code == 0
    assert result.stdout == "output\n"
    assert result.stderr == "warn\n"
    assert result.timed_out is False
    assert result.duration_seconds >= 0.0


# ---------------------------------------------------------------------------
# Exit code propagation
# ---------------------------------------------------------------------------

def test_run_propagates_nonzero_exit_code(tmp_path: Path) -> None:
    sandbox = _make_sandbox(tmp_path, responses=[_ok(returncode=42)])

    result = sandbox.run(["false"])

    assert result.exit_code == 42
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------

def test_run_returns_timed_out_when_timeout_expires(tmp_path: Path) -> None:
    sandbox = _make_sandbox(
        tmp_path,
        timeout_after=5,
        config=SandboxConfig(timeout_seconds=5),
    )

    result = sandbox.run(["sleep", "9999"])

    assert result.timed_out is True
    assert result.exit_code == -1


def test_run_timeout_captures_partial_output(tmp_path: Path) -> None:
    sandbox = _make_sandbox(
        tmp_path,
        timeout_after=5,
        config=SandboxConfig(timeout_seconds=5),
    )

    result = sandbox.run(["sleep", "9999"])

    assert result.stdout == "partial out"
    assert result.stderr == "partial err"


# ---------------------------------------------------------------------------
# CRITICAL: Docker socket must never be mounted
# ---------------------------------------------------------------------------

def test_docker_socket_mount_is_rejected(tmp_path: Path) -> None:
    """The sandbox must refuse to run if the Docker socket appears in the command."""
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    # Manually inject a poisoned command containing the socket path and assert it is caught.
    poisoned_cmd = sandbox._build_run_command(["sh", "-c", "echo hi"])
    # Simulate an adversarial injection by inserting the socket path.
    poisoned_cmd = poisoned_cmd + ["--volume", "/var/run/docker.sock:/var/run/docker.sock"]

    with pytest.raises(DockerSocketMountError, match="/var/run/docker.sock"):
        DockerSandbox._assert_no_docker_socket(poisoned_cmd)


def test_normal_run_command_does_not_contain_docker_socket(tmp_path: Path) -> None:
    """The legitimately constructed run command must not reference the Docker socket."""
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    cmd = sandbox._build_run_command(["pytest"])

    for token in cmd:
        assert "/var/run/docker.sock" not in token, (
            f"Docker socket found in token: {token!r}"
        )


def test_run_rejects_docker_socket_in_constructed_command(tmp_path: Path) -> None:
    """_assert_no_docker_socket is called inside run(); verify it raises correctly."""

    called_with: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        called_with.append(list(cmd))
        raise AssertionError("runner should not be called when socket is detected")

    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest", runner=fake_runner)

    # Patch _build_run_command to return a poisoned command.
    original_build = sandbox._build_run_command

    def poisoned_build(command):
        cmd = original_build(command)
        return cmd + ["--volume", "/var/run/docker.sock:/var/run/docker.sock"]

    sandbox._build_run_command = poisoned_build  # type: ignore[method-assign]

    with pytest.raises(DockerSocketMountError):
        sandbox.run(["echo", "hi"])

    assert called_with == [], "subprocess runner must not be called when socket mount is detected"


# ---------------------------------------------------------------------------
# CRITICAL: No host environment variables must be leaked
# ---------------------------------------------------------------------------

def test_no_env_vars_leaked_into_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sandbox must pass env={} to subprocess so no host env vars reach the container."""
    monkeypatch.setenv("SECRET_TOKEN", "super-secret")
    monkeypatch.setenv("HOME", "/root")

    captured_kwargs: dict = {}

    def capturing_runner(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    sandbox = DockerSandbox(
        repo_path=tmp_path,
        image_tag="test-image:latest",
        runner=capturing_runner,
    )

    sandbox.run(["echo", "hi"])

    assert "env" in captured_kwargs, "subprocess.run must receive an explicit 'env' kwarg"
    assert captured_kwargs["env"] == {}, (
        f"env must be an empty dict; got {captured_kwargs['env']!r}"
    )


# ---------------------------------------------------------------------------
# --network=none flag
# ---------------------------------------------------------------------------

def test_run_command_includes_network_none(tmp_path: Path) -> None:
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    cmd = sandbox._build_run_command(["pytest"])

    assert "--network=none" in cmd


# ---------------------------------------------------------------------------
# --rm flag
# ---------------------------------------------------------------------------

def test_run_command_includes_rm(tmp_path: Path) -> None:
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    cmd = sandbox._build_run_command(["pytest"])

    assert "--rm" in cmd


# ---------------------------------------------------------------------------
# Workspace bind-mount
# ---------------------------------------------------------------------------

def test_run_command_mounts_repo_as_workspace(tmp_path: Path) -> None:
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    cmd = sandbox._build_run_command(["pytest"])

    volume_index = cmd.index("--volume")
    mount_spec = cmd[volume_index + 1]
    repo_resolved = str(tmp_path.resolve())
    assert mount_spec == f"{repo_resolved}:/workspace"


# ---------------------------------------------------------------------------
# Resource limits
# ---------------------------------------------------------------------------

def test_run_command_includes_resource_limits(tmp_path: Path) -> None:
    config = SandboxConfig(memory_limit="256m", cpu_limit=0.5)
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest", config=config)

    cmd = sandbox._build_run_command(["pytest"])

    assert "--memory=256m" in cmd
    assert "--cpus=0.5" in cmd


# ---------------------------------------------------------------------------
# build_image
# ---------------------------------------------------------------------------

def test_build_image_raises_when_dockerfile_missing(tmp_path: Path) -> None:
    sandbox = DockerSandbox(repo_path=tmp_path, image_tag="test-image:latest")

    with pytest.raises(FileNotFoundError, match="Dockerfile"):
        sandbox.build_image()


def test_build_image_raises_on_nonzero_exit(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    sandbox = _make_sandbox(
        tmp_path,
        responses=[_ok(returncode=1, stderr="build error")],
    )

    with pytest.raises(RuntimeError, match="docker build failed"):
        sandbox.build_image()


def test_build_image_succeeds(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    sandbox = _make_sandbox(tmp_path, responses=[_ok(returncode=0)])

    sandbox.build_image()  # should not raise


# ---------------------------------------------------------------------------
# Default timeout
# ---------------------------------------------------------------------------

def test_default_timeout_is_300_seconds() -> None:
    config = SandboxConfig()
    assert config.timeout_seconds == 300
