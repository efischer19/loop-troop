# Epic 2: Data Contracts & Context Engineering

> Defining Pydantic schemas, setting up Repomix hydration, and ensuring strict separation between the Loop Troop workspace and the Target Repo workspace.

This epic establishes the shared data contracts that all workers consume and the context-assembly pipeline that feeds them. Every LLM interaction in the system goes through Instructor with a Pydantic schema — no free-form text parsing.

## Tickets

1. `01-pydantic-schemas.md` — Pydantic Schemas for Event Routing, Worker Contracts & Epic Planning
2. `02-repomix-context-hydration.md` — Repomix Context Hydration Pipeline with Strict Context Hierarchy
3. `03-workspace-isolation.md` — Workspace Isolation — Control Plane vs. Data Plane Directory Management
4. `04-instructor-ollama-client.md` — Instructor Client Configuration for Ollama with Model Override
