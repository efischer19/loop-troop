"""Local evaluation CLI — Bake-off Tool.

This module provides the ``loop-troop-eval`` command, which runs controlled
bake-off evaluations comparing different Ollama model configurations against a
set of benchmark tasks.

No real GitHub API calls are made during evaluation: tasks are processed using
only the LLM client.  The benchmark definition file is a TOML (or YAML) file
that declares model configurations and tasks with assertions.

Usage::

    loop-troop-eval benchmarks/loop_troop.toml
    loop-troop-eval benchmarks/loop_troop.toml --tier T1
    loop-troop-eval benchmarks/loop_troop.toml --output results.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from loop_troop.core.llm_client import LLMClient
from loop_troop.core.metrics import LLMMetrics, MetricsCollector
from loop_troop.execution import WorkerTier

# ---------------------------------------------------------------------------
# LLM response schema used for all eval tasks
# ---------------------------------------------------------------------------


class EvalResponse(BaseModel):
    """Structured LLM response captured for every eval task."""

    checklist: list[str] = Field(
        description="Ordered list of implementation steps or action items for the issue."
    )
    reasoning: str = Field(
        description="Brief reasoning that explains the chosen approach."
    )


# ---------------------------------------------------------------------------
# In-memory metrics collector
# ---------------------------------------------------------------------------


class _CapturingMetricsCollector(MetricsCollector):
    """MetricsCollector that stores records in memory instead of SQLite."""

    def __init__(self) -> None:
        super().__init__(shadow_log=None)
        self.all_metrics: list[LLMMetrics] = []

    def record(self, metrics: LLMMetrics) -> None:  # type: ignore[override]
        self.all_metrics.append(metrics)


# ---------------------------------------------------------------------------
# Benchmark data structures
# ---------------------------------------------------------------------------


@dataclass
class TaskAssertion:
    """A single assertion to check against an :class:`EvalResponse`."""

    type: str
    value: Any = None


@dataclass
class BenchmarkTask:
    """A single eval task derived from a GitHub issue body."""

    id: str
    tier: str  # "T1", "T2", or "T3"
    issue_body: str
    assertions: list[TaskAssertion] = field(default_factory=list)


@dataclass
class ModelConfig:
    """A model configuration entry from the benchmark definition file."""

    name: str
    tier: str  # "T1", "T2", or "T3"
    model: str


@dataclass
class BenchmarkSuite:
    """A complete benchmark definition loaded from a TOML/YAML file."""

    title: str
    model_configs: list[ModelConfig]
    tasks: list[BenchmarkTask]


@dataclass
class TaskRunResult:
    """Outcome of running a single (task, model_config) pair."""

    task_id: str
    model_config_name: str
    model_name: str
    tier: str
    passed: bool
    failed_assertions: list[str]
    metrics: LLMMetrics | None
    wall_clock_ms: float
    error: str | None = None


# ---------------------------------------------------------------------------
# Benchmark file parsing
# ---------------------------------------------------------------------------


def _parse_assertion(raw: dict[str, Any]) -> TaskAssertion:
    assertion_type = raw.get("type")
    if not assertion_type:
        raise ValueError("Each assertion must have a 'type' field.")
    return TaskAssertion(type=str(assertion_type), value=raw.get("value"))


def _parse_task(raw: dict[str, Any]) -> BenchmarkTask:
    task_id = raw.get("id")
    if not task_id:
        raise ValueError("Each task must have an 'id' field.")
    tier = str(raw.get("tier", "T2")).upper()
    if tier not in ("T1", "T2", "T3"):
        raise ValueError(
            f"Task '{task_id}' has invalid tier '{tier}'. Must be T1, T2, or T3."
        )
    issue_body = raw.get("issue_body")
    if not issue_body:
        raise ValueError(f"Task '{task_id}' must have an 'issue_body' field.")
    assertions = [_parse_assertion(a) for a in raw.get("assertions", [])]
    return BenchmarkTask(
        id=str(task_id),
        tier=tier,
        issue_body=str(issue_body),
        assertions=assertions,
    )


def _parse_model_config(raw: dict[str, Any]) -> ModelConfig:
    name = raw.get("name")
    if not name:
        raise ValueError("Each model_config must have a 'name' field.")
    tier = str(raw.get("tier", "T2")).upper()
    if tier not in ("T1", "T2", "T3"):
        raise ValueError(
            f"Model config '{name}' has invalid tier '{tier}'. Must be T1, T2, or T3."
        )
    model = raw.get("model")
    if not model:
        raise ValueError(f"Model config '{name}' must have a 'model' field.")
    return ModelConfig(name=str(name), tier=tier, model=str(model))


def parse_benchmark_file(path: Path) -> BenchmarkSuite:
    """Parse a TOML or YAML benchmark definition file into a :class:`BenchmarkSuite`.

    TOML files (``.toml``) are parsed with the stdlib ``tomllib`` module.
    YAML files (``.yaml`` / ``.yml``) require ``PyYAML`` to be installed.

    Raises
    ------
    ValueError
        If the file format is unsupported or the file contains invalid data.
    ImportError
        If a YAML file is provided but PyYAML is not installed.
    """
    suffix = path.suffix.lower()
    if suffix == ".toml":
        with path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            raise ImportError(
                "PyYAML is required to parse YAML benchmark files. "
                "Install it with: pip install pyyaml"
            ) from None
        with path.open() as fh:
            loaded = yaml.safe_load(fh)
        if not isinstance(loaded, dict):
            raise ValueError(
                "YAML benchmark file must contain a mapping at the top level."
            )
        raw = loaded
    else:
        raise ValueError(
            f"Unsupported benchmark file format '{suffix}'. Use .toml or .yaml/.yml."
        )

    title = str(raw.get("title", "Unnamed Benchmark Suite"))
    model_configs = [_parse_model_config(mc) for mc in raw.get("model_configs", [])]
    tasks = [_parse_task(t) for t in raw.get("tasks", [])]
    return BenchmarkSuite(title=title, model_configs=model_configs, tasks=tasks)


# ---------------------------------------------------------------------------
# Assertion checking
# ---------------------------------------------------------------------------


def check_assertions(
    response: EvalResponse,
    assertions: list[TaskAssertion],
) -> list[str]:
    """Evaluate all *assertions* against *response*.

    Returns a (possibly empty) list of human-readable failure messages.
    An empty list means every assertion passed.
    """
    failures: list[str] = []
    for assertion in assertions:
        atype = assertion.type
        if atype == "min_checklist_items":
            expected = int(assertion.value)
            actual = len(response.checklist)
            if actual < expected:
                failures.append(
                    f"min_checklist_items: expected \u2265{expected} items, got {actual}"
                )
        elif atype == "max_checklist_items":
            expected = int(assertion.value)
            actual = len(response.checklist)
            if actual > expected:
                failures.append(
                    f"max_checklist_items: expected \u2264{expected} items, got {actual}"
                )
        elif atype == "checklist_item_contains":
            needle = str(assertion.value)
            if not any(needle.lower() in item.lower() for item in response.checklist):
                failures.append(
                    f"checklist_item_contains: no item contains '{needle}'"
                )
        elif atype == "has_reasoning":
            if not response.reasoning.strip():
                failures.append("has_reasoning: response has empty reasoning")
        else:
            failures.append(f"unknown assertion type: '{atype}'")
    return failures


# ---------------------------------------------------------------------------
# Tier-specific system prompts
# ---------------------------------------------------------------------------

_TIER_SYSTEM_PROMPTS: dict[str, str] = {
    "T1": (
        "You are a fast dispatch classifier for a software development automation system. "
        "Given a GitHub issue body, produce a concise checklist of the key implementation "
        "steps needed to address the issue, along with brief reasoning."
    ),
    "T2": (
        "You are a senior software architect. Given a GitHub issue body, produce a detailed "
        "checklist of implementation steps that considers design decisions and trade-offs, "
        "along with clear reasoning about the architectural approach."
    ),
    "T3": (
        "You are an expert software engineer. Given a GitHub issue body, produce a precise "
        "checklist of code changes, tests to write, and files to modify, along with "
        "reasoning about the implementation strategy."
    ),
}


def _build_messages(task: BenchmarkTask) -> list[dict[str, Any]]:
    """Build the LLM message list for an eval task."""
    system_prompt = _TIER_SYSTEM_PROMPTS.get(task.tier, _TIER_SYSTEM_PROMPTS["T2"])
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"GitHub Issue:\n\n{task.issue_body}\n\n"
                "Produce a checklist of implementation steps and explain your reasoning."
            ),
        },
    ]


# ---------------------------------------------------------------------------
# Single-task runner
# ---------------------------------------------------------------------------


def run_task(
    task: BenchmarkTask,
    model_config: ModelConfig,
    llm_client: Any,
    collector: _CapturingMetricsCollector,
) -> TaskRunResult:
    """Run *task* against *model_config* and return a :class:`TaskRunResult`.

    *collector* is used to read back the :class:`~loop_troop.core.metrics.LLMMetrics`
    recorded during the LLM call.  If *llm_client* is a duck-typed fake that
    does not call ``collector.record``, the result's *metrics* field will be
    ``None``.
    """
    tier = WorkerTier(model_config.tier)
    messages = _build_messages(task)
    started_at = time.perf_counter()
    error: str | None = None
    passed = False
    failed_assertions: list[str] = []
    pre_count = len(collector.all_metrics)

    try:
        response: EvalResponse = llm_client.complete_structured(
            tier=tier,
            response_model=EvalResponse,
            messages=messages,
            model_override=model_config.model,
            event_id=f"eval:{task.id}",
        )
        failed_assertions = check_assertions(response, task.assertions)
        passed = len(failed_assertions) == 0
    except Exception as exc:
        error = str(exc)
    finally:
        wall_clock_ms = round((time.perf_counter() - started_at) * 1000, 3)

    new_metrics = collector.all_metrics[pre_count:]
    last_metric = new_metrics[-1] if new_metrics else None

    return TaskRunResult(
        task_id=task.id,
        model_config_name=model_config.name,
        model_name=model_config.model,
        tier=model_config.tier,
        passed=passed,
        failed_assertions=failed_assertions,
        metrics=last_metric,
        wall_clock_ms=wall_clock_ms,
        error=error,
    )


# ---------------------------------------------------------------------------
# Eval orchestrator
# ---------------------------------------------------------------------------


def run_eval(
    suite: BenchmarkSuite,
    *,
    tier_filter: str | None = None,
    ollama_host: str | None = None,
    llm_client: Any | None = None,
) -> list[TaskRunResult]:
    """Run all benchmark tasks against matching model configs.

    Each task is matched to model configs that share its *tier*.  When
    *tier_filter* is set, only tasks and model configs belonging to that tier
    are evaluated.

    Parameters
    ----------
    suite:
        The :class:`BenchmarkSuite` to evaluate.
    tier_filter:
        When set to ``"T1"``, ``"T2"``, or ``"T3"``, only tasks and model
        configs belonging to that tier are evaluated.
    ollama_host:
        Ollama base URL forwarded to :class:`~loop_troop.core.llm_client.LLMClient`
        when *llm_client* is ``None``.
    llm_client:
        An :class:`~loop_troop.core.llm_client.LLMClient` instance (or
        duck-typed equivalent) to use for LLM calls.  When ``None`` a new
        :class:`LLMClient` is created, wired to a
        :class:`_CapturingMetricsCollector`.  Passing a fake client is the
        recommended approach for unit tests.
    """
    collector = _CapturingMetricsCollector()
    _client = llm_client if llm_client is not None else LLMClient(
        ollama_host=ollama_host,
        metrics_collector=collector,
    )

    tier_upper = tier_filter.upper() if tier_filter else None

    results: list[TaskRunResult] = []
    for task in suite.tasks:
        if tier_upper is not None and task.tier != tier_upper:
            continue
        matching_configs = [
            mc for mc in suite.model_configs if mc.tier == task.tier
        ]
        for model_config in matching_configs:
            result = run_task(task, model_config, _client, collector)
            results.append(result)

    return results


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _format_table(headers: list[str], rows: list[list[Any]]) -> str:
    """Render *headers* and *rows* as a simple ASCII box table."""
    if not rows:
        return ""
    str_rows = [[str(cell) for cell in row] for row in rows]
    col_widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(cell))

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header_row = (
        "|"
        + "|".join(f" {h:<{col_widths[i]}} " for i, h in enumerate(headers))
        + "|"
    )
    lines = [sep, header_row, sep]
    for row in str_rows:
        lines.append(
            "|"
            + "|".join(f" {cell:<{col_widths[i]}} " for i, cell in enumerate(row))
            + "|"
        )
        lines.append(sep)
    return "\n".join(lines)


def generate_report(results: list[TaskRunResult], title: str = "") -> str:
    """Build a human-readable summary report from *results*.

    The report is an ASCII table showing per-model-config aggregates:
    pass/fail counts, average TTFT, average retries, total token usage, and
    average wall-clock time.
    """
    if not results:
        return "No evaluation results to report.\n"

    from collections import defaultdict

    model_results: dict[str, list[TaskRunResult]] = defaultdict(list)
    for r in results:
        model_results[r.model_config_name].append(r)

    headers = [
        "Config",
        "Model",
        "Tier",
        "Tasks",
        "Pass",
        "Fail",
        "Pass%",
        "Avg TTFT ms",
        "Avg Retries",
        "Total Tokens",
        "Avg Wall ms",
    ]
    rows: list[list[Any]] = []
    for config_name, config_results in sorted(model_results.items()):
        total = len(config_results)
        passed = sum(1 for r in config_results if r.passed)
        failed = total - passed
        pass_pct = f"{100 * passed / total:.0f}%" if total > 0 else "N/A"

        ttft_values = [
            r.metrics.ttft_ms
            for r in config_results
            if r.metrics is not None and r.metrics.ttft_ms is not None
        ]
        avg_ttft = (
            f"{sum(ttft_values) / len(ttft_values):.1f}" if ttft_values else "N/A"
        )

        retry_values = [
            r.metrics.instructor_retries
            for r in config_results
            if r.metrics is not None
        ]
        avg_retries = (
            f"{sum(retry_values) / len(retry_values):.2f}" if retry_values else "N/A"
        )

        total_tokens = sum(
            (r.metrics.prompt_tokens or 0) + (r.metrics.completion_tokens or 0)
            for r in config_results
            if r.metrics is not None
        )

        avg_wall = f"{sum(r.wall_clock_ms for r in config_results) / total:.1f}"
        model_name = config_results[0].model_name
        tier = config_results[0].tier

        rows.append(
            [
                config_name,
                model_name,
                tier,
                total,
                passed,
                failed,
                pass_pct,
                avg_ttft,
                avg_retries,
                total_tokens,
                avg_wall,
            ]
        )

    heading = f"Bake-off Report: {title}" if title else "Bake-off Report"
    divider = "=" * len(heading)
    table = _format_table(headers, rows)
    return f"{heading}\n{divider}\n\n{table}\n"


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


def _result_to_dict(result: TaskRunResult) -> dict[str, Any]:
    """Serialize a :class:`TaskRunResult` to a JSON-compatible dict.

    :func:`dataclasses.asdict` recursively converts nested dataclasses
    (including :class:`~loop_troop.core.metrics.LLMMetrics`).
    """
    return dataclasses.asdict(result)


def results_to_json(results: list[TaskRunResult], title: str = "") -> str:
    """Serialize all *results* to an indented JSON string."""
    payload: dict[str, Any] = {
        "title": title,
        "results": [_result_to_dict(r) for r in results],
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-troop-eval",
        description=(
            "Run a bake-off evaluation comparing Ollama model configurations "
            "against benchmark tasks defined in a TOML or YAML file."
        ),
    )
    parser.add_argument(
        "benchmark",
        type=Path,
        help="Path to a TOML or YAML benchmark definition file.",
    )
    parser.add_argument(
        "--tier",
        choices=["T1", "T2", "T3"],
        default=None,
        help="Limit evaluation to a specific tier (T1, T2, or T3).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write detailed JSON results to this file.",
    )
    parser.add_argument(
        "--ollama-host",
        default=None,
        help="Ollama base URL (overrides the OLLAMA_HOST environment variable).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        suite = parse_benchmark_file(args.benchmark)
    except (ValueError, OSError, ImportError) as exc:
        print(f"Error: failed to load benchmark file: {exc}", file=sys.stderr)
        return 1

    results = run_eval(
        suite,
        tier_filter=args.tier,
        ollama_host=args.ollama_host,
    )

    print(generate_report(results, title=suite.title))

    if args.output is not None:
        args.output.write_text(results_to_json(results, title=suite.title))
        print(f"JSON results written to {args.output}", file=sys.stderr)

    return 0
