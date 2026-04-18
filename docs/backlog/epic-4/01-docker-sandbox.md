# feat: Ephemeral Docker Sandbox — Network-Isolated Container Execution

## What do you want to build?

Build the Docker sandbox execution layer that runs LLM-generated code inside ephemeral, network-isolated containers. This is the most security-critical component in the system. The sandbox must enforce the air-gap security model (ADR-0001): the LLM-generated code runs in a container with no network access, no Docker socket access, and no host credential access.

## Acceptance Criteria

- [ ] A `DockerSandbox` class that executes commands inside ephemeral Docker containers.
- [ ] Containers are launched with: `--network=none` (no network access), `--rm` (auto-cleanup), resource limits (`--memory`, `--cpus`), and a configurable timeout.
- [ ] The target repository directory is bind-mounted as the sole writable volume (`-v /path/to/target:/workspace`).
- [ ] **CRITICAL: The Docker socket (`/var/run/docker.sock`) is NEVER mounted into the container.** This must be enforced by code (explicit check before container launch) and validated by tests.
- [ ] **CRITICAL: No host environment variables are passed into the container.** The `--env` flag is not used; the container runs with a clean environment.
- [ ] Container image is built from the target repo's `Dockerfile` (assumed to contain all build dependencies).
- [ ] Returns structured execution results: `exit_code`, `stdout`, `stderr`, `duration_seconds`, `timed_out` (bool).
- [ ] Enforces a maximum execution timeout (default: 300 seconds), killing the container if exceeded.
- [ ] Unit tests covering: successful execution, timeout enforcement, exit code propagation, **explicit test that Docker socket mount is rejected**, **explicit test that env vars are not leaked**.

## Implementation Notes (Optional)

Use `subprocess.run(["docker", "run", ...])` from the native Control Plane — NOT the Docker Python SDK (which would require the socket). Build the `docker run` command string programmatically and include an assertion/check that scans the final command for `/var/run/docker.sock` before execution — defense in depth. Log the full `docker run` command (minus any secrets) to the shadow log for auditability. The resource limits should be configurable per-project via the target repo's template config.
