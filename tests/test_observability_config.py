from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_signoz_foundry_maps_ui_to_requested_port():
    casting = _yaml("observability/signoz/casting.yaml")

    assert casting["spec"]["deployment"] == {"flavor": "compose", "mode": "docker"}
    operations = casting["spec"]["patches"][0]["operations"]
    assert {
        "op": "replace",
        "path": "/services/signoz-signoz-0/ports/0",
        "value": "7117:8080",
    } in operations
    ingester_command = next(
        item["value"]
        for item in operations
        if item["path"] == "/services/ingester/command"
    )
    assert "--config=/etc/otel-collector-config.yaml" in ingester_command[1]
    assert "--manager-config" not in ingester_command[1]


def test_signoz_foundry_installs_restricted_runtime_recycler():
    casting = _yaml("observability/signoz/casting.yaml")
    operations = casting["spec"]["patches"][0]["operations"]
    recycler = next(
        item["value"]
        for item in operations
        if item["path"] == "/services/signoz-runtime-recycler"
    )

    assert recycler["command"] == [
        "python",
        "-m",
        "backend.services.signoz_runtime_recycler",
    ]
    assert recycler["environment"]["SIGNOZ_AUTO_RECYCLE_ENABLED"].endswith(":-true}")
    assert recycler["environment"]["SIGNOZ_IDLE_TTL_SECONDS"].endswith(":-900}")
    assert "/var/run/docker.sock:/var/run/docker.sock" in recycler["volumes"]
    assert "../../../../backend:/app/backend:ro" in recycler["volumes"]
    assert recycler["security_opt"] == ["no-new-privileges:true"]
    expected_image = "${HR_AGENT_BACKEND_IMAGE:-06-emotion-main-backend:local}"
    assert recycler["image"] == expected_image

    generated_path = "observability/signoz/pours/deployment/compose.yaml"
    generated = _yaml(generated_path)
    generated_recycler = generated["services"]["signoz-runtime-recycler"]
    assert generated_recycler["container_name"] == "signoz-runtime-recycler"
    assert generated_recycler["restart"] == "unless-stopped"
    assert generated_recycler["networks"] == ["signoz-network"]
    assert generated_recycler["image"] == expected_image
    assert "image: 06-backend" not in (
        ROOT / "observability/signoz/casting.yaml"
    ).read_text(encoding="utf-8")
    assert "image: 06-backend" not in (ROOT / generated_path).read_text(
        encoding="utf-8"
    )


def test_collection_agent_captures_application_and_infrastructure_signals():
    config = _yaml("observability/otel-collector-config.yaml")

    assert {"otlp", "hostmetrics", "docker_stats"} <= set(config["receivers"])
    assert "filelog/docker" in config["receivers"]
    docker_stats = config["receivers"]["docker_stats"]
    assert docker_stats["api_version"] == "1.44"
    assert docker_stats["container_labels_to_metric_labels"] == {
        "com.docker.compose.project": "docker.compose.project",
        "com.docker.compose.service": "docker.compose.service",
    }
    assert config["exporters"]["otlphttp/signoz"]["endpoint"] == "${env:SIGNOZ_OTLP_HTTP_ENDPOINT}"
    assert config["service"]["pipelines"]["traces"]["receivers"] == ["otlp"]
    assert set(config["service"]["pipelines"]["metrics"]["receivers"]) == {
        "otlp",
        "hostmetrics",
        "docker_stats",
    }
    assert config["service"]["pipelines"]["logs"]["receivers"] == ["filelog/docker"]


def test_compose_instruments_backend_without_granting_docker_control():
    compose = _yaml("deployment/compose/compose.services.yml")
    services = compose["services"]
    backend = services["backend"]
    agent = services["signoz_collection_agent"]

    assert "opentelemetry-instrument uvicorn backend.main:app" in backend["command"]
    assert backend["environment"]["OTEL_SERVICE_NAME"] == "hr-agent-backend"
    assert backend["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://signoz_collection_agent:4318"
    assert backend["environment"]["OTEL_LOGS_EXPORTER"] == "none"
    assert all("/var/run/docker.sock" not in volume for volume in backend["volumes"])
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in agent["volumes"]
    assert "/var/lib/docker/containers:/var/lib/docker/containers:ro" in agent["volumes"]
    assert agent["environment"]["SIGNOZ_OTLP_HTTP_ENDPOINT"] == "http://signoz-ingester:4318"
    assert set(agent["networks"]) == {"default", "signoz"}
    assert compose["networks"]["signoz"] == {"name": "signoz-network", "external": True}


def test_backend_image_installs_official_zero_code_instrumentation():
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "backend/requirements.txt").read_text(encoding="utf-8")

    assert "opentelemetry-distro" in requirements
    assert "opentelemetry-exporter-otlp" in requirements
    assert "opentelemetry-bootstrap --action=install" in dockerfile
    assert "chmod -R a+rX /app/backend /app/scripts" in dockerfile
