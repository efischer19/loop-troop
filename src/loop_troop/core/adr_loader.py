"""ADR parsing and context assembly for target repositories."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .context_hydrator import (
    ContextBudgetExceededError,
    WorkspaceViolationError,
    estimate_token_count,
    validate_target_repo_path,
)
from .schemas import ADRDocument, ADRStatus

_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n?(?P<body>.*)\Z", re.DOTALL)
_SECTION_PATTERN = re.compile(r"^##\s+(?P<name>[^\n]+)\n(?P<body>.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
_ADR_ID_PATTERN = re.compile(r"(ADR[-_ ]?\d+)", re.IGNORECASE)
_TITLE_WITH_ID_PATTERN = re.compile(
    r"^\s*(?P<id>ADR[-_ ]?\d+)(?:\s*[:\-]\s*|\s+)(?P<title>.+?)\s*$",
    re.IGNORECASE,
)


class ADRLoader:
    """Load and cache project ADRs from a target repository workspace."""

    def __init__(
        self,
        *,
        token_budget: int = 4_000,
        cache_dir: str | Path | None = None,
        loop_troop_root: str | Path | None = None,
        token_counter: Callable[[str], int] = estimate_token_count,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.token_budget = token_budget
        self.cache_dir = Path(cache_dir or Path.home() / ".loop-troop" / "cache" / "adr_loader")
        self.loop_troop_root = Path(loop_troop_root or Path(__file__).resolve().parents[3]).resolve()
        self._token_counter = token_counter
        self._runner = runner

    def load(self, repo_path: str | Path, *, include_all: bool = False) -> list[ADRDocument]:
        resolved_repo_path = validate_target_repo_path(repo_path, loop_troop_root=self.loop_troop_root)
        documents = self._load_or_parse_documents(resolved_repo_path)
        if include_all:
            return documents
        return [document for document in documents if document.status is ADRStatus.ACCEPTED]

    def build_context(self, repo_path: str | Path, *, include_all: bool = False) -> str:
        documents = self.load(repo_path, include_all=include_all)
        if not documents:
            return ""

        context = "\n\n".join(self._format_context_entry(document) for document in documents)
        token_count = self._token_counter(context)
        if token_count > self.token_budget:
            raise ContextBudgetExceededError(
                f"ADR context exceeds token budget: {token_count} > {self.token_budget}."
            )
        return context

    def _load_or_parse_documents(self, repo_path: Path) -> list[ADRDocument]:
        adr_dir = self._resolve_adr_directory(repo_path)
        if not adr_dir.is_dir():
            return []

        commit_sha = self._read_commit_sha(repo_path)
        cache_key = self._cache_key(repo_path=repo_path, commit_sha=commit_sha)
        cached_documents = self._read_cache_entry(cache_key)
        if cached_documents is not None:
            return cached_documents

        documents = self._parse_documents(adr_dir)
        self._write_cache_entry(cache_key, documents)
        return documents

    def _resolve_adr_directory(self, repo_path: Path) -> Path:
        adr_dir = (repo_path / "docs" / "architecture").resolve()
        if not adr_dir.is_relative_to(repo_path):
            raise WorkspaceViolationError("ADR directory must remain within the target repository workspace.")
        return adr_dir

    def _read_commit_sha(self, repo_path: Path) -> str:
        completed = self._runner(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            text=True,
        )
        return completed.stdout.strip()

    def _cache_key(self, *, repo_path: Path, commit_sha: str) -> str:
        payload = json.dumps(
            {"repo_path": str(repo_path), "commit_sha": commit_sha},
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache_entry(self, cache_key: str) -> list[ADRDocument] | None:
        cache_path = self.cache_dir / f"{cache_key}.json"
        if not cache_path.exists():
            return None
        payload = json.loads(cache_path.read_text())
        return [ADRDocument.model_validate(item) for item in payload]

    def _write_cache_entry(self, cache_key: str, documents: list[ADRDocument]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_dir / f"{cache_key}.json"
        cache_path.write_text(json.dumps([document.model_dump(mode="json") for document in documents]))

    def _parse_documents(self, adr_dir: Path) -> list[ADRDocument]:
        parsed_documents: list[tuple[tuple[int, str], ADRDocument]] = []
        for path in sorted(adr_dir.glob("*.md")):
            document, sort_key = self._parse_document(path)
            parsed_documents.append((sort_key, document))

        parsed_documents.sort(key=lambda item: item[0], reverse=True)
        return [document for _sort_key, document in parsed_documents]

    def _parse_document(self, path: Path) -> tuple[ADRDocument, tuple[int, str]]:
        raw_text = path.read_text()
        frontmatter_match = _FRONTMATTER_PATTERN.match(raw_text)

        if frontmatter_match:
            metadata = self._parse_frontmatter(frontmatter_match.group("frontmatter"))
            body = frontmatter_match.group("body").strip()
            title = self._strip_quotes(metadata.get("title", ""))
            status = self._parse_status(metadata.get("status", ""))
            decision_text = self._extract_section(body, "Decision")
        else:
            body = raw_text.strip()
            title_line = self._extract_inline_title(body)
            title = title_line
            status = self._parse_status(self._extract_section(body, "Status"))
            decision_text = self._extract_section(body, "Decision")

        adr_id, normalized_title = self._split_adr_title(title, path)
        document = ADRDocument(
            id=adr_id,
            title=normalized_title,
            status=status,
            decision_summary=self._summarize_decision(decision_text),
            full_text=body,
        )
        return document, (self._extract_adr_number(adr_id), path.name)

    @staticmethod
    def _parse_frontmatter(frontmatter: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        current_key: str | None = None
        current_items: list[str] = []

        for raw_line in frontmatter.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                continue
            if current_key and line.lstrip().startswith("- "):
                current_items.append(line.split("- ", maxsplit=1)[1].strip())
                continue
            if current_key and current_items:
                metadata[current_key] = ", ".join(current_items)
                current_key = None
                current_items = []

            key, separator, value = line.partition(":")
            if not separator:
                continue
            key = key.strip().lower()
            value = value.strip()
            if value:
                metadata[key] = value
                current_key = None
                current_items = []
            else:
                current_key = key
                current_items = []

        if current_key and current_items:
            metadata[current_key] = ", ".join(current_items)
        return metadata

    @staticmethod
    def _extract_inline_title(body: str) -> str:
        for line in body.splitlines():
            if line.startswith("# "):
                return line.removeprefix("# ").strip()
        raise ValueError("ADR document is missing a top-level heading.")

    @staticmethod
    def _extract_section(body: str, section_name: str) -> str:
        for match in _SECTION_PATTERN.finditer(body):
            if match.group("name").strip().lower() == section_name.lower():
                return match.group("body").strip()
        raise ValueError(f"ADR document is missing a '{section_name}' section.")

    @staticmethod
    def _parse_status(raw_status: str) -> ADRStatus:
        normalized = ADRLoader._strip_quotes(raw_status).strip().lower()
        if normalized.startswith("accepted"):
            return ADRStatus.ACCEPTED
        if normalized.startswith("superseded"):
            return ADRStatus.SUPERSEDED
        if normalized.startswith("deprecated"):
            return ADRStatus.DEPRECATED
        if normalized.startswith("proposed"):
            return ADRStatus.PROPOSED
        raise ValueError(f"Unsupported ADR status: {raw_status}")

    @staticmethod
    def _summarize_decision(decision_text: str) -> str:
        paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", decision_text) if paragraph.strip()]
        if not paragraphs:
            raise ValueError("ADR decision section must not be empty.")
        return re.sub(r"\s+", " ", paragraphs[0])

    @staticmethod
    def _split_adr_title(title: str, path: Path) -> tuple[str, str]:
        clean_title = ADRLoader._strip_quotes(title).strip()
        match = _TITLE_WITH_ID_PATTERN.match(clean_title)
        if match:
            adr_id = ADRLoader._normalize_adr_id(match.group("id"))
            return adr_id, match.group("title").strip()

        inferred_id = ADRLoader._infer_adr_id(path)
        return inferred_id, clean_title

    @staticmethod
    def _infer_adr_id(path: Path) -> str:
        match = _ADR_ID_PATTERN.search(path.stem)
        if not match:
            raise ValueError(f"Could not infer ADR id from path: {path}")
        return ADRLoader._normalize_adr_id(match.group(1))

    @staticmethod
    def _normalize_adr_id(raw_id: str) -> str:
        digits = re.sub(r"\D", "", raw_id)
        if not digits:
            raise ValueError(f"Invalid ADR id: {raw_id}")
        return f"ADR-{digits.zfill(4)}"

    @staticmethod
    def _extract_adr_number(adr_id: str) -> int:
        return int(re.sub(r"\D", "", adr_id))

    @staticmethod
    def _strip_quotes(value: str) -> str:
        return value.strip().strip("\"'")

    @staticmethod
    def _format_context_entry(document: ADRDocument) -> str:
        return "\n".join(
            [
                f"### {document.id}: {document.title}",
                f"Status: {document.status.value}",
                f"Decision Summary: {document.decision_summary}",
                "",
                document.full_text,
            ]
        )
