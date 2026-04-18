# feat: Sync Daemon Main Loop, Graceful Shutdown & Zombie Sweep

## What do you want to build?

Wire together the GitHub client, shadow log, and dispatcher into a single long-running daemon process with a clean main loop, signal handling, graceful shutdown, and a "Dead Letter / Zombie Sweep" mechanism. The daemon should be launchable via a single CLI command and configurable via environment variables and/or a TOML config file.

The zombie sweep addresses a critical reliability gap: if the 35B Coder is running a 4-minute Docker test suite and the Mac restarts or the Python process crashes, events stuck in `dispatched` state would stall the system indefinitely. The sweep resets stale events to `pending` so they get picked up on the next tick.

## Acceptance Criteria

- [ ] A `main()` entrypoint that initializes the `GitHubClient`, `ShadowLog`, and `Dispatcher`, then enters the poll-dispatch loop.
- [ ] Configurable poll interval (default: 30 seconds) via `LOOP_TROOP_POLL_INTERVAL` or config file.
- [ ] Handles `SIGINT` and `SIGTERM` for graceful shutdown: finishes current dispatch cycle, flushes shadow log, then exits cleanly.
- [ ] Structured logging (JSON or key-value) to stdout with configurable log level.
- [ ] Startup self-check: verifies GitHub PAT validity, Ollama reachability, and SQLite writability before entering the loop.
- [ ] Supports a `--dry-run` flag that polls and logs events but does not apply label changes or dispatch work.
- [ ] **Zombie Sweep**: Every 5 minutes (configurable), the daemon queries the `event_state` table for any event stuck in `dispatched` for longer than a configurable timeout (default: 15 minutes). Stale events are reset to `pending` status so they are picked up on the next dispatch tick.
- [ ] The zombie sweep logs a warning for each reset event, including the event ID, how long it was stuck, and the original dispatch target.
- [ ] Integration test that runs the daemon for 2-3 poll cycles against mocked GitHub/Ollama endpoints and verifies events flow through the full pipeline, including zombie sweep recovery of a simulated stale event.

## Implementation Notes (Optional)

Use `asyncio` for the main loop. Signal handling in asyncio requires `loop.add_signal_handler()`. For config, consider a simple TOML file (`tomllib` is stdlib in 3.11+) with env var overrides. The `--dry-run` flag is critical for safe local testing. Use Python's `logging` module with a JSON formatter for structured logs.

The zombie sweep should run as a periodic task within the asyncio loop (e.g., `asyncio.create_task` with a sleep interval). The sweep query is: `SELECT * FROM event_state WHERE status = 'dispatched' AND dispatched_at < datetime('now', '-15 minutes')`. Reset these to `pending` and clear the `dispatched_at` timestamp. Be careful not to sweep events that are legitimately still processing — the timeout should be generous enough to cover the longest expected Docker sandbox execution (default 5 minutes for sandbox + buffer).
