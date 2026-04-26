# Loop Troop
> *A polling-based, local-first AI software factory that works your GitHub backlog while you sleep.*

Loop Troop is an asynchronous, polling-based, multi-agent AI software factory. Instead of a monolithic in-memory state machine, it uses **GitHub issues and PR comments as the message broker and state tracker**, orchestrating stateless "chunky" worker agents that hydrate context, execute work, and sleep.

## 🔐 GitHub Authentication (MVP)

For the polling client MVP, use a **dedicated GitHub Machine Account** with a fine-grained Personal Access Token scoped only to the target repositories. Store the token in the native host environment as `GITHUB_PAT` and configure the polling cadence with `LOOP_TROOP_POLL_INTERVAL`.

Per [ADR-0001](docs/architecture/ADR-0001-air-gap-security-model.md), this token belongs only in the native Control Plane environment and must never be passed into Docker containers.

## 🏗️ Core Architecture

### Codebase Separation & The "Air-Gap" Security Model

1. **The Control Plane (Loop Troop Code):** Runs *natively* on the Mac host OS (e.g., via `uv` or `venv`). It handles polling, LLM API calls, git operations, and orchestrates subprocesses. It is **never containerized** to prevent exposing the host's Docker socket to an LLM.
2. **The Data Plane (Target Codebase):** The repositories being worked on by the LLMs are cloned locally. All LLM-generated code execution (e.g., `make test`) happens strictly inside ephemeral, network-isolated Docker containers spun up by the native Control Plane. The LLM has **zero access** to the native host OS.

### Core Components

* **Event Router:** A lightweight Python Sync Daemon polling the GitHub REST API (no webhooks).
* **Shadow Log / Replayability:** SQLite (every polled event is instantly logged locally).
* **Execution Sandbox:** Ephemeral, network-isolated Docker containers.
* **Context Hydration:** Repomix (for token-optimized repository mapping).
* **Structured Output:** Instructor using Pydantic schemas.

### Hardware & Inference

* **Hardware:** Local Mac system(s) clustered via a Thunderbolt bridge.
* **Inference Engine:** Ollama running natively, with Exo (or `llama.cpp` RPC) clustering the memory.

## 🧠 Cognitive Tiering (The Workers)
The system routes tasks to appropriately sized local models via Ollama / Exo clusters:
1. **Tier 1 (8B Models) — The Dispatcher:** Polling GitHub, routing events, and managing the "Label Waterfall" (moving tickets sequentially).
2. **Tier 2 (35B Models) — The Coder:** Code generation, inner loop execution (`make test` in Docker sandbox), and merge conflict resolution.
3. **Tier 3 (70B/100B Models) — The Architect & Reviewer:** Planning (Rule of 3 checklist generation), PR review, and ADR enforcement.

## 📂 Target Directory Structure
(Note: This is the expected target state for the MVP implementation)

```text
loop-troop/
├── README.md
├── pyproject.toml            # Project config (uv/pip)
├── .env.example              # GitHub PAT, Ollama host URLs
├── src/
│   ├── daemon/               # Sync Daemon, polling loop, shutdown
│   ├── shadow_log/           # SQLite event logger
│   ├── core/                 # Repomix hydration, Instructor client, GitHub API wrapper
│   └── workers/
│       ├── dispatcher.py     # 8B Worker (Label waterfall)
│       ├── architect.py      # 70B Worker (Rule of 3 planner)
│       ├── coder.py          # 35B Worker (Code generation & Docker execution)
│       ├── conflict.py       # 35B Worker (Merge conflict resolution subflow)
│       └── reviewer.py       # 70B Worker (PR review & ADR enforcement)
├── tests/
└── docs/
    ├── architecture/         # ADRs and system diagrams
    └── backlog/              # Epic planning & ticket backlog
```
