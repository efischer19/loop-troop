# Epic 5: Observability, DX & Bake-off CLI

> Setting up local evaluation CLI, tracking Time-To-First-Token and Instructor retry rates.

This epic adds the observability, developer experience, and evaluation tooling needed to operate and tune the system. Without metrics, we're flying blind — especially when comparing model performance across tiers.

---

### [Epic 5] Ticket 1: Observability — TTFT & Instructor Retry Rate Tracking
**Priority:** High
**Description:**
Instrument all LLM calls across the system to track key performance metrics: Time-To-First-Token (TTFT), total inference latency, token usage (prompt + completion), and Instructor retry rates. These metrics are stored in the SQLite shadow log alongside the events that triggered them, enabling post-hoc analysis and model comparison.

**Acceptance Criteria:**
* [ ] An `LLMMetrics` dataclass/Pydantic model: `call_id`, `tier`, `model_name`, `prompt_tokens`, `completion_tokens`, `ttft_ms` (nullable — depends on Ollama streaming support), `total_latency_ms`, `instructor_retries`, `validation_errors` (list[str]), `success` (bool).
* [ ] A `MetricsCollector` that wraps Instructor calls and automatically captures the above metrics.
* [ ] Metrics are written to a dedicated `llm_metrics` table in the SQLite shadow log.
* [ ] Schema migration extends the existing shadow log DB (from Epic 1 Ticket 2) without breaking existing tables.
* [ ] `MetricsCollector` is integrated into the `LLMClient` factory (Epic 2 Ticket 4) so all LLM calls are automatically instrumented — workers don't need to opt in.
* [ ] Metrics include the `event_id` from the shadow log, linking each LLM call back to the GitHub event that triggered it.
* [ ] TTFT is measured by timing the first chunk from Ollama's streaming API (if available), otherwise recorded as `null`.
* [ ] Unit tests covering: metric capture on success, metric capture on retry, metric persistence to SQLite.

**Implementation Notes (Tech Lead hints):**
Wrap the Instructor client's `create()` method with a timing decorator. For TTFT, use Ollama's streaming mode (`stream=True`) and measure `time.monotonic()` between request start and first chunk arrival. Instructor retry count can be tracked by hooking into `instructor`'s retry callback. Store metrics in a separate table to avoid polluting the event log, but use the same SQLite database for simplicity.

---

### [Epic 5] Ticket 2: Local Evaluation CLI — Bake-off Tool
**Priority:** Medium
**Description:**
Build a CLI tool that enables developers to run controlled evaluations ("bake-offs") comparing different model configurations against a set of benchmark tasks. This is essential for tuning the cognitive tiering: is the 8B model fast enough for dispatch? Does the 35B model produce better code than the 8B? Does the 70B model justify its slower inference for planning?

**Acceptance Criteria:**
* [ ] A `loop-troop eval` CLI subcommand (or standalone script) that runs a benchmark suite.
* [ ] Accepts a benchmark definition file (TOML or YAML) specifying: tasks (issue bodies), expected outputs (checklist structure, code patches), and model configurations to compare.
* [ ] Runs each task against each model configuration, capturing all `LLMMetrics` (Ticket 1).
* [ ] Produces a summary report (to stdout and/or a Markdown file) with: pass/fail rates, average TTFT, average retries, token usage, and total wall-clock time per model.
* [ ] Supports `--tier` flag to run evaluations for a specific tier only (T1/T2/T3).
* [ ] Supports `--output` flag to write results to a JSON file for further analysis.
* [ ] Benchmark tasks run against mocked GitHub endpoints (no real GitHub API calls during eval).
* [ ] All code execution benchmarks run inside the Docker sandbox (per ADR-0001).
* [ ] Unit tests covering: benchmark file parsing, report generation, tier filtering.

**Implementation Notes (Tech Lead hints):**
Use Python's `argparse` or `click` for the CLI. The benchmark definition file should be simple — each task is an issue body and a set of assertions about the output (e.g., "checklist has ≤5 items", "all files in checklist exist in the repo"). For the bake-off report, a simple ASCII table (using `tabulate`) is sufficient. Consider seeding with 3-5 benchmark tasks from the Loop Troop codebase itself as dogfood.

---

### [Epic 5] Ticket 3: Developer Experience — Configuration, `.env`, & Quickstart Guide
**Priority:** Medium
**Description:**
Create the developer-facing configuration system, example environment file, and quickstart documentation that enables a new contributor to set up and run Loop Troop locally within 15 minutes. This includes consolidating all scattered environment variable references from previous epics into a single, well-documented configuration surface.

**Acceptance Criteria:**
* [ ] A `.env.example` file listing all required and optional environment variables with descriptions and safe defaults.
* [ ] A `Config` class (Pydantic `BaseSettings`) that loads configuration from environment variables and/or a `loop-troop.toml` file, with validation and helpful error messages on misconfiguration.
* [ ] All previous tickets' env var references (`GITHUB_PAT`, `OLLAMA_HOST`, `LOOP_TROOP_DB_PATH`, `LOOP_TROOP_POLL_INTERVAL`, model names, etc.) are consolidated into this single `Config` class.
* [ ] A `docs/quickstart.md` guide covering: prerequisites (Python 3.11+, Docker, Ollama, Node.js for Repomix), installation steps (`uv` or `pip`), configuration, first run with `--dry-run`, and a walkthrough of the shadow log.
* [ ] A `pyproject.toml` with project metadata, dependencies, and a `[project.scripts]` entry point for the `loop-troop` CLI command.
* [ ] The quickstart guide explicitly warns against running Loop Troop inside a Docker container (per ADR-0001) and explains why.
* [ ] Unit tests covering: Config loading from env vars, Config loading from TOML, validation errors on missing required fields.

**Implementation Notes (Tech Lead hints):**
Use Pydantic `BaseSettings` for config — it natively supports env var loading with type validation. The TOML file support can use `tomllib` (stdlib in 3.11+). For `pyproject.toml`, use `uv` as the build backend if the team prefers it, otherwise `hatchling` or `setuptools`. The quickstart guide is a first impression — make it excellent. Include a "Verify your setup" section that runs health checks (GitHub PAT valid, Ollama responsive, Docker available).
