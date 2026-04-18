# Epic 2: Data Contracts & Context Engineering

> Defining Pydantic schemas, setting up Repomix hydration, and ensuring strict separation between the Loop Troop workspace and the Target Repo workspace.

This epic establishes the shared data contracts that all workers consume and the context-assembly pipeline that feeds them. Every LLM interaction in the system goes through Instructor with a Pydantic schema — no free-form text parsing.

---

### [Epic 2] Ticket 1: Pydantic Schemas for Event Routing & Worker Contracts
**Priority:** High
**Description:**
Define the canonical Pydantic v2 schemas that form the data contracts between the Sync Daemon, the Dispatcher, and all worker tiers. These schemas are the system's lingua franca — every LLM call uses Instructor to produce one of these models, and every inter-component message is validated against them.

**Acceptance Criteria:**
* [ ] `DispatchDecision` schema: `event_id`, `event_type` (enum), `target_tier` (enum: T1/T2/T3), `label_action` (add/remove label + label name), `reasoning` (str).
* [ ] `ArchitectPlan` schema: `issue_number`, `checklist_items` (list of `ChecklistItem`), `adr_references` (list of referenced ADR filenames).
* [ ] `ChecklistItem` schema: `description` (str), `files_touched` (list[str], max 3), `logical_steps` (list[str], max 3), `architectural_decisions` (list — must be empty, validated).
* [ ] `CodePatch` schema: `issue_number`, `checklist_item_index`, `branch_name`, `files_changed` (list of `FileChange`), `test_command` (str), `commit_message` (str).
* [ ] `ReviewVerdict` schema: `pr_number`, `verdict` (enum: approve/request_changes/reject), `adr_violations` (list[str]), `comments` (list of `ReviewComment`).
* [ ] All schemas use Pydantic v2 `model_validator` or `field_validator` to enforce domain invariants (e.g., `ChecklistItem.files_touched` has max length 3).
* [ ] Schemas are importable from a single `src/core/schemas.py` module.
* [ ] Unit tests validating: schema construction, validation error on Rule-of-3 violation, serialization round-trip (JSON ↔ model).

**Implementation Notes (Tech Lead hints):**
Use `Literal` types and `Enum` classes for constrained fields. The `ChecklistItem` validator enforcing "zero architectural decisions" should raise a clear `ValidationError` with a message like "Checklist items must not contain architectural decisions — use an ADR instead." This is the single most important schema in the system. Keep schemas in one file for now; split later if needed.

---

### [Epic 2] Ticket 2: Repomix Context Hydration Pipeline
**Priority:** High
**Description:**
Build the context-assembly pipeline that uses Repomix to generate a token-optimized representation of the target repository. This context is fed to every LLM call to ground the model in the current state of the codebase. The pipeline must produce a context string that fits within the model's context window, prioritizing files relevant to the current task.

**Acceptance Criteria:**
* [ ] A `ContextHydrator` class that wraps the Repomix CLI (`npx repomix`).
* [ ] Accepts a target repository path and an optional file-focus list (from the `ChecklistItem.files_touched` field) to prioritize relevant files.
* [ ] Produces a single string output suitable for LLM prompt injection.
* [ ] Enforces a configurable max-token budget (default: 16,000 tokens) and truncates gracefully with a `[TRUNCATED]` marker.
* [ ] Caches Repomix output per-commit-SHA to avoid redundant regeneration.
* [ ] The target repository path must be validated to ensure it is NOT inside the Loop Troop source tree (enforcing Control Plane / Data Plane separation).
* [ ] Raises `WorkspaceViolationError` if a hydration request targets a path inside the Loop Troop installation directory.
* [ ] Unit tests with a fixture repository covering: full hydration, focused hydration, token truncation, cache hit/miss, workspace violation.

**Implementation Notes (Tech Lead hints):**
Repomix is a Node.js tool — invoke it via `subprocess.run(["npx", "repomix", ...])` from the native Control Plane. Parse its stdout/file output. The cache key should be `(repo_path, commit_sha, focus_files_hash)`. Store the cache in the SQLite shadow log or a simple file-based cache under `~/.loop-troop/cache/`. The workspace violation check is a critical security boundary — use `pathlib.Path.resolve()` to prevent symlink traversal attacks.

---

### [Epic 2] Ticket 3: Workspace Isolation — Control Plane vs. Data Plane Directory Management
**Priority:** High
**Description:**
Implement the directory management layer that enforces strict separation between the Loop Troop Control Plane workspace (where Loop Troop's own code lives) and the Data Plane workspaces (where target repositories are cloned and manipulated). This prevents accidental or malicious cross-contamination.

**Acceptance Criteria:**
* [ ] A `WorkspaceManager` class that manages target repository cloning, branch management, and cleanup.
* [ ] Target repositories are cloned into a configurable base directory (default: `~/.loop-troop/workspaces/`), never inside the Loop Troop source tree.
* [ ] Provides `clone_or_update(repo_url)`, `checkout_branch(repo_path, branch)`, `create_branch(repo_path, branch)`, and `cleanup(repo_path)` methods.
* [ ] All methods validate that the provided path resolves to within the configured workspace base directory (no path traversal via `../` or symlinks).
* [ ] Git operations use `subprocess.run(["git", ...], cwd=target_path)` — never `os.chdir()` which would affect the Control Plane process.
* [ ] Cloned repositories are checked for the expected template structure (Makefile, Dockerfile, `docs/architecture/` folder) before being accepted for work.
* [ ] Unit tests covering: clone, update, path traversal rejection, symlink rejection, template validation.

**Implementation Notes (Tech Lead hints):**
Use `pathlib.Path.resolve()` and check `.is_relative_to(workspace_base)` for path validation. The `subprocess.run` calls should always set `cwd=` rather than changing the Control Plane's host process working directory. Consider adding a `.loop-troop-workspace` sentinel file to workspace directories to distinguish managed clones from user directories.

---

### [Epic 2] Ticket 4: Instructor Client Configuration for Ollama
**Priority:** Medium
**Description:**
Set up the shared Instructor client configuration that all workers use to interact with local Ollama models. This includes model routing (8B for Tier 1, 35B for Tier 2, 70B for Tier 3), retry policies, and structured output enforcement.

**Acceptance Criteria:**
* [ ] An `LLMClient` factory that returns pre-configured Instructor clients for each cognitive tier.
* [ ] Model names are configurable via environment variables (`LOOP_TROOP_T1_MODEL`, `LOOP_TROOP_T2_MODEL`, `LOOP_TROOP_T3_MODEL`) with sensible defaults.
* [ ] Ollama base URL is configurable (`OLLAMA_HOST`, default: `http://localhost:11434`).
* [ ] Each client is configured with Instructor retry logic: max 3 retries on validation failure, with the validation error fed back to the model for self-correction.
* [ ] Includes a health-check method that verifies the target model is loaded and responsive.
* [ ] Logs every LLM call's token usage and latency (for Epic 5 observability).
* [ ] Must never pass credentials (GitHub PAT, etc.) in LLM prompts — validated by a prompt-sanitization check.
* [ ] Unit tests with mocked Ollama responses covering: successful structured output, retry on validation error, model health check, prompt sanitization.

**Implementation Notes (Tech Lead hints):**
Use `instructor.from_openai()` with `openai.OpenAI(base_url="http://localhost:11434/v1")` — Ollama exposes an OpenAI-compatible API. The retry logic is Instructor's killer feature: when a Pydantic validation fails, Instructor automatically re-prompts the model with the error message. Log the `usage` field from the response for TTFT tracking. The prompt-sanitization check should scan for known credential patterns (e.g., `ghp_`, `gho_`) in the assembled prompt string.
