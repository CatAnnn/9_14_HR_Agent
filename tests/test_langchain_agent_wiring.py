import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from langchain_core.messages import AIMessage, HumanMessage

from backend.config.settings import Settings
from backend.exceptions.llm_errors import StructuredOutputError
from backend.schemas.simulation import EmotionTransitionStructuredOutput
from backend.services import langchain_llm_service as langchain_module
from backend.services.langchain_llm_service import LangChainLLMService


def test_all_agent_modules_use_langchain_service_not_legacy_llm_service():
    agent_dir = Path("backend/agents")
    files = [p for p in agent_dir.rglob("*.py") if p.name != "__init__.py"]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "from backend.services.llm_service import LLMService" not in text, path
        assert "LLMService().chat_" not in text, path
    llm_calling_files = [p for p in files if "LangChainLLMService" in p.read_text(encoding="utf-8")]
    assert llm_calling_files, "Agent LLM calls must be routed through LangChainLLMService."


def test_structured_agents_use_langchain_create_agent_response_format():
    service_text = Path("backend/services/langchain_llm_service.py").read_text(encoding="utf-8")
    assert "from langchain.agents import create_agent" in service_text
    assert "ProviderStrategy" in service_text
    assert "ToolStrategy" in service_text
    assert "response_format=response_format" in service_text
    assert "structured_response" in service_text


def test_agent_modules_do_not_call_manual_json_structured_invoke():
    agent_dir = Path("backend/agents")
    for path in agent_dir.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "ainvoke_json(" not in text, path
        if "LangChainLLMService" in text:
            assert (
                "ainvoke_structured(" in text
                or "ainvoke_structured_single(" in text
                or "ainvoke_text(" in text
                or "astream_text(" in text
            ), path


class ExampleStructuredResponse(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_provider_structured_parse_error_falls_back_to_tool_strategy(monkeypatch):
    service = LangChainLLMService()
    calls = []

    async def fake_structured_once(**kwargs):
        calls.append(kwargs["strategy_name"])
        if kwargs["strategy_name"] == "provider":
            raise ValueError("Native structured output expected valid JSON: Invalid control character")
        return ExampleStructuredResponse(value="ok")

    monkeypatch.setattr(service, "_ainvoke_structured_once", fake_structured_once)

    result = await service.ainvoke_structured(
        prompt="hi",
        schema=ExampleStructuredResponse,
        strategy="provider",
    )

    assert result.value == "ok"
    assert calls == ["provider", "tool"]

@pytest.mark.asyncio
async def test_bosch_configured_provider_strategy_uses_tool_strategy(monkeypatch):
    service = LangChainLLMService()
    service.settings = Settings(
        llm_provider="bosch_openai_compatible",
        langchain_structured_output_strategy="provider",
    )
    calls = []

    async def fake_structured_once(**kwargs):
        calls.append(kwargs["strategy_name"])
        return ExampleStructuredResponse(value="ok")

    monkeypatch.setattr(service, "_ainvoke_structured_once", fake_structured_once)

    result = await service.ainvoke_structured(
        prompt="hi",
        schema=ExampleStructuredResponse,
    )

    assert result.value == "ok"
    assert calls == ["tool"]

class FakeSingleCallModel:
    def __init__(self, message: AIMessage, *, delay_seconds: float = 0.0):
        self.message = message
        self.delay_seconds = delay_seconds
        self.invoke_count = 0
        self.bound_tools = []
        self.tool_choice = None

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.bound_tools = list(tools)
        self.tool_choice = tool_choice
        return self

    async def ainvoke(self, messages):
        self.invoke_count += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.message

    async def ainvoke_stream_message(self, messages, *, record_stream_timing=False):
        return await self.ainvoke(messages)


@pytest.mark.asyncio
async def test_json_schema_transport_uses_strict_response_format_without_tools(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content='{"value":"ok"}'))
    captured = {}

    def fake_chat_model(**kwargs):
        captured.update(kwargs)
        return model

    monkeypatch.setattr(service, "chat_model", fake_chat_model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="guidance",
        timeout_seconds=1,
        stream=True,
        record_stream_timing=True,
        structured_transport="json_schema",
    )

    assert result.value == "ok"
    assert model.invoke_count == 1
    assert model.bound_tools == []
    assert model.tool_choice is None
    assert captured["max_retries"] == 0
    assert captured["response_format"]["type"] == "json_schema"
    response_schema = captured["response_format"]["json_schema"]
    assert response_schema["name"] == "example_structured_response"
    assert response_schema["schema"] == ExampleStructuredResponse.model_json_schema()
    assert response_schema["strict"] is True


def test_guidance_json_schema_payload_omits_tool_fields():
    service = LangChainLLMService(
        Settings(
            llm_provider="bosch_openai_compatible",
            llm_response_format_style="openai",
        )
    )
    response_format = service._json_schema_response_format(
        ExampleStructuredResponse,
        schema_name="example_structured_response",
        strict=True,
    )
    model = service.chat_model(
        task_name="guidance",
        response_format=response_format,
        max_retries=0,
    )

    payload = model._build_payload(
        [HumanMessage(content="return one object")],
        stream=True,
    )

    assert payload["response_format"] == response_format
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in payload
    assert "tool_choice" not in payload


@pytest.mark.asyncio
async def test_json_schema_transport_accepts_one_full_json_code_fence(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content='```json\n{"value":"ok"}\n```'))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="guidance",
        timeout_seconds=1,
        stream=True,
        structured_transport="json_schema",
    )

    assert result.value == "ok"
    assert model.invoke_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        '```json{"value":"ok"}```',
        '```json\n{"value":"ok"}',
        'json\n{"value":"ok"}',
        '<json>{"value":"ok"}</json>',
        '<think>internal reasoning</think>\n{"value":"ok"}',
    ],
)
async def test_json_schema_transport_accepts_one_object_with_presentation_wrapper(
    monkeypatch,
    content,
):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content=content))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="guidance",
        timeout_seconds=1,
        stream=True,
        structured_transport="json_schema",
    )

    assert result.value == "ok"
    assert model.invoke_count == 1


@pytest.mark.asyncio
async def test_json_schema_transport_recovers_one_schema_valid_object_from_prose(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(
        AIMessage(content='result follows:\n```json\n{"value":"ok"}\n```')
    )
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="guidance",
        timeout_seconds=1,
        stream=True,
        structured_transport="json_schema",
    )

    assert result.value == "ok"
    assert model.invoke_count == 1


@pytest.mark.asyncio
async def test_json_schema_transport_rejects_multiple_json_objects(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(
        AIMessage(content='{"value":"first"}\n{"value":"second"}')
    )
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    with pytest.raises(StructuredOutputError) as exc_info:
        await service.ainvoke_structured_single(
            prompt="hi",
            schema=ExampleStructuredResponse,
            task_name="guidance",
            timeout_seconds=1,
            stream=True,
            structured_transport="json_schema",
        )

    assert exc_info.value.code == "malformed_json"
    assert model.invoke_count == 1


@pytest.mark.asyncio
async def test_single_structured_call_accepts_tool_call_and_disables_retries(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ExampleStructuredResponse",
                    "args": {"value": "ok"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
    )
    captured = {}

    def fake_chat_model(**kwargs):
        captured.update(kwargs)
        return model

    monkeypatch.setattr(service, "chat_model", fake_chat_model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="motivation_scoring",
        timeout_seconds=1,
    )

    assert result.value == "ok"
    assert model.invoke_count == 1
    assert model.bound_tools == [ExampleStructuredResponse]
    assert model.tool_choice == "required"
    assert captured["max_retries"] == 0


@pytest.mark.asyncio
async def test_single_structured_call_accepts_standalone_json_content(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content='{"value":"ok"}'))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="emotion_transition",
        timeout_seconds=1,
    )

    assert result.value == "ok"
    assert model.invoke_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "vad_delta": {"valence": 2, "arousal": 0, "dominance": 0},
            "transition_strategy": "expected_value",
        },
        {
            "vad_delta": {"valence": 0, "arousal": 0, "dominance": 0},
            "transition_strategy": "平均值",
        },
    ],
)
def test_single_structured_parser_rejects_invalid_vad_and_strategy(payload):
    message = AIMessage(content=json.dumps(payload, ensure_ascii=False))

    with pytest.raises(StructuredOutputError) as exc_info:
        LangChainLLMService._parse_single_structured_message(
            message=message,
            schema=EmotionTransitionStructuredOutput,
            expected_tool_name="EmotionTransitionStructuredOutput",
        )

    assert exc_info.value.code == "schema_validation"


def test_structured_validation_error_reports_safe_field_paths_and_types():
    message = AIMessage(
        content=json.dumps(
            {
                "vad_delta": {"valence": 2, "arousal": 0, "dominance": 0},
                "transition_strategy": "expected_value",
            }
        )
    )

    with pytest.raises(StructuredOutputError) as exc_info:
        LangChainLLMService._parse_single_structured_message(
            message=message,
            schema=EmotionTransitionStructuredOutput,
            expected_tool_name="EmotionTransitionStructuredOutput",
        )

    error_text = str(exc_info.value)
    assert "vad_delta.valence:less_than_equal" in error_text
    assert "input_value" not in error_text


@pytest.mark.asyncio
async def test_single_structured_call_rejects_malformed_json_without_retry(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content="not-json"))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    with pytest.raises(StructuredOutputError) as exc_info:
        await service.ainvoke_structured_single(
            prompt="hi",
            schema=ExampleStructuredResponse,
            task_name="motivation_scoring",
            timeout_seconds=1,
        )

    assert exc_info.value.code == "malformed_json"
    assert model.invoke_count == 1


def test_single_structured_parser_recovers_wrong_tool_name_and_duplicate_calls():
    wrong_tool = AIMessage(
        content="",
        tool_calls=[
            {"name": "wrong_tool", "args": {"value": "ok"}, "id": "call-1", "type": "tool_call"}
        ],
    )
    result, source, repair_steps = LangChainLLMService._parse_single_structured_message(
        message=wrong_tool,
        schema=ExampleStructuredResponse,
        expected_tool_name="ExampleStructuredResponse",
    )
    assert result.value == "ok"
    assert source == "tool_call"
    assert "normalized_tool_name" in repair_steps

    duplicate_tools = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ExampleStructuredResponse",
                "args": {"value": "ok"},
                "id": "call-1",
                "type": "tool_call",
            },
            {
                "name": "ExampleStructuredResponse",
                "args": {"value": "ok"},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )
    result, _, repair_steps = LangChainLLMService._parse_single_structured_message(
        message=duplicate_tools,
        schema=ExampleStructuredResponse,
        expected_tool_name="ExampleStructuredResponse",
    )
    assert result.value == "ok"
    assert "deduplicated_tool_calls" in repair_steps


def test_single_structured_parser_rejects_conflicting_tool_calls():
    conflicting_tools = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ExampleStructuredResponse",
                "args": {"value": "one"},
                "id": "call-1",
                "type": "tool_call",
            },
            {
                "name": "ExampleStructuredResponse",
                "args": {"value": "two"},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )
    with pytest.raises(StructuredOutputError) as exc_info:
        LangChainLLMService._parse_single_structured_message(
            message=conflicting_tools,
            schema=ExampleStructuredResponse,
            expected_tool_name="ExampleStructuredResponse",
        )
    assert exc_info.value.code == "conflicting_tool_calls"


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"value":"ok"}\n```',
        'Result follows: {"value":"ok"} end.',
        "Here's the result: {'value': 'ok'} done.",
        '<think>internal analysis</think>{"value":"ok"}',
        json.dumps(json.dumps({"value": "ok"})),
        "{'value': 'ok'}",
    ],
)
@pytest.mark.asyncio
async def test_single_structured_call_recovers_common_content_formats_once(monkeypatch, content):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content=content))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="motivation_scoring",
        timeout_seconds=1,
    )

    assert result.value == "ok"
    assert model.invoke_count == 1


@pytest.mark.parametrize("wrapper_key", ["arguments", "args", "input", "data"])
def test_single_structured_parser_unwraps_tool_argument_containers(wrapper_key):
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ExampleStructuredResponse",
                "args": {wrapper_key: {"value": "ok"}},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )

    result, source, repair_steps = LangChainLLMService._parse_single_structured_message(
        message=message,
        schema=ExampleStructuredResponse,
        expected_tool_name="ExampleStructuredResponse",
    )

    assert result.value == "ok"
    assert source == "tool_call"
    assert f"unwrapped_{wrapper_key}" in repair_steps


def test_single_structured_parser_recovers_raw_python_tool_arguments():
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ExampleStructuredResponse",
                "args": {"raw_arguments": "{'value': 'ok'}"},
                "id": "call-1",
                "type": "tool_call",
            }
        ],
    )

    result, _, repair_steps = LangChainLLMService._parse_single_structured_message(
        message=message,
        schema=ExampleStructuredResponse,
        expected_tool_name="ExampleStructuredResponse",
    )

    assert result.value == "ok"
    assert "parsed_raw_tool_arguments" in repair_steps
    assert "parsed_python_literal" in repair_steps


@pytest.mark.asyncio
async def test_single_structured_metrics_distinguish_recovery_and_fallback(monkeypatch):
    captured_metrics = []
    service = LangChainLLMService()
    recovered_model = FakeSingleCallModel(AIMessage(content="prefix {'value': 'ok'} suffix"))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: recovered_model)
    monkeypatch.setattr(
        langchain_module,
        "log_metric",
        lambda event, **fields: captured_metrics.append({"event": event, **fields}),
    )

    result = await service.ainvoke_structured_single(
        prompt="hi",
        schema=ExampleStructuredResponse,
        task_name="motivation_scoring",
        timeout_seconds=1,
    )
    assert result.value == "ok"
    success_metric = captured_metrics[-1]
    assert success_metric["structured_output_recovered"] is True
    assert "extracted_balanced_object" in success_metric["structured_output_repair_steps"]

    captured_metrics.clear()
    failed_model = FakeSingleCallModel(AIMessage(content="not-json"))
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: failed_model)
    with pytest.raises(StructuredOutputError):
        await service.ainvoke_structured_single(
            prompt="hi",
            schema=ExampleStructuredResponse,
            task_name="motivation_scoring",
            timeout_seconds=1,
        )
    failure_metric = captured_metrics[-1]
    assert failure_metric["structured_output_recovered"] is False
    assert failure_metric["structured_output_fallback_reason"] == "malformed_json"


@pytest.mark.asyncio
async def test_single_structured_call_times_out_without_retry(monkeypatch):
    service = LangChainLLMService()
    model = FakeSingleCallModel(AIMessage(content='{"value":"late"}'), delay_seconds=1)
    monkeypatch.setattr(service, "chat_model", lambda **kwargs: model)

    with pytest.raises(StructuredOutputError) as exc_info:
        await service.ainvoke_structured_single(
            prompt="hi",
            schema=ExampleStructuredResponse,
            task_name="emotion_transition",
            timeout_seconds=0.01,
        )

    assert exc_info.value.code == "timeout"
    assert model.invoke_count == 1
