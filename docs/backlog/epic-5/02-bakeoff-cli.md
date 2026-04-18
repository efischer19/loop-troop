# feat: Local Evaluation CLI — Bake-off Tool

## What do you want to build?

Build a CLI tool that enables developers to run controlled evaluations ("bake-offs") comparing different model configurations against a set of benchmark tasks. This is essential for tuning the cognitive tiering: is the 8B model fast enough for dispatch? Does the 35B model produce better code than the 8B? Does the 70B model justify its slower inference for planning?

## Acceptance Criteria

- [ ] A `loop-troop eval` CLI subcommand (or standalone script) that runs a benchmark suite.
- [ ] Accepts a benchmark definition file (TOML or YAML) specifying: tasks (issue bodies), expected outputs (checklist structure, code patches), and model configurations to compare.
- [ ] Runs each task against each model configuration, capturing all `LLMMetrics` (Ticket 1).
- [ ] Produces a summary report (to stdout and/or a Markdown file) with: pass/fail rates, average TTFT, average retries, token usage, and total wall-clock time per model.
- [ ] Supports `--tier` flag to run evaluations for a specific tier only (T1/T2/T3).
- [ ] Supports `--output` flag to write results to a JSON file for further analysis.
- [ ] Benchmark tasks run against mocked GitHub endpoints (no real GitHub API calls during eval).
- [ ] All code execution benchmarks run inside the Docker sandbox (per ADR-0001).
- [ ] Unit tests covering: benchmark file parsing, report generation, tier filtering.

## Implementation Notes (Optional)

Use Python's `argparse` or `click` for the CLI. The benchmark definition file should be simple — each task is an issue body and a set of assertions about the output (e.g., "checklist has ≤5 items", "all files in checklist exist in the repo"). For the bake-off report, a simple ASCII table (using `tabulate`) is sufficient. Consider seeding with 3-5 benchmark tasks from the Loop Troop codebase itself as dogfood.
