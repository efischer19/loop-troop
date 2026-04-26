# Epic 1: The Native Sync Daemon & Shadow Log

> Building the native Python polling script, GitHub API wrapper, SQLite event logger, and the 8B dispatcher logic.

This epic establishes the foundational event-processing pipeline. The Sync Daemon is the heartbeat of Loop Troop — it polls GitHub, logs every event to SQLite for replay, and dispatches work to the appropriate cognitive tier.

**MVP Note:** For the MVP, use a dedicated GitHub Machine Account (free, within TOS) with a Fine-Grained PAT. This provides visual separation of bot-authored PRs/comments from human activity. Store the PAT in `.env`.

## Tickets

1. `01-github-api-polling-client.md` — GitHub REST API Polling Client & Authentication
2. `02-sqlite-shadow-log.md` — SQLite Shadow Log Schema & Event Logger
3. `03-tier1-dispatcher-label-waterfall.md` — Tier 1 (8B) Dispatcher — Label Waterfall Router
4. `04-sync-daemon-main-loop.md` — Sync Daemon Main Loop, Graceful Shutdown & Zombie Sweep
