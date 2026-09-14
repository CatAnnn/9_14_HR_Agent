from __future__ import annotations

from backend.config.settings import Settings
from backend.observability import metrics as metrics_module
from backend.services import langchain_llm_service as llm_module
from backend.services.langchain_llm_service import ModelFarmLangChainChatModel


class FakeCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int | float, dict[str, object]]] = []

    def add(self, value: int | float, attributes: dict[str, object]) -> None:
        self.calls.append((value, attributes))


def test_record_llm_token_usage_exports_input_and_output_counters(monkeypatch) -> None:
    counter = FakeCounter()
    monkeypatch.setattr(metrics_module, "_LLM_TOKEN_USAGE_COUNTER", counter)

    metrics_module.record_llm_token_usage(
        input_tokens=120,
        output_tokens=35,
        task_name="employee_reply",
        model_name="qwen3.6-flash",
        provider_name="bosch_openai_compatible",
        stream=True,
        request_outcome="success",
        input_estimated=False,
        output_estimated=True,
    )

    assert [value for value, _attributes in counter.calls] == [120, 35]
    input_attributes = counter.calls[0][1]
    output_attributes = counter.calls[1][1]
    assert input_attributes == {
        "hr_agent.task.name": "employee_reply",
        "gen_ai.request.model": "qwen3.6-flash",
        "gen_ai.provider.name": "bosch_openai_compatible",
        "hr_agent.stream": True,
        "hr_agent.request.outcome": "success",
        "hr_agent.user.id": "system",
        "gen_ai.token.type": "input",
        "hr_agent.token.estimated": False,
    }
    assert output_attributes["gen_ai.token.type"] == "output"
    assert output_attributes["hr_agent.token.estimated"] is True


class FakeHistogram:
    def __init__(self) -> None:
        self.calls: list[tuple[int | float, dict[str, object]]] = []

    def record(self, value: int | float, attributes: dict[str, object]) -> None:
        self.calls.append((value, attributes))


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def is_recording(self) -> bool:
        return True

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_record_llm_request_exports_cost_and_pricing_coverage(monkeypatch) -> None:
    request_counter = FakeCounter()
    duration = FakeHistogram()
    cost_counter = FakeCounter()
    missing_counter = FakeCounter()
    span = FakeSpan()
    monkeypatch.setattr(metrics_module, "_LLM_REQUEST_COUNTER", request_counter)
    monkeypatch.setattr(metrics_module, "_LLM_REQUEST_DURATION", duration)
    monkeypatch.setattr(metrics_module, "_LLM_COST_COUNTER", cost_counter)
    monkeypatch.setattr(metrics_module, "_LLM_PRICING_MISSING_COUNTER", missing_counter)
    monkeypatch.setattr(metrics_module.otel_trace, "get_current_span", lambda: span)

    metrics_module.record_llm_request(
        input_tokens=120,
        cached_input_tokens=20,
        output_tokens=35,
        input_cost_cny=0.0002,
        cached_input_cost_cny=0.00002,
        output_cost_cny=0.00028,
        task_name="guidance",
        model_name="glm-5.2",
        provider_name="bosch_openai_compatible",
        stream=True,
        request_outcome="success",
        duration_ms=8123,
        input_estimated=False,
        output_estimated=True,
        pricing_version="bosch-contract-2026-07",
        pricing_configured=True,
    )

    assert request_counter.calls[0][0] == 1
    assert duration.calls[0][0] == 8123
    assert [value for value, _attributes in cost_counter.calls] == [
        0.0002,
        0.00002,
        0.00028,
    ]
    assert missing_counter.calls == []
    assert cost_counter.calls[0][1]["gen_ai.token.type"] == "input"
    assert cost_counter.calls[1][1]["gen_ai.token.type"] == "cached_input"
    assert cost_counter.calls[2][1]["hr_agent.cost.estimated"] is True
    assert cost_counter.calls[0][1]["hr_agent.cost.currency"] == "CNY"
    assert span.attributes["hr_agent.llm.cost.cny"] == 0.0005
    assert span.attributes["hr_agent.llm.cost.currency"] == "CNY"
    assert span.attributes["gen_ai.usage.cached_input_tokens"] == 20
    assert span.attributes["hr_agent.llm.pricing.configured"] is True


def test_record_llm_request_counts_missing_price(monkeypatch) -> None:
    request_counter = FakeCounter()
    duration = FakeHistogram()
    cost_counter = FakeCounter()
    missing_counter = FakeCounter()
    monkeypatch.setattr(metrics_module, "_LLM_REQUEST_COUNTER", request_counter)
    monkeypatch.setattr(metrics_module, "_LLM_REQUEST_DURATION", duration)
    monkeypatch.setattr(metrics_module, "_LLM_COST_COUNTER", cost_counter)
    monkeypatch.setattr(metrics_module, "_LLM_PRICING_MISSING_COUNTER", missing_counter)
    monkeypatch.setattr(metrics_module.otel_trace, "get_current_span", lambda: FakeSpan())

    metrics_module.record_llm_request(
        input_tokens=10,
        cached_input_tokens=0,
        output_tokens=5,
        input_cost_cny=None,
        cached_input_cost_cny=None,
        output_cost_cny=None,
        task_name="employee_reply",
        model_name="unknown-model",
        provider_name="bosch_openai_compatible",
        stream=False,
        request_outcome="error",
        duration_ms=500,
        input_estimated=True,
        output_estimated=True,
        pricing_version="unconfigured",
        pricing_configured=False,
    )

    assert request_counter.calls[0][1]["hr_agent.request.outcome"] == "error"
    assert missing_counter.calls[0][0] == 1
    assert cost_counter.calls == []


def test_record_structured_output_exports_signoz_metrics_and_trace_attributes(monkeypatch) -> None:
    counter = FakeCounter()
    histogram = FakeHistogram()
    span = FakeSpan()
    monkeypatch.setattr(metrics_module, "_STRUCTURED_OUTPUT_COUNTER", counter)
    monkeypatch.setattr(metrics_module, "_STRUCTURED_OUTPUT_DURATION", histogram)
    monkeypatch.setattr(metrics_module.otel_trace, "get_current_span", lambda: span)

    metrics_module.record_structured_output(
        task_name="emotion_transition",
        schema_name="EmotionTransitionStructuredOutput",
        source="content_json",
        valid=True,
        recovered=True,
        repair_steps=["removed_markdown_fence", "normalized_transition_strategy"],
        fallback_reason="",
        duration_ms=8123,
    )

    assert counter.calls[0][0] == 1
    attributes = counter.calls[0][1]
    assert attributes["hr_agent.task.name"] == "emotion_transition"
    assert attributes["hr_agent.structured_output.outcome"] == "recovered"
    assert attributes["hr_agent.structured_output.source"] == "content_json"
    assert histogram.calls == [(8123, attributes)]
    assert span.attributes["hr_agent.structured_output.recovered"] is True
    assert span.attributes["hr_agent.structured_output.repair_steps"] == (
        "removed_markdown_fence,normalized_transition_strategy"
    )
    assert span.attributes["hr_agent.structured_output.fallback_reason"] == "none"


def test_record_llm_stream_timing_exports_milestones_and_trace_attributes(monkeypatch) -> None:
    histogram = FakeHistogram()
    span = FakeSpan()
    monkeypatch.setattr(metrics_module, "_LLM_STREAM_MILESTONE_DURATION", histogram)
    monkeypatch.setattr(metrics_module.otel_trace, "get_current_span", lambda: span)

    metrics_module.record_llm_stream_timing(
        task_name="guidance",
        model_name="glm-5.2",
        provider_name="bosch_openai_compatible",
        ttft_ms=120,
        thinking_complete_ms=860,
        tool_call_complete_ms=1420,
        complete=True,
    )

    assert [value for value, _attributes in histogram.calls] == [120, 860, 1420]
    assert [
        attributes["hr_agent.llm.stream.milestone"]
        for _value, attributes in histogram.calls
    ] == ["ttft", "thinking_complete", "tool_call_complete"]
    assert span.attributes["hr_agent.llm.stream.ttft_ms"] == 120
    assert span.attributes["hr_agent.llm.stream.thinking_complete_ms"] == 860
    assert span.attributes["hr_agent.llm.stream.tool_call_complete_ms"] == 1420


def test_llm_metric_uses_provider_usage_and_reports_total(monkeypatch) -> None:
    logged: dict[str, object] = {}
    recorded: dict[str, object] = {}
    request_recorded: dict[str, object] = {}
    monkeypatch.setattr(
        llm_module,
        "log_metric",
        lambda event, **fields: logged.update({"event": event, **fields}),
    )
    monkeypatch.setattr(
        llm_module,
        "record_llm_token_usage",
        lambda **fields: recorded.update(fields),
    )
    monkeypatch.setattr(
        llm_module,
        "record_llm_request",
        lambda **fields: request_recorded.update(fields),
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_pricing_version="bosch-contract-2026-07",
            llm_model_pricing={
                "bosch_openai_compatible/qwen3.6-flash": {
                    "input_per_million": 4.8,
                    "cached_input_per_million": 0.48,
                    "output_per_million": 28.8,
                    "tiers": [
                        {
                            "max_input_tokens": 256000,
                            "input_per_million": 1.2,
                            "cached_input_per_million": 0.12,
                            "output_per_million": 7.2,
                        }
                    ],
                }
            },
        ),
        task_name="employee_reply",
    )
    payload = {
        "model": "qwen3.6-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }

    model._log_llm_metric(
        payload,
        usage={
            "prompt_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens": 35,
            "total_tokens": 155,
        },
        duration_ms=800,
        error_count=0,
        output_text="reply",
    )

    assert logged["event"] == "llm.invoke"
    assert logged["llm_input_tokens"] == 120
    assert logged["llm_cached_input_tokens"] == 20
    assert logged["llm_output_tokens"] == 35
    assert logged["llm_total_tokens"] == 155
    assert logged["llm_token_usage_source"] == "provider"
    assert logged["llm_total_tokens_provider_reported"] is True
    assert logged["llm_cost_cny"] == 0.0003744
    assert logged["llm_cost_currency"] == "CNY"
    assert logged["llm_pricing_configured"] is True
    assert recorded == {
        "input_tokens": 120,
        "output_tokens": 35,
        "task_name": "employee_reply",
        "model_name": "qwen3.6-flash",
        "provider_name": "bosch_openai_compatible",
        "stream": False,
        "request_outcome": "success",
        "input_estimated": False,
        "output_estimated": False,
    }
    assert request_recorded["cached_input_tokens"] == 20
    assert request_recorded["input_cost_cny"] == 0.00012
    assert request_recorded["cached_input_cost_cny"] == 0.0000024
    assert request_recorded["output_cost_cny"] == 0.000252
    assert request_recorded["pricing_version"] == "bosch-contract-2026-07"
    assert request_recorded["pricing_configured"] is True


def test_streaming_llm_metric_marks_estimated_tokens(monkeypatch) -> None:
    logged: dict[str, object] = {}
    recorded: dict[str, object] = {}
    request_recorded: dict[str, object] = {}
    monkeypatch.setattr(
        llm_module,
        "log_metric",
        lambda event, **fields: logged.update({"event": event, **fields}),
    )
    monkeypatch.setattr(
        llm_module,
        "record_llm_token_usage",
        lambda **fields: recorded.update(fields),
    )
    monkeypatch.setattr(
        llm_module,
        "record_llm_request",
        lambda **fields: request_recorded.update(fields),
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_pricing_version="unconfigured",
            llm_model_pricing={},
        ),
        task_name="employee_reply",
    )

    model._log_llm_metric(
        {
            "model": "unpriced-model",
            "messages": [{"role": "user", "content": "stream input"}],
            "stream": True,
        },
        usage=None,
        duration_ms=500,
        error_count=0,
        output_text="stream output",
    )

    assert logged["llm_token_usage_source"] == "estimated"
    assert recorded["request_outcome"] == "success"
    assert logged["llm_total_tokens"] == (
        logged["llm_input_tokens"] + logged["llm_output_tokens"]
    )
    assert recorded["stream"] is True
    assert recorded["input_estimated"] is True
    assert recorded["output_estimated"] is True
    assert request_recorded["pricing_configured"] is False
    assert request_recorded["input_cost_cny"] is None
    assert request_recorded["cached_input_cost_cny"] is None
    assert logged["llm_cost_cny"] is None


def test_cached_input_token_count_supports_openai_and_direct_usage_shapes() -> None:
    assert metrics_module.cached_input_token_count_from_usage(
        {"prompt_tokens_details": {"cached_tokens": 120}}
    ) == 120
    assert metrics_module.cached_input_token_count_from_usage(
        {"input_tokens_details": {"cached_tokens": 80}}
    ) == 80
    assert metrics_module.cached_input_token_count_from_usage(
        {"cache_read_input_tokens": 40}
    ) == 40
    assert metrics_module.cached_input_token_count_from_usage(
        {"prompt_cache_hit_tokens": 60}
    ) == 60
    assert metrics_module.cached_input_token_count_from_usage(None) is None


def test_llm_metric_uses_long_context_pricing_tier(monkeypatch) -> None:
    logged: dict[str, object] = {}
    request_recorded: dict[str, object] = {}
    monkeypatch.setattr(
        llm_module,
        "log_metric",
        lambda event, **fields: logged.update({"event": event, **fields}),
    )
    monkeypatch.setattr(llm_module, "record_llm_token_usage", lambda **_fields: None)
    monkeypatch.setattr(
        llm_module,
        "record_llm_request",
        lambda **fields: request_recorded.update(fields),
    )
    model = ModelFarmLangChainChatModel(
        settings=Settings(
            llm_provider="bosch_openai_compatible",
            llm_model_pricing={
                "qwen3.6-flash": {
                    "input_per_million": 4.8,
                    "cached_input_per_million": 0.48,
                    "output_per_million": 28.8,
                    "tiers": [
                        {
                            "max_input_tokens": 256000,
                            "input_per_million": 1.2,
                            "cached_input_per_million": 0.12,
                            "output_per_million": 7.2,
                        }
                    ],
                }
            },
        )
    )

    model._log_llm_metric(
        {"model": "qwen3.6-flash", "messages": [], "stream": False},
        usage={"prompt_tokens": 300000, "completion_tokens": 1000},
        duration_ms=100,
        error_count=0,
    )

    assert request_recorded["input_cost_cny"] == 1.44
    assert request_recorded["output_cost_cny"] == 0.0288
    assert logged["llm_cost_cny"] == 1.4688
