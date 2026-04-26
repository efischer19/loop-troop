# feat: ADR Parser & Architecture Context Loader

## What do you want to build?

Build the ADR (Architecture Decision Record) parser that reads and indexes all ADR documents from a target repository's `docs/architecture/` directory. This provides the Architect and Reviewer workers with the current architectural context, ensuring all planning and review decisions are grounded in the project's accepted ADRs.

This parser handles **Project ADRs** (per ADR-0001's platform vs. project distinction) — i.e., the ADRs that live in the target repository and govern that project's architecture. Platform ADRs (Loop Troop's own, e.g., ADR-0001, ADR-0002) are constants and do not need dynamic loading.

## Acceptance Criteria

- [ ] An `ADRLoader` class that scans a target repo's `docs/architecture/` folder for Markdown ADR files.
- [ ] Parses each ADR into a structured `ADRDocument` model: `id`, `title`, `status` (Accepted/Superseded/Deprecated), `decision_summary`, `full_text`. Supports both YAML frontmatter format (per `docs/example/ADR-001-use_adrs.md`) and inline heading format.
- [ ] Filters to only `Accepted` ADRs by default (with option to include all).
- [ ] Produces a combined context string of all active ADRs, suitable for LLM prompt injection.
- [ ] Enforces a configurable token budget for ADR context (default: 4,000 tokens), prioritizing by recency.
- [ ] The ADR directory path must resolve to within the target repository's workspace (not the Loop Troop source tree) — reuse workspace validation from Epic 2 Ticket 3.
- [ ] Unit tests with fixture ADR files (in both frontmatter and inline formats) covering: parsing, status filtering, token truncation, missing ADR directory (graceful handling).

## Implementation Notes (Optional)

ADR format follows two patterns: (1) YAML frontmatter with `title`, `status`, `date`, `tags` fields (see `docs/example/ADR-001-use_adrs.md` for the canonical template), or (2) inline headings: `# ADR-NNNN: Title`, `## Status`, `## Decision`, etc. Support both. Use `pyyaml` or a simple frontmatter parser for the YAML variant; regex for inline headings. The `ADRDocument` model should be a Pydantic schema in `src/core/schemas.py`. Cache parsed ADRs per-commit-SHA alongside the Repomix cache.

Note: The ADR context must fit entirely within the Strict Context Hierarchy (Epic 2 Ticket 2) — it is never truncated. If it exceeds its budget, a hard error is raised. This means the 4,000-token default must be generous enough for typical projects.
