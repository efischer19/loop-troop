---
title: "ADR-0002: Recursive Epic Decomposition"
status: "Accepted"
date: "2026-04-18"
tags:
  - "planning"
  - "architecture"
  - "workflow"
---

## Context

The 70B Architect needs to decompose large features into actionable work items. Sometimes a feature is too large for a single checklist and needs to be broken into sub-issues. Those sub-issues may themselves be features that require further decomposition. Imposing an artificial limit on nesting depth would force the Architect to produce oversized, hard-to-review work items or to flatten genuinely hierarchical problems into a single layer, losing the natural structure of the solution.

## Decision

The system supports **unlimited nesting depth** for epic decomposition. There is no inherent restriction on how deeply subproblems may be nested — if the best way to solve a task requires four levels of epics, the system must support that.

### Recursive DAG Structure

- A `loop: feature` issue can produce sub-issues that are themselves `loop: feature` issues, creating a recursive directed acyclic graph (DAG).
- The Dispatcher tracks dependencies via `(Depends on: #X)` markers in the parent issue's DAG comment.
- Each level of decomposition follows the same planning protocol: the 70B Architect analyzes the issue, reads relevant ADRs and code, and produces a checklist or further sub-issues as appropriate.

### Integration Testing Requirement

The final sub-issue in any feature decomposition **must** be an integration or feature test that runs via standard CI. This ensures the feature works end-to-end before the parent epic is considered complete. The test sub-issue depends on all other sub-issues in its group, guaranteeing it runs last.

## Consequences

- The Dispatcher **must** implement cycle detection in the dependency graph to prevent infinite loops caused by circular `Depends on` references.
- The system must handle **partial completion** gracefully: some sub-issues may be done while others are blocked or in progress. The parent epic remains open until all children (including the integration test) are resolved.
- Deeply nested decompositions increase the total number of issues and may extend wall-clock time for feature delivery, but they produce smaller, more reviewable units of work.
- The Architect must exercise judgment on when further decomposition adds value versus when a single checklist suffices.
