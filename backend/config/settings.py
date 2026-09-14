from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMTokenRates(BaseModel):
    """Contract rates in CNY for one million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float = Field(ge=0)


class LLMTokenPricingTier(LLMTokenRates):
    """Rates applied when the request input does not exceed the threshold."""

    max_input_tokens: int = Field(gt=0)


class LLMTokenPricing(LLMTokenRates):
    """Default rates plus optional ascending long-context pricing tiers."""

    tiers: tuple[LLMTokenPricingTier, ...] = ()

    def rates_for_input_tokens(self, input_tokens: int) -> LLMTokenRates:
        token_count = max(0, int(input_tokens))
        for tier in sorted(self.tiers, key=lambda item: item.max_input_tokens):
            if token_count <= tier.max_input_tokens:
                return tier
        return self


class Settings(BaseSettings):
    """Runtime settings for the MinerU + PostgreSQL/pgvector + real model API build."""

    model_config = SettingsConfigDict(
        env_file=("backend/config/.env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "HR 绩效反馈和对话预演 Agent POC"
    app_version: str = "0.5.0-postgres-pgvector"
    environment: str = "local"
    api_prefix: str = "/api/v1"

    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])
    runtime_data_dir: Path | None = None
    redis_url: str
    redis_connect_timeout_seconds: float = 1.0
    redis_socket_timeout_seconds: float = 1.0
    # Zero removes the application-level Redis connection-pool cap. redis-py
    # treats 0/None as its own finite default, so client construction uses the
    # explicit compatibility sentinel exposed by effective_redis_max_connections.
    redis_max_connections: int = Field(default=0, ge=0)
    distributed_coordination_enabled: bool = False
    distributed_coordination_key_prefix: str = "hr_agent:coordination"
    distributed_lease_ttl_seconds: float = Field(default=30.0, ge=5.0)
    distributed_lease_poll_interval_ms: int = Field(default=50, ge=10, le=1_000)
    distributed_session_lock_timeout_seconds: float = Field(default=900.0, gt=0)
    # Zero disables the distributed RAG database-search admission limit.
    rag_global_db_search_max_concurrency: int = Field(default=0, ge=0)
    # Zero disables the distributed reranker admission limit. Each backend
    # process still uses rag_rerank_max_concurrency as its local safety bound.
    rag_global_rerank_max_concurrency: int = Field(default=0, ge=0)
    workflow_global_db_max_concurrency: int = Field(default=64, ge=1)
    embedding_cache_enabled: bool
    embedding_cache_ttl_seconds: int
    embedding_cache_key_prefix: str
    rag_result_cache_enabled: bool = True
    rag_result_cache_ttl_seconds: float = Field(default=900.0, gt=0)
    rag_result_cache_max_entries: int = Field(default=256, ge=1)
    rag_result_cache_key_prefix: str = "hr_agent:rag_result"

    llm_provider: Literal["openai_compatible", "bosch_openai_compatible", "bosch_messages"]
    chat_api_base_url: str = ""
    chat_api_endpoint: str
    chat_api_endpoints: str = ""
    api_key: str
    chat_model: str = ""

    default_chat_model: str
    profile_model: str
    intent_model: str
    intent_performance_model: str = ""
    personality_facet_model: str = ""
    rehearsal_dimension_model: str = ""
    employee_model: str
    conversation_summary_model: str = "qwen3.6-flash"
    motivation_scoring_model: str
    emotion_transition_model: str
    guidance_model: str
    coach_evaluator_model: str
    coach_redline_model: str
    model_retry_race_model: str = "deepseek-v4-pro"
    # Business-level fallback used only after the configured model retries are
    # exhausted.  Candidate lookup is always isolated by owner, employee and
    # intent; individual services apply their own schema/evidence checks.
    model_history_fallback_enabled: bool = False
    model_history_fallback_max_sessions: int = Field(default=50, ge=1, le=200)
    model_history_fallback_max_age_days: int = Field(default=730, ge=1, le=3650)
    model_history_fallback_min_session_similarity: float = Field(
        default=0.78,
        ge=0.0,
        le=1.0,
    )
    model_history_fallback_min_reply_similarity: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
    )
    model_history_fallback_min_conversation_similarity: float = Field(
        default=0.88,
        ge=0.0,
        le=1.0,
    )
    llm_pricing_version: str = "unconfigured"
    llm_model_pricing: dict[str, LLMTokenPricing] = Field(default_factory=dict)

    model_provider_mode: Literal["local", "platform"] = "local"

    embedding_provider: Literal["openai_compatible", "bosch"] = "bosch"
    embedding_local_api_endpoint: str = "http://qwen_embedding:8000/v1/embeddings"
    embedding_local_model: str = "Qwen/Qwen3-Embedding-4B"
    embedding_local_dimensions: int = Field(default=2560, gt=0, le=16000)
    embedding_api_base_url: str = ""
    embedding_api_endpoint: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = Field(
        default=2048,
        gt=0,
        le=16000,
        validation_alias=AliasChoices(
            "EMBEDDING_PLATFORM_DIMENSIONS",
            "embedding_dimensions",
        ),
    )
    embedding_api_batch_size: int = Field(default=46, gt=0, le=2048)

    rerank_provider: Literal["openai_compatible", "bosch"] = "bosch"
    rerank_local_api_endpoint: str = "http://qwen_reranker:8000/v1/rerank"
    rerank_local_protocol: Literal["completion", "rerank", "score"] = "rerank"
    rerank_local_model: str = "Qwen/Qwen3-Reranker-4B"
    rerank_api_base_url: str = ""
    rerank_api_endpoint: str = ""
    rerank_model: str = ""

    model_warmup_enabled: bool = True
    model_warmup_block_startup: bool = False
    model_warmup_timeout_seconds: float = 900.0
    model_warmup_max_concurrency: int = 4

    local_model_auto_recycle_enabled: bool = False
    local_model_auto_start_enabled: bool = True
    local_model_pinned_services: str = "qwen3_asr"
    local_model_controller_url: str = ""
    local_model_idle_ttl_seconds: int = 900
    local_model_recycle_check_interval_seconds: int = 30
    local_model_start_timeout_seconds: float = 900.0
    local_model_health_poll_interval_seconds: float = 2.0
    local_model_docker_socket_path: Path = Path("/var/run/docker.sock")
    local_model_docker_project_name: str = "06"
    local_model_docker_command: str = "docker"
    local_model_docker_compose_file: str = "docker-compose.yml"
    local_model_docker_project_dir: Path | None = None
    local_model_stop_timeout_seconds: float = 30.0

    model_api_auth_mode: Literal["api_key", "bearer", "client_credentials"]
    model_api_key_header_name: str
    oauth2_token_url: str = ""
    oauth2_client_id: str = ""
    oauth2_client_secret: str = ""
    oauth2_scope: str = ""

    llm_temperature: float
    llm_intent_performance_temperature: float = Field(
        default=0.4,
        ge=0.0,
        le=2.0,
    )
    llm_personality_facet_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
    )
    llm_rehearsal_dimension_temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=2.0,
    )
    llm_employee_temperature: float
    llm_conversation_summary_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_motivation_scoring_temperature: float = Field(ge=0.0, le=2.0)
    llm_emotion_transition_temperature: float = Field(ge=0.0, le=2.0)
    llm_guidance_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_coach_evaluator_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_coach_redline_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_top_p: float | None = None
    llm_max_tokens: int | None
    llm_profile_max_tokens: int | None
    llm_intent_max_tokens: int | None
    llm_intent_performance_max_tokens: int | None = 8192
    llm_personality_facet_max_tokens: int = Field(default=1200, gt=0)
    llm_rehearsal_dimension_max_tokens: int = Field(default=256, gt=0)
    llm_employee_max_tokens: int | None
    llm_conversation_summary_max_tokens: int = Field(default=512, gt=0)
    llm_motivation_scoring_max_tokens: int = Field(gt=0)
    llm_emotion_transition_max_tokens: int = Field(gt=0)
    llm_employee_state_transition_max_tokens: int = Field(default=4096, gt=0)
    llm_psychological_pattern_dynamics_max_tokens: int = Field(default=1024, gt=0)
    llm_guidance_max_tokens: int | None
    llm_coach_evaluator_max_tokens: int | None
    llm_coach_redline_max_tokens: int | None
    llm_enable_thinking: bool | None = None
    llm_profile_enable_thinking: bool | None = None
    llm_intent_enable_thinking: bool | None = None
    llm_intent_performance_enable_thinking: bool | None = None
    llm_personality_facet_enable_thinking: bool | None = False
    llm_rehearsal_dimension_enable_thinking: bool | None = False
    llm_employee_reply_enable_thinking: bool | None = None
    llm_conversation_summary_enable_thinking: bool | None = None
    llm_motivation_scoring_enable_thinking: bool | None = None
    llm_emotion_transition_enable_thinking: bool | None = None
    llm_guidance_enable_thinking: bool | None = None
    llm_coach_evaluator_enable_thinking: bool | None = None
    llm_coach_redline_enable_thinking: bool | None = None
    llm_document_vision_enable_thinking: bool | None = None
    llm_thinking_budget: int | None = None
    llm_web_search: bool
    llm_timeout_seconds: float
    intent_performance_llm_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=600,
    )
    personality_facet_llm_timeout_seconds: float = Field(
        default=45.0,
        gt=0,
        le=600,
    )
    rehearsal_dimension_llm_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    employee_reply_llm_timeout_seconds: float = Field(gt=0)
    conversation_summary_llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    motivation_scoring_llm_timeout_seconds: float = Field(gt=0, le=60)
    emotion_transition_llm_timeout_seconds: float = Field(gt=0, le=60)
    guidance_llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    coach_evaluator_llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    coach_redline_llm_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    llm_max_retries: int
    llm_response_format_style: Literal["auto", "bosch", "openai", "none"]
    langchain_structured_output_strategy: Literal["auto", "provider", "tool"]

    conversation_raw_turn_window: int = Field(default=12, ge=1)
    conversation_summary_batch_turns: int = Field(default=6, ge=1)
    employee_reply_test_prompt_enabled: bool = False
    employee_dialogue_history_mode: Literal["ecp_labels", "legacy_labels"] = (
        "ecp_labels"
    )
    # Controls Pattern inference, state updates, and Employee Reply guidance.
    psychological_pattern_dynamics_enabled: bool = True
    psychological_pattern_history_turns: int = Field(default=8, ge=1)
    psychological_pattern_supplemental_max_chars: int = Field(
        default=2_000,
        ge=1,
    )
    # When enabled, Jinja checks template mtimes on every lookup so prompt-only
    # edits become effective on the next request without restarting a backend.
    prompt_auto_reload_enabled: bool = True
    coach_conversation_context_max_chars: int = Field(default=32_000, ge=1_000)
    coach_rag_context_max_chars: int = Field(default=24_000, ge=1_000)

    mineru_enabled: bool
    mineru_api_url: str
    mineru_backend: Literal["pipeline", "vlm-engine", "vlm-http-client", "hybrid-engine", "hybrid-http-client"]
    mineru_effort: Literal["medium", "high"]
    mineru_parse_method: Literal["auto", "txt", "ocr"]
    mineru_lang: str = "ch"
    mineru_output_dir: Path | None = None
    mineru_timeout_seconds: float
    mineru_fail_on_error: bool

    document_vision_enabled: bool = True
    document_vision_model: str = "qwen3.7-plus"
    document_vision_prompt_version: str = "v1"
    document_vision_temperature: float = 0.1
    document_vision_max_tokens: int = 1600
    document_vision_timeout_seconds: float = 120.0
    document_vision_max_concurrency: int = 2
    document_vision_batch_size: int = 4
    document_vision_max_image_bytes: int = 10_000_000
    document_vision_context_chars: int = 3000
    document_vision_preflight_enabled: bool = True
    document_vision_fail_on_error: bool = False

    auth_enabled: bool = True
    # Allow unauthenticated visitors to use the user-facing workflow as a
    # temporary guest.  Guest requests are deliberately kept separate from
    # admin access and do not create an account or receive a login cookie.
    auth_guest_access_enabled: bool = False
    auth_cookie_name: str = "hragent_session"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    auth_session_idle_timeout_minutes: int = 30
    auth_session_absolute_timeout_hours: int = 8
    # Domain validation remains mandatory; individual email allowlisting is
    # disabled so any address in auth_allowed_email_domains may register.
    auth_whitelist_enabled: bool = False
    auth_allowed_emails: str = ""
    auth_admin_emails: str = ""
    auth_allowed_email_domains: str = "cn.bosch.com,bosch.com"
    auth_argon2_memory_cost: int = 19456
    auth_argon2_time_cost: int = 2
    auth_argon2_parallelism: int = 1
    auth_argon2_hash_len: int = 32
    auth_argon2_salt_len: int = 16
    auth_login_hash_max_concurrency: int = 8
    # Zero admits all authenticated sessions; positive values restore a cap.
    auth_max_active_sessions: int = Field(default=0, ge=0)
    auth_login_rate_limit_per_email: str = "5/minute"
    auth_login_rate_limit_per_ip: str = "20/minute"
    auth_register_rate_limit_per_ip: str = "3/hour"

    asr_enabled: bool = True
    asr_provider_mode: Literal["local", "bosch", "browser"] = "local"
    asr_api_key: str = ""
    asr_realtime_enabled: bool = True
    asr_realtime_model: str = "Qwen/Qwen3-ASR-1.7B"
    asr_local_streaming_url: str = "http://qwen3_asr:7118"
    # At most two model processes may be configured. Each process owns its
    # session state; callers above their combined inference capacity wait in
    # the model schedulers instead of causing more model replicas to start.
    asr_local_streaming_urls: str = ""
    asr_local_chunk_ms: int = Field(default=500, ge=250)
    asr_local_request_timeout_seconds: float = Field(default=0.0, ge=0)
    # A full local preview pool may wait briefly for a slot, but never forever.
    # Capture continues independently and falls back to full-recording ASR.
    asr_local_admission_timeout_seconds: float = Field(default=2.0, ge=0.1, le=30)
    asr_local_routing_probe_timeout_seconds: float = Field(
        default=0.25,
        ge=0.05,
        le=2.0,
    )
    asr_sample_rate: int = 16000
    asr_connect_timeout_seconds: float = 15.0
    asr_local_preview_max_concurrency: int = Field(default=10, ge=1, le=11)
    asr_local_stream_soft_window_seconds: float = Field(default=12.0, ge=5)
    asr_local_stream_window_seconds: float = Field(default=20.0, ge=5)
    asr_local_stream_overlap_seconds: float = Field(default=1.5, ge=0)
    asr_local_silence_commit_ms: int = Field(default=750, ge=100)
    asr_local_force_refresh_ms: int = Field(default=1000, ge=250)
    asr_local_silence_rms_threshold: float = Field(default=0.006, ge=0, le=1)
    # Bosch exposes file transcription rather than an upstream streaming
    # session. New audio triggers a 750 ms refresh, while each active speech
    # segment is re-read cumulatively so provisional text can be fixed.
    asr_bosch_preview_chunk_seconds: float = Field(default=0.75, ge=0.5, le=1.0)
    asr_bosch_first_preview_chunk_seconds: float = Field(
        default=0.4,
        ge=0.25,
        le=1.0,
    )
    asr_bosch_accumulation_max_seconds: float = Field(
        default=20.0,
        ge=3.0,
        le=60.0,
    )
    asr_bosch_preview_overlap_ms: int = Field(default=400, ge=0, le=2_000)
    asr_bosch_silence_commit_ms: int = Field(default=400, ge=100, le=5_000)
    asr_bosch_verified_stream_final_enabled: bool = True
    asr_bosch_final_hedge_delay_ms: int = Field(default=150, ge=0, le=5_000)
    asr_bosch_tail_final_timeout_seconds: float = Field(
        default=8.0, gt=0, le=120
    )
    asr_bosch_min_speech_ms: int = Field(default=250, ge=100, le=5_000)
    asr_bosch_preview_max_concurrency: int = Field(default=8, ge=1, le=64)
    asr_bosch_preview_per_session_concurrency: int = Field(
        default=2,
        ge=1,
        le=4,
    )
    # This is a cross-backend inference limit, not a recording-session limit.
    # Requests above the limit wait in Redis so multiple backend replicas do
    # not independently overload the same Bosch model deployment.
    asr_bosch_global_max_concurrency: int = Field(default=16, ge=0)
    asr_bosch_keepwarm_enabled: bool = True
    asr_bosch_keepwarm_interval_seconds: float = Field(
        default=45.0,
        ge=15.0,
        le=600.0,
    )
    # Leave blank while Bosch only exposes the file transcription endpoint.
    # Once a Qwen realtime-compatible proxy is available, this endpoint can be
    # enabled without changing the browser WebSocket protocol.
    asr_bosch_realtime_url: str = ""
    asr_bosch_realtime_model: str = "qwen3-asr-flash-realtime"
    asr_bosch_realtime_vad_threshold: float = Field(default=0.2, ge=-1, le=1)
    asr_bosch_realtime_silence_ms: int = Field(default=500, ge=100, le=5_000)
    asr_bosch_prompt: str = (
        "Bosch, 博世, TCL, SL1, SL2, G9, Performance Rating, "
        "Career Elements, Leadership Feedback, 高绩效文化"
    )
    asr_bosch_prompt_context_chars: int = Field(default=160, ge=0, le=2_000)
    # Zero means all authenticated recording sessions are admitted. GPU and
    # remote-ASR work remain bounded by their separate compute concurrency.
    asr_capture_max_connections: int = Field(default=0, ge=0)
    asr_local_final_enabled: bool = True
    asr_local_final_segment_seconds: int = Field(default=10, ge=3)
    asr_local_final_per_recording_concurrency: int = Field(default=8, ge=1, le=8)
    asr_local_final_timeout_seconds: float = Field(default=20.0, ge=1)
    asr_local_final_min_preview_ratio: float = Field(default=0.45, ge=0, le=1)
    # When enabled, omit the language hint and let local Qwen3-ASR identify any
    # language supported by the model. The legacy environment name remains an
    # input alias so existing deployments do not silently lose this behavior.
    asr_local_language_auto_detect: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "ASR_LOCAL_LANGUAGE_AUTO_DETECT",
            "ASR_LOCAL_ZH_EN_AUTO_DETECT",
            "asr_local_language_auto_detect",
        ),
    )
    asr_remote_fallback_on_stream_failure: bool = True
    asr_final_segment_seconds: int = Field(default=45, ge=10)
    asr_final_segment_overlap_seconds: float = Field(default=1.0, ge=0, le=5)
    asr_final_per_recording_concurrency: int = Field(default=8, ge=1, le=8)
    asr_final_max_concurrency: int = Field(default=8, ge=1)
    asr_recording_tmp_dir: Path = Path("/app/data/asr_tmp")
    asr_recording_min_free_bytes: int = Field(default=2_147_483_648, ge=0)
    asr_recording_orphan_ttl_seconds: int = Field(default=3600, ge=60)
    asr_http_url: str = "https://aigc.bosch.com.cn/llmservice/api/v1/audio/transcriptions"
    asr_http_model: str = "qwen3-asr-flash"
    asr_language: str = "zh"
    asr_timeout_seconds: float = 120.0
    # Zero removes the legacy single-file upload cap. The realtime path always
    # streams to disk and segments inference without limiting total duration.
    asr_max_file_bytes: int = Field(default=0, ge=0)

    tts_enabled: bool = True
    tts_provider: Literal["fish_legacy", "fish_vllm_omni"] = "fish_vllm_omni"
    tts_http_url: str = "http://fish_tts:8091/v1/audio/speech"
    tts_model: str = "/app/checkpoints/s2-pro"
    tts_model_revision: str = "1de9996b6be38b745688de084d87a5633f714e4e"
    tts_stage_0_max_num_seqs: int = Field(default=10, ge=1, le=12)
    tts_stage_1_max_num_seqs: int = Field(default=10, ge=1, le=12)
    tts_default_voice: str = "Fish Audio 默认音色"
    tts_available_voices: str = "Fish Audio 默认音色"
    tts_random_voice_name: str = "Fish Audio 默认音色"
    # Fish text-only generation has no speaker conditioning. Require a named,
    # uploaded reference voice so one rehearsal cannot silently drift between
    # different speaker identities.
    tts_require_registered_voice: bool = True
    # Seeded Fish requests retain independent RNG streams while the heavy
    # SlowAR/FastAR and DAC work is dynamically batched on the model worker.
    tts_max_concurrency: int = Field(default=10, ge=1, le=12)
    # Cross-backend admission limit for the single Fish inference service. Zero
    # disables Redis coordination while retaining the per-process safety bound.
    tts_global_max_concurrency: int = Field(default=10, ge=0)
    # Zero keeps callers queued until a synthesis slot is available.
    tts_queue_timeout_seconds: float = Field(default=0.0, ge=0)
    tts_send_timeout_seconds: float = Field(default=5.0, gt=0)
    tts_audio_queue_max_bytes: int = Field(default=2_097_152, ge=1)
    tts_audio_queue_max_events: int = Field(default=32, ge=1)
    # Playback pacing happens between sentences, before taking a synthesis slot.
    tts_playback_high_water_seconds: float = Field(default=8.0, gt=0, le=120)
    tts_playback_low_water_seconds: float = Field(default=4.0, ge=0, lt=120)
    tts_playback_feedback_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    tts_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    tts_generation_timeout_seconds: float = Field(default=120.0, gt=0)
    tts_sample_rate: int = Field(default=44_100, ge=8_000, le=96_000)
    tts_chunk_length: int = Field(default=200, ge=100, le=300)
    tts_sentence_max_chars: int = Field(default=180, ge=20, le=2_000)
    tts_first_segment_min_chars: int = Field(default=16, ge=1, le=2_000)
    tts_first_segment_max_chars: int = Field(default=64, ge=1, le=2_000)
    tts_first_segment_wait_ms: int = Field(default=350, ge=0, le=5_000)
    tts_sentence_merge_max_chars: int = Field(default=80, ge=1, le=2_000)
    tts_max_new_tokens: int = Field(default=1_024, ge=64, le=8_192)
    tts_top_p: float = Field(default=0.8, ge=0.1, le=1.0)
    tts_repetition_penalty: float = Field(default=1.1, ge=0.9, le=2.0)
    tts_temperature: float = Field(default=0.8, ge=0.1, le=1.0)
    tts_seed: int | None = Field(default=42, ge=0)
    # Fish S2-Pro emotion control is expressed as short inline cues at the
    # beginning of the input.  ``inline`` is the bounded production strategy;
    # ``legacy`` is an explicit rollback/A-B option and ``off`` provides a
    # neutral acoustic baseline.  The cap prevents callers from recreating the
    # old multi-axis prompt by accident.
    tts_emotion_tag_mode: Literal["inline", "legacy", "off"] = "inline"
    tts_emotion_max_tags: int = Field(default=2, ge=1, le=2)
    tts_emotion_delta_threshold: float = Field(default=0.10, ge=0.0, le=1.0)
    # The historical ``/v1/tts`` endpoint in this deployment also runs the
    # S2-Pro checkpoint, so its default cue syntax is square brackets.  Keep
    # parentheses as an explicit opt-in for a genuinely S1-compatible server;
    # do not infer syntax from the provider name alone.
    tts_legacy_emotion_tag_delimiter: Literal["square", "parentheses"] = "square"
    tts_use_memory_cache: bool = True
    tts_normalize: bool = True

    vectorstore_provider: Literal["postgres_pgvector"]
    database_url: str
    database_admin_url: str = ""
    postgres_auto_initialize_schema: bool = True
    postgres_pool_min_size: int = Field(default=1, ge=0)
    postgres_pool_max_size: int = Field(default=122, ge=1)
    postgres_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    postgres_create_hnsw_index: bool
    postgres_hnsw_iterative_scan: Literal["off", "strict_order", "relaxed_order"] = "relaxed_order"
    postgres_hnsw_ef_search: int = Field(default=100, ge=1)
    postgres_hnsw_max_scan_tuples: int = Field(default=20_000, ge=1)
    postgres_bm25_limit: int = Field(default=200, ge=1, le=65_535)
    postgres_bm25_tokenizer_name: str = "kb_jieba_v1"
    postgres_ann_min_rows: int = Field(default=10_000, ge=1)
    postgres_ann_force_scopes: str = "employee,organization_unit"
    # Zero disables the process-wide RAG database-search admission limit.
    rag_db_search_max_concurrency: int = Field(default=0, ge=0)
    rag_thread_pool_max_workers: int = Field(default=98, ge=1)
    workflow_db_thread_pool_max_workers: int = Field(default=16, ge=1, le=32)
    rag_rerank_max_concurrency: int = Field(default=98, ge=1)
    rag_hybrid_search_enabled: bool = True
    rag_agentic_search_mode: Literal["off", "shadow", "enabled", "on"] = "off"
    rag_agentic_search_shadow_sample_rate: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
    )
    rag_agentic_search_active_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
    )
    rag_agentic_search_timeout_seconds: float = Field(default=45.0, gt=0.0)
    model_queue_timeout_seconds: float = Field(default=120.0, gt=0)
    model_total_max_concurrency: int = Field(default=0, ge=0)
    coach_model_max_concurrency: int = Field(default=28, ge=1)
    vector_collection_prefix: str
    kb_sync_on_startup: bool = True
    kb_startup_sync_wait_timeout_seconds: float = Field(default=3_600.0, gt=0.0)
    # Runtime workers keep serving the previous verified generation while a
    # new knowledge build is prepared, then discover and atomically publish
    # the newly activated build without requiring a process restart.
    kb_active_profile_refresh_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=300.0,
    )
    kb_ingest_batch_size: int
    kb_chunk_size: int
    kb_chunk_overlap: int
    kb_parent_context_max_chars: int = Field(
        default=12_000,
        ge=2_048,
        le=100_000,
    )
    kb_parent_context_boundary_scan_chars: int = Field(
        default=800,
        ge=0,
        le=4_096,
    )
    kb_index_version: str

    enable_trace: bool
    cors_allow_origins: list[str] = ["*"]

    @property
    def data_dir(self) -> Path:
        return self.runtime_data_dir or self.project_root / "data"

    @property
    def kb_core_dir(self) -> Path:
        """Return the source root for the compact, precision-first KB."""
        return self.data_dir / "kb_raw"

    @property
    def kb_organization_unit_dir(self) -> Path:
        """Return the independently managed large organization-unit root."""
        return self.data_dir / "kb_large"

    @property
    def business_config_dir(self) -> Path:
        return self.project_root / "backend" / "business_config"

    @property
    def prompt_dir(self) -> Path:
        return self.project_root / "backend" / "prompts"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def resolved_mineru_output_dir(self) -> Path:
        return self.mineru_output_dir or self.kb_processed_dir

    @property
    def kb_processed_dir(self) -> Path:
        return self.data_dir / "kb_processed"

    @property
    def employee_database_dir(self) -> Path:
        return self.data_dir / "employee_database"

    @property
    def effective_redis_max_connections(self) -> int:
        """Return redis-py's historical practical-unbounded pool sentinel."""

        return self.redis_max_connections or 2**31

    @property
    def auth_session_idle_timeout_seconds(self) -> int:
        return max(60, self.auth_session_idle_timeout_minutes * 60)

    @property
    def auth_session_absolute_timeout_seconds(self) -> int:
        return max(self.auth_session_idle_timeout_seconds, self.auth_session_absolute_timeout_hours * 3600)

    @property
    def auth_allowed_email_set(self) -> set[str]:
        return self._parse_csv_set(self.auth_allowed_emails)

    @property
    def auth_admin_email_set(self) -> set[str]:
        return self._parse_csv_set(self.auth_admin_emails)

    @property
    def auth_allowed_domain_set(self) -> set[str]:
        return self._parse_csv_set(self.auth_allowed_email_domains)

    @property
    def local_model_pinned_service_set(self) -> set[str]:
        return self._parse_csv_set(self.local_model_pinned_services)

    @property
    def embedding_uses_local_runtime(self) -> bool:
        return self.model_provider_mode == "local"

    @property
    def rerank_uses_local_runtime(self) -> bool:
        return self.model_provider_mode == "local"

    @property
    def effective_embedding_provider(self) -> Literal[
        "openai_compatible", "bosch", "local_qwen"
    ]:
        if self.embedding_uses_local_runtime:
            return "local_qwen"
        return self.embedding_provider

    @property
    def effective_rerank_provider(self) -> Literal[
        "openai_compatible", "bosch", "local_qwen"
    ]:
        if self.rerank_uses_local_runtime:
            return "local_qwen"
        return self.rerank_provider

    @property
    def effective_embedding_model(self) -> str:
        if self.embedding_uses_local_runtime:
            return self.embedding_local_model.strip()
        return self.embedding_model.strip()

    @property
    def effective_embedding_dimensions(self) -> int:
        if self.embedding_uses_local_runtime:
            return int(self.embedding_local_dimensions)
        return int(self.embedding_dimensions)

    @property
    def embedding_profile_id(self) -> str:
        from backend.vectorstore.embedding_profile import embedding_profile_id

        return embedding_profile_id(
            self.effective_embedding_model,
            self.effective_embedding_dimensions,
        )

    @property
    def effective_rerank_model(self) -> str:
        if self.rerank_uses_local_runtime:
            return self.rerank_local_model.strip()
        return self.rerank_model.strip()

    @property
    def configured_local_model_services(self) -> frozenset[str]:
        services: set[str] = set()
        if self.embedding_uses_local_runtime:
            services.add("qwen_embedding")
        if self.rerank_uses_local_runtime:
            services.add("qwen_reranker")
        if (
            self.asr_enabled
            and self.asr_realtime_enabled
            and self.asr_provider_mode == "local"
        ):
            services.add("qwen3_asr")
        return frozenset(services)

    def local_model_service_enabled(self, service_name: str) -> bool:
        return service_name in self.configured_local_model_services

    @property
    def effective_asr_api_key(self) -> str:
        return self.asr_api_key.strip() or self.api_key.strip()

    @property
    def asr_local_streaming_endpoint_urls(self) -> tuple[str, ...]:
        configured = [
            item.strip().rstrip("/")
            for item in self.asr_local_streaming_urls.replace("\n", ",").split(",")
            if item.strip()
        ]
        primary = self.asr_local_streaming_url.strip().rstrip("/")
        if primary:
            configured.insert(0, primary)
        return tuple(dict.fromkeys(configured))

    @property
    def tts_voice_options(self) -> tuple[str, ...]:
        voices = tuple(
            dict.fromkeys(
                item.strip()
                for item in self.tts_available_voices.split(",")
                if item.strip()
            )
        )
        return voices or (self.tts_default_voice.strip() or "Fish Audio 默认音色",)

    @property
    def chat_url(self) -> str:
        return self._resolve_url(
            explicit_endpoint=self.chat_api_endpoint,
            base_url=self.chat_api_base_url,
            default_path="/chat/messages" if self.llm_provider == "bosch_messages" else "/chat/completions",
        )

    @property
    def chat_urls(self) -> tuple[str, ...]:
        configured = [
            item.strip()
            for item in self.chat_api_endpoints.replace("\n", ",").split(",")
            if item.strip()
        ]
        primary = self.chat_url.strip()
        if primary:
            configured.insert(0, primary)
        return tuple(dict.fromkeys(configured))

    @property
    def admin_database_url(self) -> str:
        return self.database_admin_url.strip() or self.database_url.strip()

    @property
    def embedding_url(self) -> str:
        if self.embedding_uses_local_runtime:
            return self.embedding_local_api_endpoint.strip()
        return self._resolve_url(
            explicit_endpoint=self.embedding_api_endpoint,
            base_url=self.embedding_api_base_url,
            default_path="/embeddings",
        )

    @property
    def rerank_url(self) -> str:
        if self.rerank_uses_local_runtime:
            return self.rerank_local_api_endpoint.strip()
        return self._resolve_url(
            explicit_endpoint=self.rerank_api_endpoint,
            base_url=self.rerank_api_base_url,
            default_path="/rerank",
        )

    def collection_name_for_scope(self, scope: str) -> str:
        safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in scope.strip()).strip("_") or "general"
        return f"{self.vector_collection_prefix}_{safe}"

    def model_for_task(self, task_name: str | None = None, explicit_model: str | None = None) -> str:
        if explicit_model:
            return explicit_model
        if self.chat_model:
            return self.chat_model
        mapping = {
            "profile": self.profile_model,
            "intent": self.intent_model,
            "intent_performance": (
                self.intent_performance_model or self.intent_model
            ),
            "personality_facets": (
                self.personality_facet_model or self.employee_model
            ),
            "rehearsal_dimensions": (
                self.rehearsal_dimension_model or self.emotion_transition_model
            ),
            "employee_reply": self.employee_model,
            "conversation_summary": self.conversation_summary_model,
            "motivation_scoring": self.motivation_scoring_model,
            "emotion_transition": self.emotion_transition_model,
            "employee_state_transition": self.emotion_transition_model,
            "psychological_pattern_dynamics": self.emotion_transition_model,
            "guidance": self.guidance_model,
            "guidance_evidence": self.guidance_model,
            "coach_evaluator": self.coach_evaluator_model,
            "coach_evidence": self.coach_evaluator_model,
            "coach_redline": self.coach_redline_model,
            "document_vision": self.document_vision_model,
        }
        if task_name and task_name in mapping and mapping[task_name]:
            return mapping[task_name]
        return self.default_chat_model

    def pricing_for_model(
        self,
        *,
        provider_name: str,
        model_name: str,
    ) -> LLMTokenPricing | None:
        """Resolve a provider-specific price, falling back to a model-only key."""

        provider = provider_name.strip()
        model = model_name.strip()
        if not model:
            return None
        provider_pricing = self.llm_model_pricing.get(f"{provider}/{model}")
        return provider_pricing or self.llm_model_pricing.get(model)

    def max_tokens_for_task(self, task_name: str | None = None) -> int | None:
        mapping = {
            "profile": self.llm_profile_max_tokens,
            "intent": self.llm_intent_max_tokens,
            "intent_performance": self.llm_intent_performance_max_tokens,
            "personality_facets": self.llm_personality_facet_max_tokens,
            "rehearsal_dimensions": self.llm_rehearsal_dimension_max_tokens,
            "employee_reply": self.llm_employee_max_tokens,
            "conversation_summary": self.llm_conversation_summary_max_tokens,
            "motivation_scoring": self.llm_motivation_scoring_max_tokens,
            "emotion_transition": self.llm_emotion_transition_max_tokens,
            "employee_state_transition": self.llm_employee_state_transition_max_tokens,
            "psychological_pattern_dynamics": (
                self.llm_psychological_pattern_dynamics_max_tokens
            ),
            "guidance": self.llm_guidance_max_tokens,
            "guidance_evidence": self.llm_guidance_max_tokens,
            "coach_evaluator": self.llm_coach_evaluator_max_tokens,
            "coach_evidence": self.llm_coach_evaluator_max_tokens,
            "coach_redline": self.llm_coach_redline_max_tokens,
            "document_vision": self.document_vision_max_tokens,
        }
        if task_name and task_name in mapping:
            return mapping[task_name]
        return self.llm_max_tokens

    def temperature_for_task(self, task_name: str | None = None) -> float:
        mapping = {
            "intent_performance": self.llm_intent_performance_temperature,
            "personality_facets": self.llm_personality_facet_temperature,
            "rehearsal_dimensions": self.llm_rehearsal_dimension_temperature,
            "employee_reply": self.llm_employee_temperature,
            "conversation_summary": self.llm_conversation_summary_temperature,
            "motivation_scoring": self.llm_motivation_scoring_temperature,
            "emotion_transition": self.llm_emotion_transition_temperature,
            "employee_state_transition": self.llm_emotion_transition_temperature,
            "psychological_pattern_dynamics": (
                self.llm_emotion_transition_temperature
            ),
            "guidance": self.llm_guidance_temperature,
            "guidance_evidence": self.llm_guidance_temperature,
            "coach_evaluator": self.llm_coach_evaluator_temperature,
            "coach_evidence": self.llm_coach_evaluator_temperature,
            "coach_redline": self.llm_coach_redline_temperature,
            "document_vision": self.document_vision_temperature,
        }
        if task_name and task_name in mapping:
            return mapping[task_name]
        return self.llm_temperature

    def timeout_for_task(self, task_name: str | None = None) -> float:
        mapping = {
            "intent_performance": self.intent_performance_llm_timeout_seconds,
            "personality_facets": self.personality_facet_llm_timeout_seconds,
            "rehearsal_dimensions": self.rehearsal_dimension_llm_timeout_seconds,
            "employee_reply": self.employee_reply_llm_timeout_seconds,
            "conversation_summary": self.conversation_summary_llm_timeout_seconds,
            "motivation_scoring": self.motivation_scoring_llm_timeout_seconds,
            "emotion_transition": self.emotion_transition_llm_timeout_seconds,
            "employee_state_transition": self.emotion_transition_llm_timeout_seconds,
            "psychological_pattern_dynamics": (
                self.emotion_transition_llm_timeout_seconds
            ),
            "guidance": self.guidance_llm_timeout_seconds,
            "guidance_evidence": self.guidance_llm_timeout_seconds,
            "coach_evaluator": self.coach_evaluator_llm_timeout_seconds,
            "coach_evidence": self.coach_evaluator_llm_timeout_seconds,
            "coach_redline": self.coach_redline_llm_timeout_seconds,
            "document_vision": self.document_vision_timeout_seconds,
        }
        if task_name and task_name in mapping:
            return mapping[task_name]
        return self.llm_timeout_seconds

    def enable_thinking_for_task(self, task_name: str | None = None) -> bool | None:
        mapping = {
            "profile": self.llm_profile_enable_thinking,
            "intent": self.llm_intent_enable_thinking,
            "intent_performance": self.llm_intent_performance_enable_thinking,
            "personality_facets": self.llm_personality_facet_enable_thinking,
            "rehearsal_dimensions": self.llm_rehearsal_dimension_enable_thinking,
            "employee_reply": self.llm_employee_reply_enable_thinking,
            "conversation_summary": self.llm_conversation_summary_enable_thinking,
            "motivation_scoring": self.llm_motivation_scoring_enable_thinking,
            "emotion_transition": self.llm_emotion_transition_enable_thinking,
            "employee_state_transition": self.llm_emotion_transition_enable_thinking,
            "psychological_pattern_dynamics": (
                self.llm_emotion_transition_enable_thinking
            ),
            "guidance": self.llm_guidance_enable_thinking,
            "guidance_evidence": self.llm_guidance_enable_thinking,
            "coach_evaluator": self.llm_coach_evaluator_enable_thinking,
            "coach_evidence": self.llm_coach_evaluator_enable_thinking,
            "coach_redline": self.llm_coach_redline_enable_thinking,
            "document_vision": self.llm_document_vision_enable_thinking,
        }
        task_value = mapping.get(task_name) if task_name else None
        return task_value if task_value is not None else self.llm_enable_thinking

    @staticmethod
    def _resolve_url(explicit_endpoint: str, base_url: str, default_path: str) -> str:
        endpoint = explicit_endpoint.strip()
        if endpoint:
            return endpoint
        base = base_url.strip().rstrip("/")
        if not base:
            return ""
        known_suffixes = (
            "/chat/completions",
            "/chat/messages",
            "/embeddings",
            "/rerank",
        )
        if base.endswith(known_suffixes):
            return base
        return f"{base}{default_path}"

    @staticmethod
    def _parse_csv_set(value: str) -> set[str]:
        return {item.strip().lower() for item in value.split(",") if item.strip()}

    @model_validator(mode="after")
    def validate_concurrency_topology(self) -> "Settings":
        if self.tts_playback_low_water_seconds >= self.tts_playback_high_water_seconds:
            raise ValueError("TTS playback low watermark must be below the high watermark.")
        if self.tts_first_segment_min_chars > self.tts_first_segment_max_chars:
            raise ValueError("TTS first segment min chars must not exceed max chars.")
        if self.rag_thread_pool_max_workers < self.rag_db_search_max_concurrency:
            raise ValueError(
                "RAG_THREAD_POOL_MAX_WORKERS must be greater than or equal to "
                "RAG_DB_SEARCH_MAX_CONCURRENCY."
            )
        if (
            self.rag_global_db_search_max_concurrency > 0
            and self.rag_db_search_max_concurrency > 0
            and self.rag_global_db_search_max_concurrency
            < self.rag_db_search_max_concurrency
        ):
            raise ValueError(
                "RAG_GLOBAL_DB_SEARCH_MAX_CONCURRENCY must not be lower than "
                "the per-process RAG_DB_SEARCH_MAX_CONCURRENCY."
            )
        if (
            self.rag_global_rerank_max_concurrency > 0
            and self.rag_global_rerank_max_concurrency
            < self.rag_rerank_max_concurrency
        ):
            raise ValueError(
                "RAG_GLOBAL_RERANK_MAX_CONCURRENCY must not be lower than "
                "the per-process RAG_RERANK_MAX_CONCURRENCY."
            )
        asr_urls = self.asr_local_streaming_endpoint_urls
        if len(asr_urls) > 2:
            raise ValueError(
                "ASR_LOCAL_STREAMING_URLS supports at most two ASR model "
                "instances; additional users must queue on those instances."
            )
        if self.asr_provider_mode == "local" and self.asr_realtime_enabled:
            if not asr_urls:
                raise ValueError(
                    "ASR_LOCAL_STREAMING_URL or ASR_LOCAL_STREAMING_URLS is required."
                )
            invalid_urls = [
                url
                for url in asr_urls
                if not url.lower().startswith(("http://", "https://"))
            ]
            if invalid_urls:
                raise ValueError(
                    "Every local ASR streaming URL must use http:// or https://."
                )
        realtime_url = self.asr_bosch_realtime_url.strip().lower()
        if realtime_url and not realtime_url.startswith("wss://"):
            raise ValueError("ASR_BOSCH_REALTIME_URL must use wss://.")
        if (
            self.workflow_global_db_max_concurrency
            < self.workflow_db_thread_pool_max_workers
        ):
            raise ValueError(
                "WORKFLOW_GLOBAL_DB_MAX_CONCURRENCY must not be lower than "
                "WORKFLOW_DB_THREAD_POOL_MAX_WORKERS."
            )
        if self.distributed_coordination_enabled and not self.redis_url.strip():
            raise ValueError(
                "REDIS_URL is required when DISTRIBUTED_COORDINATION_ENABLED=true."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)
    settings.resolved_mineru_output_dir.mkdir(parents=True, exist_ok=True)
    settings.employee_database_dir.mkdir(parents=True, exist_ok=True)
    return settings
