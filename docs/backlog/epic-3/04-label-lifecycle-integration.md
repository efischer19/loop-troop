# feat: Architect Subflow — Label Lifecycle Integration

## What do you want to build?

Wire the Architect and Reviewer workers into the Sync Daemon's main loop, establishing the full label lifecycle for Tier 3 operations. This ticket connects the workers built in Tickets 1-3 to the event-processing pipeline from Epic 1, including both micro-planning and macro-planning flows.

## Acceptance Criteria

- [ ] The Sync Daemon dispatches `loop: needs-planning` events to the `ArchitectWorker` (micro-planning).
- [ ] The Sync Daemon dispatches `loop: feature` events to the `ArchitectWorker` (macro-planning, producing sub-issues with DAG).
- [ ] The Sync Daemon dispatches `loop: needs-review` events to the `ReviewerWorker`.
- [ ] The Dispatcher (Tier 1) correctly identifies PR-opened and PR-updated events and applies the `loop: needs-review` label.
- [ ] End-to-end label lifecycle is documented:
  - Micro: `loop: needs-planning` → (Architect) → `loop: ready` → (Coder, Epic 4) → `loop: needs-review` → (Reviewer) → `loop: approved` or `loop: changes-requested`.
  - Macro: `loop: feature` → (Architect) → creates sub-issues with `loop: needs-planning` + posts DAG → parent gets `loop: epic-tracking`.
  - Recursive: A sub-issue can itself be `loop: feature` → further decomposition per ADR-0002.
- [ ] Failed Architect/Reviewer runs mark the event as `failed` in the shadow log with error details, without crashing the daemon.
- [ ] Integration test simulating the full lifecycle from issue creation through planning and review using mocked GitHub/Ollama endpoints, including a macro-planning scenario with sub-issue creation.

## Implementation Notes (Optional)

This is a wiring ticket — minimal new logic, mostly connecting existing components. The key challenge is ensuring the daemon doesn't re-process events that are already in-flight. Use the shadow log's `dispatched` state to prevent duplicate processing. Consider adding a `processing_started_at` timestamp to detect stale dispatches (complementing the zombie sweep from Epic 1 Ticket 4).

For the macro-planning flow, the Dispatcher needs to monitor when sub-issues are closed. When the last sub-issue (the integration test) is resolved, the parent `loop: epic-tracking` issue should be labeled `loop: done` or auto-closed. Use the dependency graph from the DAG comment to determine completion.
