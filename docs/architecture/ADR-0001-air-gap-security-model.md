---
title: "ADR-0001: Air-Gap Security Model — Control Plane vs. Data Plane Separation"
status: "Accepted"
date: "2026-04-18"
tags:
  - "security"
  - "architecture"
  - "infrastructure"
---

## Context
Loop Troop orchestrates LLM-powered agents that generate and execute code autonomously. Allowing LLM-generated code unrestricted access to the host operating system is an unacceptable security risk. We need a strict execution boundary that prevents any LLM-influenced process from accessing host resources (filesystem, network, Docker socket, credentials).

## Decision
We adopt a two-plane architecture with a strict air-gap between them:

### 1. The Control Plane (Native Host)
- Runs **natively** on the Mac host OS via `uv` or `venv`.
- Handles: GitHub API polling, LLM API calls (Ollama), git operations, subprocess orchestration.
- Has access to: GitHub PAT, Ollama endpoints, Docker CLI (to *spawn* containers).
- Is **never containerized** itself. This prevents the Docker socket from being exposed inside any container that an LLM could influence.

### 2. The Data Plane (Ephemeral Docker Containers)
- All LLM-generated code execution (e.g., `make test`, `make lint`) happens **strictly inside ephemeral, network-isolated Docker containers**.
- Containers are spun up by the native Control Plane via `docker run` with:
  - `--network=none` (no network access)
  - `--read-only` on system paths (where appropriate)
  - Bind-mounted target repository as the only writable volume
  - No access to `/var/run/docker.sock` — **ever**
  - No access to host environment variables or credentials
- Containers are destroyed after each execution cycle.

## Critical Security Invariants
1. **No ticket, workflow, or code path may mount `/var/run/docker.sock` into any container.**
2. The LLM has zero access to the native host OS.
3. Docker containers for target code execution must use `--network=none`.
4. GitHub PAT and Ollama credentials are never passed into Data Plane containers.

## Platform ADRs vs. Project ADRs

Loop Troop distinguishes between two categories of Architecture Decision Records:

### Platform ADRs
- Stored in `docs/architecture/` **in the Loop Troop repository itself**.
- Govern the Loop Troop control plane's own architecture.
- These are **constants** — they apply universally to every target project Loop Troop operates on.
- Example: ADR-0001 (this document) — the air-gap security model.

### Project ADRs
- Stored in `docs/architecture/` **in each target repository**.
- Govern the target project's architecture.
- These **vary per-project** and are read by the 70B Architect and Reviewer workers when planning and reviewing changes.
- An example ADR template is provided in `docs/example/ADR-001-use_adrs.md`.

## Consequences
- The Control Plane must manage all Docker lifecycle operations natively.
- LLM-generated code cannot install packages that require network access at test time (dependencies must be baked into the sandbox Dockerfile).
- Debugging LLM-generated code requires inspecting container logs, not live shell access.
- This model adds subprocess management complexity to the Control Plane but eliminates an entire class of container-escape and credential-leakage attacks.
