"""Unit tests for the bake-off evaluation CLI (loop_troop.eval)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from loop_troop.eval import (
    BenchmarkSuite,
    BenchmarkTask,
    EvalResponse,
    ModelConfig,
    TaskAssertion,
    TaskRunResult,
    _CapturingMetricsCollector,
    check_assertions,
    generate_report,
    parse_args,
    parse_benchmark_file,
    results_to_json,
    run_eval,
)

# ---------------------------------------------------------------------------
# TOML fixtures
# ---------------------------------------------------------------------------

MINIMAL_TOML = """\
title = "Test Suite"

[[model_configs]]
name = "fast"
tier = "T1"
model = "qwen:7b"

[[model_configs]]
name = "smart"
tier = "T2"
model = "qwen:14b"

[[tasks]]
id = "task-1"
tier = "T1"
issue_body = "Add a --version flag to the CLI."

  [[tasks.assertions]]
  type = "min_checklist_items"
  value = 1

  [[tasks.assertions]]
  type = "max_checklist_items"
  value = 5

[[tasks]]
id = "task-2"
tier = "T2"
issue_body = "Design a caching layer for Ollama responses."

  [[tasks.assertions]]
  type = "has_reasoning"
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    task_id: str,
    config_name: str,
    model_name: str,
    tier: str = "T1",
    passed: bool = True,
    wall_clock_ms: float = 100.0,
) -> TaskRunResult:
    return TaskRunResult(
        task_id=task_id,
        model_config_name=config_name,
        model_name=model_name,
        tier=tier,
        passed=passed,
        failed_assertions=[] if passed else ["something failed"],
        metrics=None,
        wall_clock_ms=wall_clock_ms,
    )


def _make_suite(
    t1_configs: int = 1,
    t2_configs: int = 1,
    t1_tasks: int = 1,
    t2_tasks: int = 1,
) -> BenchmarkSuite:
    model_configs = [
        ModelConfig(name=f"t1-m{i}", tier="T1", model=f"qwen:7b-{i}")
        for i in range(t1_configs)
    ] + [
        ModelConfig(name=f"t2-m{i}", tier="T2", model=f"qwen:14b-{i}")
        for i in range(t2_configs)
    ]
    tasks = [
        BenchmarkTask(id=f"t1-task-{i}", tier="T1", issue_body=f"T1 task body {i}")
        for i in range(t1_tasks)
    ] + [
        BenchmarkTask(id=f"t2-task-{i}", tier="T2", issue_body=f"T2 task body {i}")
        for i in range(t2_tasks)
    ]
    return BenchmarkSuite(title="Test Suite", model_configs=model_configs, tasks=tasks)


class _FakeLLMClient:
    """Duck-typed LLMClient that returns a canned EvalResponse."""

    def __init__(self, checklist: list[str] | None = None, reasoning: str = "fake reason") -> None:
        self.checklist = checklist if checklist is not None else ["step 1", "step 2"]
        self.reasoning = reasoning
        self.calls: list[dict[str, Any]] = []

    def complete_structured(self, **kwargs: Any) -> EvalResponse:
        self.calls.append(kwargs)
        return EvalResponse(checklist=self.checklist, reasoning=self.reasoning)


class _ErrorLLMClient:
    """Duck-typed LLMClient that always raises."""

    def complete_structured(self, **_kwargs: Any) -> EvalResponse:
        raise RuntimeError("LLM call failed")


# ---------------------------------------------------------------------------
# parse_benchmark_file — TOML
# ---------------------------------------------------------------------------


def test_parse_benchmark_file_toml(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(MINIMAL_TOML)

    suite = parse_benchmark_file(bench_file)

    assert suite.title == "Test Suite"
    assert len(suite.model_configs) == 2
    assert suite.model_configs[0].name == "fast"
    assert suite.model_configs[0].tier == "T1"
    assert suite.model_configs[0].model == "qwen:7b"
    assert len(suite.tasks) == 2
    assert suite.tasks[0].id == "task-1"
    assert suite.tasks[0].tier == "T1"
    assert "version flag" in suite.tasks[0].issue_body
    assert len(suite.tasks[0].assertions) == 2
    assert suite.tasks[0].assertions[0].type == "min_checklist_items"
    assert suite.tasks[0].assertions[0].value == 1
    assert suite.tasks[1].assertions[0].type == "has_reasoning"


def test_parse_benchmark_file_defaults_title(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        '[[tasks]]\nid = "t"\ntier = "T1"\nissue_body = "body"\n'
    )
    suite = parse_benchmark_file(bench_file)
    assert suite.title == "Unnamed Benchmark Suite"


def test_parse_benchmark_file_tier_normalised_to_upper(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        '[[tasks]]\nid = "t"\ntier = "t1"\nissue_body = "body"\n'
    )
    suite = parse_benchmark_file(bench_file)
    assert suite.tasks[0].tier == "T1"


def test_parse_benchmark_file_invalid_tier_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        '[[tasks]]\nid = "t1"\ntier = "TX"\nissue_body = "body"\n'
    )
    with pytest.raises(ValueError, match="invalid tier"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_missing_task_id_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text('[[tasks]]\ntier = "T1"\nissue_body = "body"\n')
    with pytest.raises(ValueError, match="must have an 'id' field"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_missing_issue_body_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text('[[tasks]]\nid = "t1"\ntier = "T1"\n')
    with pytest.raises(ValueError, match="must have an 'issue_body' field"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_missing_model_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text('[[model_configs]]\nname = "x"\ntier = "T1"\n')
    with pytest.raises(ValueError, match="must have a 'model' field"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_model_invalid_tier_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        '[[model_configs]]\nname = "x"\ntier = "T9"\nmodel = "qwen:7b"\n'
    )
    with pytest.raises(ValueError, match="invalid tier"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_missing_assertion_type_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        '[[tasks]]\nid = "t"\ntier = "T1"\nissue_body = "body"\n'
        "  [[tasks.assertions]]\n  value = 3\n"
    )
    with pytest.raises(ValueError, match="must have a 'type' field"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_unsupported_format_raises(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.json"
    bench_file.write_text("{}")
    with pytest.raises(ValueError, match="Unsupported benchmark file format"):
        parse_benchmark_file(bench_file)


def test_parse_benchmark_file_empty_model_configs(tmp_path: Path) -> None:
    bench_file = tmp_path / "bench.toml"
    bench_file.write_text(
        'title = "Empty"\n[[tasks]]\nid = "t"\ntier = "T1"\nissue_body = "body"\n'
    )
    suite = parse_benchmark_file(bench_file)
    assert suite.model_configs == []
    assert len(suite.tasks) == 1


# ---------------------------------------------------------------------------
# check_assertions
# ---------------------------------------------------------------------------


def _resp(items: list[str] | None = None, reasoning: str = "some reason") -> EvalResponse:
    return EvalResponse(checklist=items or ["step 1", "step 2"], reasoning=reasoning)


def test_check_assertions_empty_list_always_passes() -> None:
    assert check_assertions(_resp(), []) == []


def test_check_assertions_min_checklist_passes() -> None:
    assert check_assertions(_resp(["a", "b"]), [TaskAssertion("min_checklist_items", 2)]) == []


def test_check_assertions_min_checklist_fails() -> None:
    failures = check_assertions(_resp(["a"]), [TaskAssertion("min_checklist_items", 3)])
    assert len(failures) == 1
    assert "min_checklist_items" in failures[0]
    assert "got 1" in failures[0]


def test_check_assertions_max_checklist_passes() -> None:
    assert check_assertions(_resp(["a", "b"]), [TaskAssertion("max_checklist_items", 5)]) == []


def test_check_assertions_max_checklist_fails() -> None:
    failures = check_assertions(
        _resp(["a", "b", "c"]), [TaskAssertion("max_checklist_items", 2)]
    )
    assert len(failures) == 1
    assert "max_checklist_items" in failures[0]
    assert "got 3" in failures[0]


def test_check_assertions_contains_passes_case_insensitive() -> None:
    assert (
        check_assertions(
            _resp(["Write Tests", "update docs"]),
            [TaskAssertion("checklist_item_contains", "tests")],
        )
        == []
    )


def test_check_assertions_contains_fails() -> None:
    failures = check_assertions(
        _resp(["write tests"]),
        [TaskAssertion("checklist_item_contains", "deploy")],
    )
    assert len(failures) == 1
    assert "deploy" in failures[0]


def test_check_assertions_has_reasoning_passes() -> None:
    assert (
        check_assertions(_resp(reasoning="good reason"), [TaskAssertion("has_reasoning")])
        == []
    )


def test_check_assertions_has_reasoning_fails_on_empty() -> None:
    failures = check_assertions(_resp(reasoning="   "), [TaskAssertion("has_reasoning")])
    assert len(failures) == 1
    assert "has_reasoning" in failures[0]


def test_check_assertions_unknown_type_is_reported() -> None:
    failures = check_assertions(_resp(), [TaskAssertion("nonexistent_assertion")])
    assert len(failures) == 1
    assert "unknown assertion type" in failures[0]


def test_check_assertions_multiple_failures_all_reported() -> None:
    assertions = [
        TaskAssertion("min_checklist_items", 10),  # fails — only 2 items
        TaskAssertion("checklist_item_contains", "nonexistent"),  # fails
    ]
    failures = check_assertions(_resp(), assertions)
    assert len(failures) == 2


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------


def test_generate_report_empty_results() -> None:
    report = generate_report([])
    assert "No evaluation results" in report


def test_generate_report_contains_heading() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    report = generate_report(results, title="My Suite")
    assert "Bake-off Report: My Suite" in report


def test_generate_report_without_title() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    report = generate_report(results)
    assert "Bake-off Report" in report


def test_generate_report_shows_model_and_config() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    report = generate_report(results)
    assert "fast" in report
    assert "qwen:7b" in report


def test_generate_report_pass_rate_100_percent() -> None:
    results = [
        _make_result("t1", "fast", "qwen:7b", passed=True),
        _make_result("t2", "fast", "qwen:7b", passed=True),
    ]
    report = generate_report(results)
    assert "100%" in report


def test_generate_report_pass_rate_50_percent() -> None:
    results = [
        _make_result("t1", "fast", "qwen:7b", passed=True),
        _make_result("t2", "fast", "qwen:7b", passed=False),
    ]
    report = generate_report(results)
    assert "50%" in report


def test_generate_report_multiple_configs() -> None:
    results = [
        _make_result("t1", "small", "qwen:7b", passed=True),
        _make_result("t1", "large", "qwen:32b", passed=True),
        _make_result("t2", "small", "qwen:7b", passed=False),
        _make_result("t2", "large", "qwen:32b", passed=True),
    ]
    report = generate_report(results)
    assert "small" in report
    assert "large" in report
    assert "qwen:7b" in report
    assert "qwen:32b" in report


def test_generate_report_na_when_no_metrics() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    report = generate_report(results)
    assert "N/A" in report  # TTFT and retries are N/A without real metrics


# ---------------------------------------------------------------------------
# run_eval — tier filtering
# ---------------------------------------------------------------------------


def test_run_eval_no_filter_runs_all_tasks() -> None:
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=1, t2_configs=1, t1_tasks=1, t2_tasks=1)
    results = run_eval(suite, llm_client=fake)
    assert len(results) == 2
    task_ids = {r.task_id for r in results}
    assert "t1-task-0" in task_ids
    assert "t2-task-0" in task_ids


def test_run_eval_tier_filter_t1_only() -> None:
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=1, t2_configs=1, t1_tasks=2, t2_tasks=2)
    results = run_eval(suite, tier_filter="T1", llm_client=fake)
    assert len(results) == 2
    assert all(r.tier == "T1" for r in results)


def test_run_eval_tier_filter_t2_only() -> None:
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=1, t2_configs=1, t1_tasks=2, t2_tasks=1)
    results = run_eval(suite, tier_filter="T2", llm_client=fake)
    assert len(results) == 1
    assert results[0].tier == "T2"


def test_run_eval_tier_filter_t3_no_matching() -> None:
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=1, t2_configs=1, t1_tasks=1, t2_tasks=1)
    results = run_eval(suite, tier_filter="T3", llm_client=fake)
    assert results == []


def test_run_eval_task_matched_to_same_tier_config() -> None:
    """T1 tasks must only be run against T1 model configs, not T2."""
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=1, t2_configs=1, t1_tasks=1, t2_tasks=1)
    results = run_eval(suite, llm_client=fake)
    t1_results = [r for r in results if r.tier == "T1"]
    t2_results = [r for r in results if r.tier == "T2"]
    assert all(r.model_config_name == "t1-m0" for r in t1_results)
    assert all(r.model_config_name == "t2-m0" for r in t2_results)


def test_run_eval_multiple_model_configs_per_tier() -> None:
    fake = _FakeLLMClient()
    suite = _make_suite(t1_configs=2, t2_configs=0, t1_tasks=1, t2_tasks=0)
    results = run_eval(suite, llm_client=fake)
    # 1 T1 task × 2 T1 model configs = 2 results
    assert len(results) == 2
    config_names = {r.model_config_name for r in results}
    assert config_names == {"t1-m0", "t1-m1"}


def test_run_eval_no_matching_model_config_produces_no_results() -> None:
    fake = _FakeLLMClient()
    # Task tier T3 but only T1 and T2 configs defined
    suite = BenchmarkSuite(
        title="Test",
        model_configs=[ModelConfig(name="m", tier="T1", model="qwen:7b")],
        tasks=[BenchmarkTask(id="t", tier="T3", issue_body="body")],
    )
    results = run_eval(suite, llm_client=fake)
    assert results == []


# ---------------------------------------------------------------------------
# run_eval — assertion checking
# ---------------------------------------------------------------------------


def test_run_eval_passing_assertion() -> None:
    fake = _FakeLLMClient(checklist=["step 1", "step 2"])
    suite = BenchmarkSuite(
        title="Pass",
        model_configs=[ModelConfig(name="m", tier="T1", model="qwen:7b")],
        tasks=[
            BenchmarkTask(
                id="easy",
                tier="T1",
                issue_body="Do something",
                assertions=[TaskAssertion("min_checklist_items", 1)],
            )
        ],
    )
    results = run_eval(suite, llm_client=fake)
    assert results[0].passed is True
    assert results[0].failed_assertions == []


def test_run_eval_failing_assertion() -> None:
    fake = _FakeLLMClient(checklist=["step 1", "step 2"])
    suite = BenchmarkSuite(
        title="Fail",
        model_configs=[ModelConfig(name="m", tier="T1", model="qwen:7b")],
        tasks=[
            BenchmarkTask(
                id="strict",
                tier="T1",
                issue_body="Do something",
                assertions=[TaskAssertion("max_checklist_items", 1)],
            )
        ],
    )
    results = run_eval(suite, llm_client=fake)
    assert results[0].passed is False
    assert len(results[0].failed_assertions) == 1


def test_run_eval_llm_error_recorded() -> None:
    suite = BenchmarkSuite(
        title="Error",
        model_configs=[ModelConfig(name="m", tier="T1", model="qwen:7b")],
        tasks=[BenchmarkTask(id="t", tier="T1", issue_body="body")],
    )
    results = run_eval(suite, llm_client=_ErrorLLMClient())
    assert len(results) == 1
    result = results[0]
    assert result.passed is False
    assert result.error is not None
    assert "LLM call failed" in result.error


def test_run_eval_result_fields_populated() -> None:
    fake = _FakeLLMClient()
    suite = BenchmarkSuite(
        title="Fields",
        model_configs=[ModelConfig(name="myconfig", tier="T1", model="qwen:7b")],
        tasks=[BenchmarkTask(id="mytask", tier="T1", issue_body="body")],
    )
    results = run_eval(suite, llm_client=fake)
    r = results[0]
    assert r.task_id == "mytask"
    assert r.model_config_name == "myconfig"
    assert r.model_name == "qwen:7b"
    assert r.tier == "T1"
    assert r.wall_clock_ms >= 0


# ---------------------------------------------------------------------------
# results_to_json
# ---------------------------------------------------------------------------


def test_results_to_json_valid_json() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    raw = results_to_json(results, title="Suite")
    payload = json.loads(raw)
    assert payload["title"] == "Suite"
    assert isinstance(payload["results"], list)
    assert len(payload["results"]) == 1


def test_results_to_json_fields_present() -> None:
    results = [_make_result("t1", "fast", "qwen:7b")]
    payload = json.loads(results_to_json(results))
    item = payload["results"][0]
    assert item["task_id"] == "t1"
    assert item["model_config_name"] == "fast"
    assert item["model_name"] == "qwen:7b"
    assert item["tier"] == "T1"
    assert item["passed"] is True
    assert item["failed_assertions"] == []
    assert item["metrics"] is None


def test_results_to_json_empty_results() -> None:
    payload = json.loads(results_to_json([]))
    assert payload["results"] == []


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_benchmark_only(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    args = parse_args([str(bench)])
    assert args.benchmark == bench
    assert args.tier is None
    assert args.output is None
    assert args.ollama_host is None


def test_parse_args_tier_t1(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    args = parse_args([str(bench), "--tier", "T1"])
    assert args.tier == "T1"


def test_parse_args_tier_t2(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    args = parse_args([str(bench), "--tier", "T2"])
    assert args.tier == "T2"


def test_parse_args_tier_t3(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    args = parse_args([str(bench), "--tier", "T3"])
    assert args.tier == "T3"


def test_parse_args_output_flag(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    out = tmp_path / "results.json"
    args = parse_args([str(bench), "--output", str(out)])
    assert args.output == out


def test_parse_args_ollama_host(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    args = parse_args([str(bench), "--ollama-host", "http://10.0.0.1:11434"])
    assert args.ollama_host == "http://10.0.0.1:11434"


def test_parse_args_invalid_tier_exits(tmp_path: Path) -> None:
    bench = tmp_path / "bench.toml"
    bench.touch()
    with pytest.raises(SystemExit):
        parse_args([str(bench), "--tier", "T9"])


# ---------------------------------------------------------------------------
# _CapturingMetricsCollector
# ---------------------------------------------------------------------------


def test_capturing_collector_stores_metrics() -> None:
    from loop_troop.core.metrics import LLMMetrics

    collector = _CapturingMetricsCollector()
    m = LLMMetrics(
        call_id="c1",
        tier="T1",
        model_name="qwen:7b",
        prompt_tokens=10,
        completion_tokens=5,
        ttft_ms=None,
        total_latency_ms=123.0,
        instructor_retries=0,
        validation_errors=[],
        success=True,
    )
    collector.record(m)
    assert len(collector.all_metrics) == 1
    assert collector.all_metrics[0] is m


def test_capturing_collector_accumulates() -> None:
    from loop_troop.core.metrics import LLMMetrics

    collector = _CapturingMetricsCollector()

    def _metric(call_id: str) -> LLMMetrics:
        return LLMMetrics(
            call_id=call_id,
            tier="T1",
            model_name="m",
            prompt_tokens=1,
            completion_tokens=1,
            ttft_ms=None,
            total_latency_ms=1.0,
            instructor_retries=0,
            validation_errors=[],
            success=True,
        )

    collector.record(_metric("a"))
    collector.record(_metric("b"))
    assert len(collector.all_metrics) == 2
