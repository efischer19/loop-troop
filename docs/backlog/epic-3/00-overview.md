# Epic 3: The 70B Architect & ADR Subflows

> Building the planner prompt that enforces the "Rule of 3" checklist generation, ADR parsing, macro-planning with recursive decomposition, and the PR Reviewer logic.

This epic builds the Tier 3 (70B) workers: the Architect that decomposes features into implementation checklists (micro-planning) or sub-issues (macro-planning per ADR-0002), and the Reviewer that enforces architectural consistency via ADR and CI checks.

## Tickets

1. `01-adr-parser.md` — ADR Parser & Architecture Context Loader
2. `02-architect-planner.md` — The Architect Planner — Rule of 3, Macro-Planning & Agentic TDD
3. `03-pr-reviewer.md` — The PR Reviewer — Architecture, CI & Tautological Test Enforcement
4. `04-label-lifecycle-integration.md` — Architect Subflow — Label Lifecycle Integration
