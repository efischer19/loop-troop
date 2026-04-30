"""Ephemeral, network-isolated Docker sandbox for executing LLM-generated code."""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DOCKER_SOCKET = "/var/run/docker.sock"
_WORKSPACE_MOUNT_TARGET = "/workspace"


def _decode_output(value: bytes | str | None) -> str:
    """Decode subprocess output to a string, handling bytes, str, or None."""
    if isinstance(value, bytes):
        return value.decode()
    return value or ""


class SandboxConfig(BaseModel):
    """Resource and timeout configuration for a Docker sandbox execution."""

    memory_limit: str = Field(default="512m", min_length=1)
    cpu_limit: float = Field(default=1.0, gt=0)
    timeout_seconds: int = Field(default=300, gt=0)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """Structured result returned from a sandbox execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class DockerSocketMountError(ValueError):
    """Raised when the Docker socket would be mounted into the container."""


class DockerSandbox:
    """Executes commands inside ephemeral, network-isolated Docker containers.

    Enforces the air-gap security model (ADR-0001):
    - No network access (--network=none)
    - Docker socket is never mounted
    - No host environment variables are passed into the container
    - Container is auto-removed after execution (--rm)
    """

    def __init__(
        self,
        *,
        repo_path: str | Path,
        image_tag: str | None = None,
        config: SandboxConfig | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.image_tag = image_tag or f"loop-troop-sandbox-{self.repo_path.name}".lower()
        self.config = config or SandboxConfig()
        self._runner = runner

    def build_image(self) -> None:
        """Build the Docker image from the target repository's Dockerfile."""
        dockerfile = self.repo_path / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(f"No Dockerfile found at {dockerfile}")

        cmd = [
            "docker",
            "build",
            "--tag",
            self.image_tag,
            str(self.repo_path),
        ]
        logger.info("Building sandbox image: %s", " ".join(cmd))
        result = self._runner(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker build failed (exit {result.returncode}):\n{result.stderr}"
            )

    def run(self, command: list[str]) -> SandboxResult:
        """Run a command inside an ephemeral, network-isolated container.

        The container is launched with:
        - ``--network=none`` — no outbound or inbound network access
        - ``--rm`` — auto-removed after the process exits
        - ``--memory`` / ``--cpus`` — resource limits from SandboxConfig
        - The repo directory bind-mounted read-write at /workspace
        - A clean environment (no host env vars forwarded)

        Raises:
            DockerSocketMountError: if the constructed command would mount the Docker socket.
        """
        cmd = self._build_run_command(command)
        self._assert_no_docker_socket(cmd)

        logger.info("Running sandbox command: %s", " ".join(cmd))
        start = time.monotonic()
        try:
            result = self._runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
                env={},
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return SandboxResult(
                exit_code=-1,
                stdout=_decode_output(exc.stdout),
                stderr=_decode_output(exc.stderr),
                duration_seconds=duration,
                timed_out=True,
            )

        duration = time.monotonic() - start
        return SandboxResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration,
            timed_out=False,
        )

    def _build_run_command(self, command: list[str]) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network=none",
            f"--memory={self.config.memory_limit}",
            f"--cpus={self.config.cpu_limit}",
            "--volume",
            f"{self.repo_path}:{_WORKSPACE_MOUNT_TARGET}",
            "--workdir",
            _WORKSPACE_MOUNT_TARGET,
            self.image_tag,
            *command,
        ]

    @staticmethod
    def _assert_no_docker_socket(cmd: list[str]) -> None:
        """Defense-in-depth: abort if the Docker socket appears anywhere in the command."""
        for token in cmd:
            if _DOCKER_SOCKET in token:
                raise DockerSocketMountError(
                    f"Refusing to run: Docker socket '{_DOCKER_SOCKET}' must never be "
                    f"mounted into the sandbox container."
                )
