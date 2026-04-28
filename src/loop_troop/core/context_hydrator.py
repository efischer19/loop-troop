"""Context assembly utilities for grounding LLM calls in repository state."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


class ContextBudgetExceededError(ValueError):
    """Raised when required context alone exceeds the configured token budget."""


class WorkspaceViolationError(ValueError):
    """Raised when a hydration target crosses the control-plane workspace boundary."""


def estimate_token_count(text: str) -> int:
    """Estimate token count without introducing a heavyweight tokenizer dependency."""

    return sum(1 for _ in re.finditer(r"\S+", text))


class ContextHydrator:
    """Hydrate a strict, budget-aware prompt context using Repomix."""

    def __init__(
        self,
        *,
        max_tokens: int = 16_000,
        cache_dir: str | Path | None = None,
        loop_troop_root: str | Path | None = None,
        token_counter: Callable[[str], int] = estimate_token_count,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.max_tokens = max_tokens
        self.cache_dir = Path(cache_dir or Path.home() / ".loop-troop" / "cache" / "context_hydrator")
        self.loop_troop_root = Path(loop_troop_root or Path(__file__).resolve().parents[3]).resolve()
        self._token_counter = token_counter
        self._runner = runner

    def hydrate(
        self,
        *,
        repo_path: str | Path,
        issue_context: str,
        adr_context: str,
        focus_files: Sequence[str] | None = None,
        issue_tokens: int | None = None,
        adr_tokens: int | None = None,
    ) -> str:
        resolved_repo_path = self._validate_repo_path(repo_path)
        required_tokens = self._count_required_tokens(
            issue_context=issue_context,
            adr_context=adr_context,
            issue_tokens=issue_tokens,
            adr_tokens=adr_tokens,
        )
        remaining_tokens = self.max_tokens - required_tokens
        if remaining_tokens <= 0:
            raise ContextBudgetExceededError(
                "Issue/checklist and ADR context exceed the available token budget."
            )

        codebase_context = self._load_or_generate_repomix_output(
            resolved_repo_path,
            focus_files=focus_files,
        )
        truncated_codebase_context = self._truncate_codebase_context(
            codebase_context,
            token_budget=remaining_tokens,
        )
        return self._assemble_context(
            issue_context=issue_context,
            adr_context=adr_context,
            codebase_context=truncated_codebase_context,
        )

    def _count_required_tokens(
        self,
        *,
        issue_context: str,
        adr_context: str,
        issue_tokens: int | None,
        adr_tokens: int | None,
    ) -> int:
        resolved_issue_tokens = issue_tokens if issue_tokens is not None else self._token_counter(issue_context)
        resolved_adr_tokens = adr_tokens if adr_tokens is not None else self._token_counter(adr_context)
        return resolved_issue_tokens + resolved_adr_tokens

    def _validate_repo_path(self, repo_path: str | Path) -> Path:
        resolved_repo_path = Path(repo_path).resolve()
        if not resolved_repo_path.exists():
            raise FileNotFoundError(f"Repository path does not exist: {resolved_repo_path}")
        if not resolved_repo_path.is_dir():
            raise NotADirectoryError(f"Repository path is not a directory: {resolved_repo_path}")
        if resolved_repo_path.is_relative_to(self.loop_troop_root):
            raise WorkspaceViolationError(
                "Hydration target must not be inside the Loop Troop installation directory."
            )
        return resolved_repo_path

    def _load_or_generate_repomix_output(
        self,
        repo_path: Path,
        *,
        focus_files: Sequence[str] | None,
    ) -> str:
        commit_sha = self._read_commit_sha(repo_path)
        cache_key = self._cache_key(repo_path=repo_path, commit_sha=commit_sha, focus_files=focus_files)
        cached_output = self._read_cache_entry(cache_key)
        if cached_output is not None:
            return cached_output

        generated_output = self._run_repomix(repo_path, focus_files=focus_files)
        self._write_cache_entry(cache_key, generated_output)
        return generated_output

    def _read_commit_sha(self, repo_path: Path) -> str:
        completed = self._runner(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout.strip()

    def _run_repomix(self, repo_path: Path, *, focus_files: Sequence[str] | None) -> str:
        command = ["npx", "repomix", "--stdout"]
        include_patterns = self._normalize_focus_files(focus_files)
        if include_patterns:
            command.extend(["--include", ",".join(include_patterns)])

        completed = self._runner(
            command,
            cwd=repo_path,
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout

    @staticmethod
    def _normalize_focus_files(focus_files: Sequence[str] | None) -> list[str]:
        if not focus_files:
            return []
        return sorted({item for item in focus_files if item})

    def _cache_key(
        self,
        *,
        repo_path: Path,
        commit_sha: str,
        focus_files: Sequence[str] | None,
    ) -> str:
        focus_files_hash = hashlib.sha256(
            json.dumps(self._normalize_focus_files(focus_files), separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        key_payload = json.dumps(
            {
                "repo_path": str(repo_path),
                "commit_sha": commit_sha,
                "focus_files_hash": focus_files_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(key_payload.encode("utf-8")).hexdigest()

    def _read_cache_entry(self, cache_key: str) -> str | None:
        cache_path = self.cache_dir / f"{cache_key}.txt"
        if not cache_path.exists():
            return None
        return cache_path.read_text()

    def _write_cache_entry(self, cache_key: str, content: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{cache_key}.txt").write_text(content)

    def _truncate_codebase_context(self, context: str, *, token_budget: int) -> str:
        if token_budget <= 0:
            raise ContextBudgetExceededError(
                "Codebase context cannot be included: issue and ADR context consume entire token budget."
            )

        token_spans = list(re.finditer(r"\S+", context))
        if len(token_spans) <= token_budget:
            return context

        marker = "\n[TRUNCATED]"
        marker_tokens = self._token_counter(marker)
        available_tokens = token_budget - marker_tokens
        if available_tokens < 1:
            return marker.lstrip()

        cutoff = token_spans[available_tokens - 1].end()
        return f"{context[:cutoff].rstrip()}{marker}"

    @staticmethod
    def _assemble_context(
        *,
        issue_context: str,
        adr_context: str,
        codebase_context: str,
    ) -> str:
        return "\n\n".join(
            [
                "## GitHub Issue / Checklist",
                issue_context,
                "## ADR Context",
                adr_context,
                "## Repomix Codebase Context",
                codebase_context,
            ]
        )
