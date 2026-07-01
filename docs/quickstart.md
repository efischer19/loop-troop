# Quickstart

Loop Troop should be runnable by a new contributor in about 15 minutes.

## Prerequisites

- Python 3.11+
- Docker
- Ollama running locally
- Node.js 20+ (for Repomix and related tooling)
- A GitHub Personal Access Token **or** a GitHub App installation

## Important security warning

Do **not** run Loop Troop itself inside Docker.

Per [ADR-0001](./architecture/ADR-0001-air-gap-security-model.md), the Loop Troop control plane must run natively on the host so it can safely manage Docker while keeping credentials and the Docker socket out of LLM-influenced containers.

## Install

### Option A: `uv`

```bash
uv venv
source .venv/bin/activate
uv pip install -e '.[test]'
```

### Option B: `pip`

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

## Configure Loop Troop

1. Copy the example environment file:

   ```bash
   cp .env.example .env
   ```

2. Fill in the required values in `.env`.

3. Optional: create a `loop-troop.toml` file for stable local settings:

   ```toml
   [github]
   repo = "owner/repo"
   pat = "github_pat_replace_me"

   [workspace]
   repo_path = "/absolute/path/to/target/repo"

   [shadow_log]
   db_path = "~/.loop-troop/shadow.db"

   [ollama]
   host = "http://localhost:11434"

   [models]
   t1 = "qwen2.5-coder:7b"
   t2 = "qwen2.5-coder:14b"
   t3 = "qwen2.5-coder:32b"

   [daemon]
   poll_interval_seconds = 30

   [logging]
   level = "INFO"
   ```

Environment variables override TOML values when both are set.

### Authentication modes

- **PAT mode:** set `GITHUB_PAT`
- **GitHub App mode:** set `LOOP_TROOP_APP_ID`, `LOOP_TROOP_APP_PRIVATE_KEY_PATH`, and `LOOP_TROOP_APP_INSTALLATION_ID`

If neither mode is configured, Loop Troop will fail fast with a validation error.

## Verify your setup

### GitHub auth

```bash
python - <<'PY'
from loop_troop.config import Config
config = Config.from_sources(require_repo=True, require_auth=True)
print(f"auth_mode={config.auth_mode}")
PY
```

### Ollama

```bash
curl "${OLLAMA_HOST:-http://localhost:11434}/api/tags"
```

### Docker

```bash
docker info > /dev/null
```

## First run

Start with a dry run so Loop Troop polls, logs, and validates connectivity without dispatching work:

```bash
loop-troop-daemon --dry-run
```

To use an explicit config file:

```bash
loop-troop-daemon --config loop-troop.toml --dry-run
```

## Shadow log walkthrough

Loop Troop writes polled events into a local SQLite shadow log.

- Default path: `~/.loop-troop/shadow.db`
- Override with `LOOP_TROOP_DB_PATH` or `[shadow_log].db_path`

Useful checks:

```bash
python - <<'PY'
from loop_troop.shadow_log import ShadowLog
with ShadowLog() as log:
    print(log.db_path)
    print(len(log.get_pending_events()))
PY
```

The shadow log is the first place to inspect when you want to confirm that polling worked, that dry-run mode skipped dispatch, or that an event was marked failed or completed.
