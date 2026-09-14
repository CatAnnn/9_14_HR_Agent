from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load(
        (ROOT / "deployment/compose/compose.services.yml").read_text(
            encoding="utf-8"
        )
    )


def test_root_has_one_compose_yaml_entrypoint() -> None:
    assert {path.name for path in ROOT.glob("*.yml")} == {"docker-compose.yml"}
    assert not (ROOT / "deployment/compose/compose.services.yml.orig").exists()


def test_compose_omits_confirmed_dead_runtime_overrides() -> None:
    services = _compose()["services"]

    assert "HF_HUB_DISABLE_XET" not in services["qwen_embedding"]["environment"]
    assert services["qwen_reranker"]["environment"]["HF_HUB_DISABLE_XET"] == "1"

    mineru_environment = services["mineru"]["environment"]
    assert "MINERU_API_MAX_CONCURRENT_REQUESTS" not in mineru_environment
    assert "MINERU_TASK_RESULT_TIMEOUT_SECONDS" not in mineru_environment

    backend_environment = services["backend"]["environment"]
    for key in (
        "LOCAL_MODEL_DOCKER_SOCKET_PATH",
        "LOCAL_MODEL_DOCKER_PROJECT_NAME",
        "ASR_LOCAL_STREAM_SOFT_WINDOW_SECONDS",
        "ASR_LOCAL_STREAM_OVERLAP_SECONDS",
        "ASR_LOCAL_SILENCE_COMMIT_MS",
        "ASR_LOCAL_FORCE_REFRESH_MS",
        "ASR_LOCAL_FINAL_ENABLED",
    ):
        assert key not in backend_environment

    assert "LOCAL_MODEL_DOCKER_PROJECT_NAME" not in services["kb_sync"][
        "environment"
    ]


def test_maintenance_services_are_excluded_from_default_runtime() -> None:
    compose = _compose()
    services = compose["services"]

    assert services["mineru"]["profiles"] == ["knowledge-build"]
    assert services["fish_tts_model_init"]["profiles"] == ["model-init"]
    assert "depends_on" not in services["fish_tts"]
    assert "fish_tts_references" not in compose["volumes"]


def test_mineru_image_uses_pinned_release_and_preloads_vlm() -> None:
    mineru = _compose()["services"]["mineru"]
    dockerfile = (ROOT / "mineru_service/Dockerfile").read_text(encoding="utf-8")

    assert mineru["build"]["context"] == "./mineru_service"
    assert mineru["build"]["dockerfile"] == "Dockerfile"
    assert '"mineru[core]==3.4.5"' in dockerfile
    assert "MINERU_API_MAX_CONCURRENT_REQUESTS=2" in dockerfile
    assert '"--enable-vlm-preload", "true"' in dockerfile
    assert '"--enable-vlm-preload", "false"' not in dockerfile


def test_online_services_keep_security_and_persistence_boundaries() -> None:
    services = _compose()["services"]

    assert all(
        "/var/run/docker.sock" not in volume
        for volume in services["backend"].get("volumes", [])
    )
    assert "/var/run/docker.sock:/var/run/docker.sock" in services[
        "local_model_controller"
    ]["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in services[
        "backend_autoscaler"
    ]["volumes"]
    assert services["backend_autoscaler"].get("ports", []) == []
    assert services["backend_autoscaler"]["cap_drop"] == ["ALL"]
    assert services["backend_autoscaler"]["user"] == "1956470149:1956470149"
    assert services["backend_autoscaler"]["group_add"] == [
        "${DOCKER_SOCKET_GID:-999}"
    ]
    assert services["backend_autoscaler"]["environment"]["HOME"] == "/tmp"
    assert services["backend_autoscaler"]["environment"][
        "BACKEND_AUTOSCALER_COMPOSE_WORKING_DIR"
    ] == "/home/uay4sgh/hr_agent/06_emotion"
    assert services["backend"]["stop_grace_period"] == "300s"
    assert services["fish_tts"]["stop_grace_period"] == "120s"


def test_postgres_has_enough_shared_memory_for_hnsw_index_maintenance() -> None:
    postgres = _compose()["services"]["postgres"]

    assert postgres["shm_size"] == "1g"


def test_backend_mounts_pattern_catalog_from_business_config_only() -> None:
    volumes = _compose()["services"]["backend"]["volumes"]

    assert (
        "./backend/business_config/patterns_data.json:"
        "/app/backend/business_config/patterns_data.json:ro"
    ) in volumes
    assert all("/app/patterns_data.json" not in volume for volume in volumes)


def test_backend_runtime_services_share_unique_project_image() -> None:
    compose_path = ROOT / "deployment/compose/compose.services.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    services = yaml.safe_load(compose_text)["services"]
    expected_image = "${HR_AGENT_BACKEND_IMAGE:-06-emotion-main-backend:local}"

    for service_name in (
        "local_model_controller",
        "backend_migrate",
        "employee_sync",
        "backend",
        "backend_autoscaler",
    ):
        assert services[service_name]["image"] == expected_image

    assert "image: 06-backend" not in compose_text


def test_qwen_vllm_services_use_their_compatible_runtime_images() -> None:
    services = _compose()["services"]
    expected_build = {
        "context": ".",
        "dockerfile": "deployment/vllm/Dockerfile.v0.8.5-no-xet",
    }
    expected_image = "06-emotion-qwen-vllm:v0.8.5-no-xet"

    assert services["qwen_embedding"]["build"] == expected_build
    assert services["qwen_embedding"]["image"] == expected_image
    assert "build" not in services["qwen_reranker"]
    assert services["qwen_reranker"]["image"] == "vllm/vllm-openai:v0.27.1"
    assert "pooling" in services["qwen_reranker"]["command"]

    dockerfile = (
        ROOT / "deployment/vllm/Dockerfile.v0.8.5-no-xet"
    ).read_text(encoding="utf-8")
    assert dockerfile.startswith(
        "FROM vllm/vllm-openai:v0.8.5@sha256:"
        "6cf9808ca8810fc6c3fd0451c2e7784fb224590d81f7db338e7eaf3c02a33d33\n"
    )
    assert "pip uninstall --yes hf-xet" in dockerfile
    assert "find_spec('hf_xet') is None" in dockerfile


def test_project_and_persistent_volume_names_are_stable() -> None:
    entry = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    volumes = _compose()["volumes"]

    assert entry["name"] == "06-emotion-main"
    assert volumes["postgres_data"]["name"] == "06_postgres_data"
    assert volumes["redis_data"]["name"] == "06_redis_data"
    assert volumes["mineru_hf_cache"]["name"] == "06_mineru_hf_cache"
    assert volumes["mineru_vllm_cache"]["name"] == "06_mineru_vllm_cache"
    assert volumes["qwen_hf_cache"]["name"] == "06_qwen_hf_cache"
    assert volumes["qwen_vllm_cache"]["name"] == "06_qwen_vllm_cache"
    assert volumes["qwen_vllm_reranker_cache"]["name"] == "06_qwen_vllm_reranker_cache"
    assert volumes["fish_tts_checkpoints"]["name"] == "06_fish_tts_checkpoints"
