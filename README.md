# Loop Troop
> *An event driven, local-first AI software factory that works your GitHub backlog while you sleep.*

Loop Troop is an asynchronous, tiered, multi-LLM worker pool for autonomous local development. It uses GitHub webhooks as a message broker to orchestrate stateless AI agents that write, test, and review code inside isolated Docker containers.

## 🏗️ Core Architecture
This project abandons the in-memory LangGraph state machine in favor of a decentralized, GitHub-native webhook router and shadow log.

* **Message Broker:** GitHub Webhooks & PR Comments.
* **Event Replay:** Local SQLite Shadow Log.
* **Execution Sandbox:** Ephemeral Docker containers.
* **Context Hydration:** Repomix.
* **LLM Output Structuring:** Instructor (Pydantic).

## 🧠 Cognitive Tiering (The Workers)
The system routes tasks to appropriately sized local models via `llama.cpp` / `Exo` clusters:
1. **Tier 1 (8B Models):** Triage, Label Waterfall management, routing.
2. **Tier 2 (35B Models):** The Coder (Inner loop execution, `make test`, conflict resolution).
3. **Tier 3 (70B Models):** The Arbiter (PR Review, architecture enforcement).

## 📂 Target Directory Structure
(Note: This is the expected target state for the MVP implementation)

```text
loop-troop/
├── README.md
├── docker-compose.yml       # For local infra (if needed)
├── .env.example             # GitHub PAT, Ollama host URLs
├── src/
│   ├── router/              # Shadow Log SQLite DB
│   ├── core/                # Repomix hydration, Instructor client, GitHub API wrapper
│   └── workers/             
│       ├── triage.py        # 8B Worker (Label waterfall)
│       ├── coder.py         # 35B Worker (Code generation & Docker execution)
│       ├── conflict.py      # 35B Worker (Merge conflict resolution subflow)
│       └── reviewer.py      # 70B Worker (PR review & ADR enforcement)
├── tests/
└── docs/
    └── architecture/        # ADRs and system diagrams
