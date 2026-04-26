# feat: Configuration, `.env` & Quickstart Guide

## What do you want to build?

Create the developer-facing configuration system, example environment file, and quickstart documentation that enables a new contributor to set up and run Loop Troop locally within 15 minutes. This includes consolidating all scattered environment variable references from previous epics into a single, well-documented configuration surface.

## Acceptance Criteria

- [ ] A `.env.example` file listing all required and optional environment variables with descriptions and safe defaults.
- [ ] A `Config` class (Pydantic `BaseSettings`) that loads configuration from environment variables and/or a `loop-troop.toml` file, with validation and helpful error messages on misconfiguration.
- [ ] All previous tickets' env var references (`GITHUB_PAT`, `OLLAMA_HOST`, `LOOP_TROOP_DB_PATH`, `LOOP_TROOP_POLL_INTERVAL`, model names, GitHub App config, etc.) are consolidated into this single `Config` class.
- [ ] Supports both PAT and GitHub App authentication modes, with clear validation errors if neither is configured.
- [ ] A `docs/quickstart.md` guide covering: prerequisites (Python 3.11+, Docker, Ollama, Node.js for Repomix), installation steps (`uv` or `pip`), configuration, first run with `--dry-run`, and a walkthrough of the shadow log.
- [ ] A `pyproject.toml` with project metadata, dependencies, and a `[project.scripts]` entry point for the `loop-troop` CLI command.
- [ ] The quickstart guide explicitly warns against running Loop Troop inside a Docker container (per ADR-0001) and explains why.
- [ ] Unit tests covering: Config loading from env vars, Config loading from TOML, validation errors on missing required fields, auth mode detection.

## Implementation Notes (Optional)

Use Pydantic `BaseSettings` for config — it natively supports env var loading with type validation. The TOML file support can use `tomllib` (stdlib in 3.11+). For `pyproject.toml`, use `uv` as the build backend if the team prefers it, otherwise `hatchling` or `setuptools`. The quickstart guide is a first impression — make it excellent. Include a "Verify your setup" section that runs health checks (GitHub PAT valid, Ollama responsive, Docker available).
