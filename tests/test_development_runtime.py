from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _yaml(relative_path: str) -> dict:
    return yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))


def test_runtime_mode_routes_to_explicit_compose_overlays() -> None:
    router = (ROOT / "deployment/compose/compose.mode.yml").read_text(
        encoding="utf-8"
    )
    example = (ROOT / "backend/config/.env.example").read_text(encoding="utf-8")

    assert "compose.app-runtime.${APP_RUNTIME_MODE:-production}.yml" in router
    assert _yaml("deployment/compose/compose.app-runtime.production.yml") == {
        "services": {}
    }
    assert "APP_RUNTIME_MODE=production" in example


def test_development_backend_reloads_only_source_and_business_config() -> None:
    backend = _yaml(
        "deployment/compose/compose.app-runtime.development.yml"
    )["services"]["backend"]

    assert "./backend:/app/backend:ro" in backend["volumes"]
    assert "--reload-dir /app/backend" in backend["command"]
    for suffix in ("*.py", "*.yaml", "*.yml"):
        assert f"--reload-include '{suffix}'" in backend["command"]
    assert "*.jinja2" not in backend["command"]
    assert backend["environment"]["WATCHFILES_FORCE_POLLING"] == "true"
    assert backend["environment"]["KB_SYNC_ON_STARTUP"] == "false"
    assert backend["environment"]["MODEL_WARMUP_ENABLED"] == "false"


def test_development_frontend_keeps_nginx_edge_and_uses_vite_hmr() -> None:
    services = _yaml(
        "deployment/compose/compose.app-runtime.development.yml"
    )["services"]
    frontend_dev = services["frontend_dev"]
    mounts = frontend_dev["volumes"]

    assert services["frontend"]["depends_on"]["frontend_dev"]["condition"] == (
        "service_healthy"
    )
    assert frontend_dev["build"]["target"] == "build"
    assert frontend_dev["expose"] == ["8080"]
    assert "ports" not in frontend_dev
    assert frontend_dev["environment"]["VITE_HMR_SECURE"] == "true"
    assert frontend_dev["environment"]["VITE_USE_POLLING"] == "true"
    for mount in (
        "./frontend/src:/app/frontend/src:ro",
        "./frontend/vite.config.ts:/app/frontend/vite.config.ts:ro",
        "./data/frontend:/app/data/frontend:ro",
        "./Ebook:/app/Ebook:ro",
    ):
        assert mount in mounts

    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    assert 'set $frontend_runtime_mode "${APP_RUNTIME_MODE}";' in nginx
    assert "error_page 418 = @frontend_dev;" in nginx
    assert "location @frontend_dev" in nginx
    assert "proxy_set_header Upgrade $http_upgrade;" in nginx
    assert "location /api/v1/" in nginx

