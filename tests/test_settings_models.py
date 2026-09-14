import pytest
from pydantic import ValidationError

from backend.config.settings import Settings, get_settings


def _requested_model_settings() -> Settings:
    return Settings(
        model_provider_mode="local",
        asr_provider_mode="local",
        default_chat_model="doubao-seed-evolving",
        profile_model="doubao-seed-evolving",
        intent_model="doubao-seed-evolving",
        intent_performance_model="intent-performance-model",
        rehearsal_dimension_model="",
        employee_model="doubao-seed-evolving",
        conversation_summary_model="doubao-seed-evolving",
        motivation_scoring_model="doubao-seed-2.1-turbo",
        emotion_transition_model="doubao-seed-2.1-turbo",
        guidance_model="doubao-seed-evolving",
        coach_evaluator_model="doubao-seed-evolving",
        coach_redline_model="doubao-seed-evolving",
        embedding_local_dimensions=1024,
        embedding_dimensions=1024,
        llm_intent_performance_temperature=0.4,
        llm_enable_thinking=False,
        llm_profile_enable_thinking=False,
        llm_intent_enable_thinking=False,
        llm_intent_performance_enable_thinking=False,
        llm_employee_reply_enable_thinking=False,
        llm_conversation_summary_enable_thinking=True,
        llm_motivation_scoring_enable_thinking=False,
        llm_emotion_transition_enable_thinking=False,
        llm_guidance_enable_thinking=True,
        llm_coach_evaluator_enable_thinking=True,
        llm_coach_redline_enable_thinking=True,
        llm_document_vision_enable_thinking=True,
    )


def test_model_defaults_match_requested_configuration():
    settings = _requested_model_settings()
    assert settings.default_chat_model == "doubao-seed-evolving"
    assert settings.profile_model == "doubao-seed-evolving"
    assert settings.intent_model == "doubao-seed-evolving"
    assert settings.intent_performance_model == "intent-performance-model"
    assert settings.employee_model == "doubao-seed-evolving"
    assert settings.conversation_summary_model == "doubao-seed-evolving"
    assert settings.motivation_scoring_model == "doubao-seed-2.1-turbo"
    assert settings.emotion_transition_model == "doubao-seed-2.1-turbo"
    assert settings.guidance_model == "doubao-seed-evolving"
    assert settings.coach_evaluator_model == "doubao-seed-evolving"
    assert settings.coach_redline_model == "doubao-seed-evolving"
    assert settings.model_provider_mode == "local"
    assert settings.effective_embedding_model == "Qwen/Qwen3-Embedding-4B"
    assert settings.embedding_dimensions == 1024
    assert settings.effective_embedding_dimensions == 1024
    assert settings.effective_rerank_model == "Qwen/Qwen3-Reranker-4B"
    assert settings.effective_embedding_provider == "local_qwen"
    assert settings.effective_rerank_provider == "local_qwen"
    assert Settings.model_fields["local_model_auto_recycle_enabled"].default is False
    assert settings.local_model_idle_ttl_seconds == 3600
    assert settings.model_warmup_enabled is True
    assert settings.model_warmup_block_startup is False
    assert settings.llm_response_format_style == "openai"
    assert settings.langchain_structured_output_strategy == "provider"
    assert settings.vectorstore_provider == "postgres_pgvector"
    assert settings.vector_collection_prefix == "hr_agent_kb"
    assert settings.employee_reply_test_prompt_enabled is False
    assert settings.asr_local_language_auto_detect is True


def test_local_asr_streaming_endpoints_are_capped_at_two():
    settings = _requested_model_settings()
    assert settings.asr_local_preview_max_concurrency == 10
    assert settings.asr_local_streaming_endpoint_urls == (
        "http://qwen3_asr:7118",
    )

    payload = settings.model_dump()
    payload["asr_local_streaming_urls"] = (
        "http://asr-1:7118,http://asr-2:7118,http://asr-3:7118"
    )
    with pytest.raises(ValidationError, match="at most two ASR model instances"):
        Settings.model_validate(payload)

    payload = settings.model_dump()
    payload["asr_local_preview_max_concurrency"] = 12
    with pytest.raises(ValidationError):
        Settings.model_validate(payload)


def test_zero_disables_global_reranker_admission_limit():
    settings = _requested_model_settings()
    payload = settings.model_dump()
    payload["rag_global_rerank_max_concurrency"] = 0
    payload["rag_rerank_max_concurrency"] = 98
    assert Settings.model_validate(payload).rag_global_rerank_max_concurrency == 0


def test_task_model_routing_uses_requested_models():
    settings = _requested_model_settings()
    assert settings.model_for_task("profile") == "doubao-seed-evolving"
    assert settings.model_for_task("intent") == "doubao-seed-evolving"
    assert settings.model_for_task("intent_performance") == "intent-performance-model"
    fallback = settings.model_copy(update={"intent_performance_model": ""})
    assert fallback.model_for_task("intent_performance") == settings.intent_model
    assert settings.temperature_for_task("intent_performance") == 0.4
    assert settings.max_tokens_for_task("intent_performance") == 8192
    assert settings.timeout_for_task("intent_performance") == 120
    assert settings.model_for_task("employee_reply") == "doubao-seed-evolving"
    assert settings.model_for_task("conversation_summary") == "doubao-seed-evolving"
    assert settings.temperature_for_task("conversation_summary") == 0
    assert settings.max_tokens_for_task("conversation_summary") == 2048
    assert settings.timeout_for_task("conversation_summary") == 46
    assert settings.conversation_raw_turn_window == 28
    assert settings.conversation_summary_batch_turns == 6
    assert settings.model_for_task("motivation_scoring") == "doubao-seed-2.1-turbo"
    assert settings.model_for_task("emotion_transition") == "doubao-seed-2.1-turbo"
    assert settings.model_for_task("employee_state_transition") == (
        "doubao-seed-2.1-turbo"
    )
    assert settings.model_for_task("psychological_pattern_dynamics") == (
        "doubao-seed-2.1-turbo"
    )
    assert settings.model_for_task("rehearsal_dimensions") == "doubao-seed-2.1-turbo"
    assert settings.temperature_for_task("rehearsal_dimensions") == 0
    assert settings.max_tokens_for_task("rehearsal_dimensions") == 256
    assert settings.timeout_for_task("rehearsal_dimensions") == 20
    assert settings.model_for_task("guidance") == "doubao-seed-evolving"
    assert settings.model_for_task("guidance_evidence") == settings.model_for_task(
        "guidance"
    )
    assert settings.model_for_task("coach_evaluator") == "doubao-seed-evolving"
    assert settings.model_for_task("coach_evidence") == settings.model_for_task(
        "coach_evaluator"
    )
    for evidence_task, base_task in (
        ("guidance_evidence", "guidance"),
        ("coach_evidence", "coach_evaluator"),
    ):
        assert settings.temperature_for_task(
            evidence_task
        ) == settings.temperature_for_task(base_task)
        assert settings.max_tokens_for_task(
            evidence_task
        ) == settings.max_tokens_for_task(base_task)
        assert settings.timeout_for_task(
            evidence_task
        ) == settings.timeout_for_task(base_task)
    assert settings.model_for_task("coach_redline") == "doubao-seed-evolving"


def test_task_thinking_routing_uses_requested_configuration():
    settings = _requested_model_settings()
    disabled_tasks = (
        "profile",
        "intent",
        "intent_performance",
        "employee_reply",
        "motivation_scoring",
        "emotion_transition",
        "employee_state_transition",
        "psychological_pattern_dynamics",
        "rehearsal_dimensions",
    )
    enabled_tasks = (
        "conversation_summary",
        "guidance",
        "guidance_evidence",
        "coach_evaluator",
        "coach_evidence",
        "coach_redline",
        "document_vision",
    )

    assert settings.llm_enable_thinking is False
    assert settings.llm_thinking_budget == 8192
    assert all(settings.enable_thinking_for_task(task_name) is False for task_name in disabled_tasks)
    assert all(settings.enable_thinking_for_task(task_name) is True for task_name in enabled_tasks)
    assert settings.enable_thinking_for_task("unknown_task") is False


def test_settings_reads_env_runtime_values():
    settings = get_settings()
    assert settings.mineru_fail_on_error is True
    assert settings.mineru_lang == "ch"
    assert settings.enable_trace is False
    assert settings.redis_url == "redis://redis:6379/0"
    assert settings.embedding_cache_enabled is True
    assert settings.embedding_cache_ttl_seconds == 604800
    assert settings.embedding_cache_key_prefix == "hr_agent:query_embedding"
    assert settings.llm_web_search is False
    assert settings.temperature_for_task("employee_reply") == 0.9
    assert settings.max_tokens_for_task("employee_reply") == 8192
    assert settings.timeout_for_task("employee_reply") == 120
    assert settings.intent_performance_model
    assert (
        settings.model_for_task("intent_performance")
        == settings.intent_performance_model
    )
    assert (
        settings.temperature_for_task("intent_performance")
        == settings.llm_intent_performance_temperature
    )
    assert settings.max_tokens_for_task("intent_performance") == 8192
    assert settings.timeout_for_task("intent_performance") == 120
    assert settings.enable_thinking_for_task("intent_performance") is False
    assert settings.temperature_for_task("motivation_scoring") == 0.4
    assert settings.temperature_for_task("emotion_transition") == 0.0
    assert settings.max_tokens_for_task("motivation_scoring") == 512
    assert settings.max_tokens_for_task("emotion_transition") == 512
    assert settings.max_tokens_for_task("employee_state_transition") >= 4096
    assert settings.max_tokens_for_task("psychological_pattern_dynamics") == 1024
    tuned_patterns = settings.model_copy(
        update={"llm_psychological_pattern_dynamics_max_tokens": 768}
    )
    assert tuned_patterns.max_tokens_for_task("psychological_pattern_dynamics") == 768
    assert tuned_patterns.max_tokens_for_task("employee_state_transition") >= 4096
    assert settings.temperature_for_task("psychological_pattern_dynamics") == (
        settings.temperature_for_task("emotion_transition")
    )
    assert settings.timeout_for_task("psychological_pattern_dynamics") == (
        settings.timeout_for_task("emotion_transition")
    )
    assert (
        settings.max_tokens_for_task("employee_state_transition")
        >= 8 * settings.max_tokens_for_task("emotion_transition")
    )
    assert settings.timeout_for_task("motivation_scoring") == 20
    assert settings.timeout_for_task("emotion_transition") == 20
    assert settings.employee_reply_test_prompt_enabled is False


def test_model_provider_mode_selects_both_local_services_together():
    local = Settings(
        model_provider_mode="local",
        asr_provider_mode="local",
        embedding_provider="bosch",
        rerank_provider="openai_compatible",
    )
    platform = Settings(
        model_provider_mode="platform",
        asr_provider_mode="local",
        embedding_provider="openai_compatible",
        rerank_provider="bosch",
        embedding_api_endpoint="https://models.example/v1/embeddings",
        embedding_model="embedding-model",
        rerank_api_endpoint="https://models.example/v1/rerank",
        rerank_model="rerank-model",
    )

    assert local.embedding_uses_local_runtime is True
    assert local.rerank_uses_local_runtime is True
    assert local.effective_embedding_provider == "local_qwen"
    assert local.effective_rerank_provider == "local_qwen"
    assert local.embedding_url == "http://qwen_embedding:8000/v1/embeddings"
    assert local.rerank_url == "http://qwen_reranker:8000/v1/rerank"
    assert local.configured_local_model_services == frozenset(
        {"qwen_embedding", "qwen_reranker", "qwen3_asr"}
    )
    assert platform.embedding_uses_local_runtime is False
    assert platform.rerank_uses_local_runtime is False
    assert platform.effective_embedding_provider == "openai_compatible"
    assert platform.effective_rerank_provider == "bosch"
    assert platform.effective_embedding_model == "embedding-model"
    assert platform.effective_rerank_model == "rerank-model"
    assert platform.embedding_url == "https://models.example/v1/embeddings"
    assert platform.rerank_url == "https://models.example/v1/rerank"
    assert platform.configured_local_model_services == frozenset({"qwen3_asr"})

    bosch_asr = Settings(
        model_provider_mode="platform",
        asr_provider_mode="bosch",
    )
    browser_asr = Settings(
        model_provider_mode="platform",
        asr_provider_mode="browser",
    )
    assert "qwen3_asr" not in bosch_asr.configured_local_model_services
    assert "qwen3_asr" not in browser_asr.configured_local_model_services


def test_embedding_dimensions_follow_the_selected_runtime_model():
    local = Settings(
        model_provider_mode="local",
        embedding_local_model="Qwen/Qwen3-Embedding-4B",
        embedding_local_dimensions=2560,
        embedding_model="text-embedding-v4",
        EMBEDDING_PLATFORM_DIMENSIONS=2048,
    )
    platform = local.model_copy(
        update={"model_provider_mode": "platform"}
    )

    assert local.effective_embedding_dimensions == 2560
    assert platform.effective_embedding_dimensions == 2048
    assert local.embedding_profile_id == (
        "315c56db9c3c77baa6fcd0f722ee956892a08651ca334366e71b81259020a22a"
    )
    assert platform.embedding_profile_id == (
        "42e48ce7944fa3f54f7accd2409cd5563d37e0031a4f4dbba659a64abac63ccd"
    )


def test_local_qwen_is_not_an_independent_provider_switch():
    with pytest.raises(ValidationError):
        Settings(embedding_provider="local_qwen")
    with pytest.raises(ValidationError):
        Settings(rerank_provider="local_qwen")


@pytest.mark.parametrize("dimensions", [0, 16001])
def test_embedding_dimensions_must_fit_pgvector(dimensions):
    with pytest.raises(ValidationError):
        Settings(embedding_dimensions=dimensions)


@pytest.mark.parametrize("dimensions", [0, 16001])
def test_local_embedding_dimensions_must_fit_pgvector(dimensions):
    with pytest.raises(ValidationError):
        Settings(embedding_local_dimensions=dimensions)


def test_zero_redis_pool_limit_uses_practical_unbounded_sentinel():
    assert Settings(redis_max_connections=0).effective_redis_max_connections == 2**31
    assert Settings(redis_max_connections=321).effective_redis_max_connections == 321


@pytest.mark.parametrize(
    "field_name",
    (
        "redis_max_connections",
        "auth_max_active_sessions",
        "rag_db_search_max_concurrency",
        "rag_global_db_search_max_concurrency",
    ),
)
def test_unbounded_quota_fields_reject_negative_values(field_name):
    with pytest.raises(ValidationError):
        Settings(**{field_name: -1})


def test_employee_reply_test_prompt_switch_reads_boolean_env(monkeypatch):
    monkeypatch.setenv("EMPLOYEE_REPLY_TEST_PROMPT_ENABLED", "true")

    settings = Settings()

    assert settings.employee_reply_test_prompt_enabled is True


def test_pattern_switch_defaults_to_enabled():
    assert (
        Settings.model_fields["psychological_pattern_dynamics_enabled"].default
        is True
    )


def test_pattern_switch_reads_boolean_env(monkeypatch):
    monkeypatch.setenv("PSYCHOLOGICAL_PATTERN_DYNAMICS_ENABLED", "false")

    settings = Settings()

    assert settings.psychological_pattern_dynamics_enabled is False


def test_pattern_switch_is_documented_in_example_env():
    text = __import__("pathlib").Path("backend/config/.env.example").read_text(
        encoding="utf-8"
    )

    assert text.count("PSYCHOLOGICAL_PATTERN_DYNAMICS_ENABLED=true") == 1


def test_backend_compose_does_not_override_env_file_application_keys():
    text = __import__("pathlib").Path(
        "deployment/compose/compose.services.yml"
    ).read_text(encoding="utf-8")
    backend_block = text.split("  backend:", 1)[1].split("  frontend:", 1)[0]
    for key in (
        "DATABASE_URL",
        "POSTGRES_CREATE_HNSW_INDEX",
        "REDIS_URL",
        "EMBEDDING_CACHE_ENABLED",
        "PSYCHOLOGICAL_PATTERN_DYNAMICS_ENABLED",
        "MINERU_API_URL",
        "MINERU_BACKEND",
        "MINERU_EFFORT",
        "MINERU_PARSE_METHOD",
        "MINERU_TIMEOUT_SECONDS",
    ):
        assert f"      {key}:" not in backend_block
    assert "HTTP_PROXY:" in backend_block
    assert "NO_PROXY:" in backend_block
    assert "LOCAL_MODEL_CONTROLLER_URL:" in backend_block
    assert "/var/run/docker.sock:/var/run/docker.sock" not in backend_block


def test_compose_restricts_docker_control_to_dedicated_controllers():
    text = __import__("pathlib").Path(
        "deployment/compose/compose.services.yml"
    ).read_text(encoding="utf-8")
    controller_block = text.split("  local_model_controller:", 1)[1].split("  backend:", 1)[0]
    agent_block = text.split("  signoz_collection_agent:", 1)[1].split("  local_model_controller:", 1)[0]
    backend_block = text.split("  backend:", 1)[1].split("  frontend:", 1)[0]
    autoscaler_block = text.split("  backend_autoscaler:", 1)[1].split(
        "\nnetworks:", 1
    )[0]

    assert "/var/run/docker.sock:/var/run/docker.sock" in controller_block
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in agent_block
    assert "/var/run/docker.sock:/var/run/docker.sock" not in backend_block
    assert "/var/run/docker.sock:/var/run/docker.sock" in autoscaler_block
    assert "backend.services.backend_autoscaler:app" in autoscaler_block
    assert "BACKEND_AUTOSCALER_COMPOSE_WORKING_DIR: /home/uay4sgh/hr_agent/06_emotion" in autoscaler_block
    assert "ports:" not in autoscaler_block
    assert "backend.services.local_model_runtime_controller:app" in controller_block
    assert "opentelemetry-instrument uvicorn" in controller_block
    assert 'HR_AGENT_GPU_METRICS_ENABLED: "false"' in controller_block
    assert "NVIDIA_DRIVER_CAPABILITIES:" not in controller_block
    assert "NVIDIA_VISIBLE_DEVICES:" not in controller_block
    assert "    gpus:" not in controller_block
    assert "LOCAL_MODEL_AUTO_RECYCLE_ENABLED: \"${LOCAL_MODEL_AUTO_RECYCLE_ENABLED:-true}\"" in text
    assert "LOCAL_MODEL_PINNED_SERVICES: \"${LOCAL_MODEL_PINNED_SERVICES:-}\"" in text
    assert "LOCAL_MODEL_IDLE_TTL_SECONDS: \"${LOCAL_MODEL_IDLE_TTL_SECONDS:-3600}\"" in text


def test_frontend_waits_for_backend_health_and_compose_volume_keys_are_unique():
    text = __import__("pathlib").Path(
        "deployment/compose/compose.services.yml"
    ).read_text(encoding="utf-8")
    backend_block = text.split("  backend:", 1)[1].split("  frontend:", 1)[0]
    frontend_block = text.split("  frontend:", 1)[1].split("\nnetworks:", 1)[0]

    assert "http://127.0.0.1:7111/api/v1/health" in backend_block
    assert "condition: service_healthy" in frontend_block
    assert text.count("name: 06_qwen_hf_cache") == 1


def test_compose_profiles_follow_single_model_provider_mode():
    pathlib = __import__("pathlib")
    text = pathlib.Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )
    embedding_block = text.split("  qwen_embedding:", 1)[1].split(
        "  qwen_reranker:", 1
    )[0]
    reranker_block = text.split("  qwen_reranker:", 1)[1].split(
        "  qwen_reranker_shadow:", 1
    )[0]
    asr_block = text.split("  qwen3_asr:", 1)[1].split("  fish_tts:", 1)[0]
    backend_block = text.split("  backend:", 1)[1].split("  frontend:", 1)[0]

    assert 'profiles: ["local"]' in embedding_block
    assert 'profiles: ["local"]' in reranker_block
    assert 'profiles: ["asr-local"]' in asr_block
    expected_gpu_placements = {
        "MINERU_GPU_DEVICE": "0",
        "QWEN_EMBEDDING_GPU_DEVICE": "0",
        "QWEN_RERANKER_GPU_DEVICE": "1",
        "QWEN3_ASR_PRIMARY_GPU_DEVICE": "0",
        "QWEN3_ASR_SECONDARY_GPU_DEVICE": "0",
        "FISH_TTS_GPU_DEVICE": "1",
    }
    for variable, default in expected_gpu_placements.items():
        interpolation = f"${{{variable}:-{default}}}"
        assert f'device_ids: ["{interpolation}"]' in text
        assert f'NVIDIA_VISIBLE_DEVICES: "{interpolation}"' in text
    assert 'CUDA_VISIBLE_DEVICES: "0"' in reranker_block
    assert reranker_block.count("--enable-prefix-caching") == 1
    assert "--enable-chunked-prefill" not in reranker_block
    assert "${QWEN_RERANK_GPU_MEMORY_UTILIZATION:-0.35}" in reranker_block
    assert "${QWEN_RERANK_MAX_NUM_BATCHED_TOKENS:-24576}" in reranker_block
    assert "${QWEN_RERANK_MAX_NUM_SEQS:-96}" in reranker_block
    assert "required: false" in asr_block
    assert backend_block.count("required: false") >= 3
    assert 'ASR_PROVIDER_MODE: "${ASR_PROVIDER_MODE:-local}"' in backend_block
    assert "x-model-provider-config:" not in text
    assert not pathlib.Path(".env").exists()

    entry = pathlib.Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "path: deployment/compose/compose.mode.yml" in entry
    assert "env_file: backend/config/.env" in entry

    mode_router = pathlib.Path("deployment/compose/compose.mode.yml").read_text(
        encoding="utf-8"
    )
    assert "deployment/compose/compose.services.yml" in mode_router
    assert (
        "deployment/compose/compose.model-provider.${MODEL_PROVIDER_MODE:"
        in mode_router
    )
    assert "deployment/compose/compose.asr-provider.${ASR_PROVIDER_MODE:-local}.yml" in mode_router

    local_override = pathlib.Path(
        "deployment/compose/compose.model-provider.local.yml"
    ).read_text(encoding="utf-8")
    assert local_override.count("profiles: !reset []") == 2
    asr_local_override = pathlib.Path(
        "deployment/compose/compose.asr-provider.local.yml"
    ).read_text(encoding="utf-8")
    assert asr_local_override.count("profiles: !reset []") == 1
    assert "qwen3_asr_secondary:" not in asr_local_override
    for mode in ("bosch", "browser"):
        override = pathlib.Path(
            f"deployment/compose/compose.asr-provider.{mode}.yml"
        ).read_text(encoding="utf-8")
        assert override.strip() == "services: {}"

    wrapper = pathlib.Path("scripts/compose.sh").read_text(encoding="utf-8")
    assert '--env-file "${env_file}"' in wrapper
    assert 'export COMPOSE_PROFILES="${mode}"' not in wrapper
    assert "--stop --force qwen_embedding qwen_reranker" in wrapper
    assert "ASR_PROVIDER_MODE must be local, bosch, or browser" in wrapper
    assert "qwen3_asr qwen3_asr_secondary" in wrapper

    example = pathlib.Path("backend/config/.env.example").read_text(
        encoding="utf-8"
    )
    assert "MODEL_PROVIDER_MODE=local" in example
    assert "HR_AGENT_BACKEND_IMAGE=06-emotion-main-backend:local" in example
    assert "BACKEND_REPLICAS=2" in example
    assert "BACKEND_AUTOSCALER_MAX_REPLICAS=8" in example
    assert "ASR_PROVIDER_MODE=local" in example
    assert "RERANK_LOCAL_API_ENDPOINT=http://qwen_reranker:8000/v1/rerank" in example
    for setting in (
        "MINERU_GPU_DEVICE=0",
        "QWEN_EMBEDDING_GPU_DEVICE=0",
        "QWEN_RERANKER_GPU_DEVICE=1",
        "QWEN3_ASR_PRIMARY_GPU_DEVICE=0",
        "QWEN3_ASR_SECONDARY_GPU_DEVICE=0",
        "FISH_TTS_GPU_DEVICE=1",
    ):
        assert example.count(setting) == 1


def test_settings_only_loads_backend_config_env_file():
    assert Settings.model_config["env_file"] == ("backend/config/.env",)


def test_local_rerank_protocol_defaults_to_native_and_rejects_unknown_values():
    assert Settings.model_fields["rerank_local_protocol"].default == "rerank"
    assert Settings.model_fields["rerank_local_api_endpoint"].default == (
        "http://qwen_reranker:8000/v1/rerank"
    )
    assert Settings().rerank_local_protocol == "rerank"
    with pytest.raises(ValidationError):
        Settings(rerank_local_protocol="unknown")


def test_qwen_reranker_uses_native_scoring_and_shadow_is_isolated():
    pathlib = __import__("pathlib")
    text = pathlib.Path("deployment/compose/compose.services.yml").read_text(
        encoding="utf-8"
    )
    production = text.split("  qwen_reranker:", 1)[1].split(
        "  qwen_reranker_shadow:", 1
    )[0]
    shadow = text.split("  qwen_reranker_shadow:", 1)[1].split(
        "  qwen3_asr:", 1
    )[0]

    assert 'profiles: ["local"]' in production
    assert "image: vllm/vllm-openai:v0.27.1" in production
    assert "build:" not in production
    assert "--task" not in production
    assert "--runner" in production
    assert "pooling" in production
    assert "--hf_overrides" in production
    assert '"architectures":["Qwen3ForSequenceClassification"]' in production
    assert '"classifier_from_token":["no","yes"]' in production
    assert '"is_original_qwen3_reranker":true' in production
    assert "--chat-template" in production
    assert "/etc/vllm/qwen3_reranker.jinja" in production
    assert "./deployment/vllm/qwen3_reranker.jinja" in production
    assert "qwen_vllm_reranker_cache:/root/.cache/vllm" in production
    assert "--disable-log-requests" not in production

    assert 'profiles: ["rerank-shadow"]' in shadow
    assert "image: vllm/vllm-openai:v0.27.1" in shadow
    assert 'restart: "no"' in shadow
    assert "--runner" in shadow
    assert "pooling" in shadow
    assert "--hf_overrides" in shadow
    assert '"architectures":["Qwen3ForSequenceClassification"]' in shadow
    assert '"classifier_from_token":["no","yes"]' in shadow
    assert '"is_original_qwen3_reranker":true' in shadow
    assert "--chat-template" in shadow
    assert "/etc/vllm/qwen3_reranker.jinja" in shadow
    assert shadow.count("--enable-prefix-caching") == 1
    assert "--disable-log-requests" not in shadow
    assert "127.0.0.1:${QWEN_RERANKER_SHADOW_PORT:-7123}:8000" in shadow
    assert 'device_ids: ["${QWEN_RERANKER_SHADOW_GPU_DEVICE:-1}"]' in shadow
    assert "./deployment/vllm/qwen3_reranker.jinja" in shadow

    example = pathlib.Path("backend/config/.env.example").read_text(
        encoding="utf-8"
    )
    assert "RERANK_LOCAL_PROTOCOL=rerank" in example
    assert "QWEN_RERANK_MAX_NUM_BATCHED_TOKENS=24576" in example
    assert "QWEN_RERANK_MAX_NUM_SEQS=96" in example
    assert "RAG_AGENTIC_SEARCH_MODE=off" in example
    assert "RAG_AGENTIC_SEARCH_SHADOW_SAMPLE_RATE=0" in example
    assert Settings.model_fields["rag_agentic_search_mode"].default == "off"
    assert "RERANK_SHADOW_PROTOCOL=rerank" in example
    assert "RERANK_SHADOW_API_ENDPOINT=http://127.0.0.1:7123/v1/rerank" in example
    assert "QWEN_RERANKER_SHADOW_GPU_DEVICE=1" in example
    assert "QWEN_RERANKER_SHADOW_PORT=7123" in example

    template = pathlib.Path(
        "deployment/vllm/qwen3_reranker.jinja"
    ).read_text(encoding="utf-8")
    assert "<Query>:" in template
    assert "<Document>:" in template
    assert "<think>" in template


def test_model_clients_share_one_api_key_setting():
    assert "api_key" in Settings.model_fields
    assert "chat_api_key" not in Settings.model_fields
    assert "embedding_api_key" not in Settings.model_fields
    assert "rerank_api_key" not in Settings.model_fields

    settings = Settings(api_key="shared-key", asr_api_key="")
    assert settings.api_key == "shared-key"
    assert settings.effective_asr_api_key == "shared-key"

    settings = Settings(api_key="shared-key", asr_api_key="asr-key")
    assert settings.effective_asr_api_key == "asr-key"


def test_model_env_examples_use_only_shared_api_key():
    pathlib = __import__("pathlib")
    text = pathlib.Path("backend/config/.env.example").read_text(encoding="utf-8")
    assert "API_KEY=" in text
    assert "CHAT_API_KEY=" not in text
    assert "EMBEDDING_API_KEY=" not in text
    assert "RERANK_API_KEY=" not in text
