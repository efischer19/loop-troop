# Epic 4: The 35B Coder, Sandboxed Inner Loop & Merge Conflicts

> Building the Coder worker that reads the checklist, writes code, safely triggers `docker run ... make test` without exposing the host OS, and the dedicated Git conflict resolution subflow.

This epic builds the Tier 2 (35B) workers and the security-critical Docker sandbox execution layer. The Coder is the only worker that generates and executes code, and it does so exclusively inside network-isolated containers. The Inner Loop implements an Agentic TDD pipeline (Red-Green) when the Architect specifies `requires_test: true`.

## Tickets

1. `01-docker-sandbox.md` — Ephemeral Docker Sandbox — Network-Isolated Container Execution
2. `02-coder-worker.md` — The Coder Worker — Checklist-Driven Code Generation
3. `03-inner-loop-tdd.md` — Inner Loop — Build/Test Cycle with Red-Green TDD Pipeline
4. `04-git-conflict-resolution.md` — Git Conflict Resolution Subflow
5. `05-pr-creation-checkbox.md` — PR Creation & Checklist Checkbox Update
