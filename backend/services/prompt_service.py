from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from backend.config.settings import get_settings


@lru_cache(maxsize=8)
def _prompt_environment(prompt_dir: str, auto_reload: bool) -> Environment:
    return Environment(
        loader=FileSystemLoader(prompt_dir),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        auto_reload=auto_reload,
    )


class PromptService:
    def __init__(
        self,
        prompt_dir: Path | None = None,
        *,
        auto_reload: bool | None = None,
    ):
        settings = get_settings()
        self.prompt_dir = prompt_dir or settings.prompt_dir
        self.auto_reload = (
            settings.prompt_auto_reload_enabled
            if auto_reload is None
            else auto_reload
        )
        self.env = _prompt_environment(
            str(self.prompt_dir.resolve()),
            self.auto_reload,
        )

    def render(self, template_path: str, **kwargs) -> str:
        return self.env.get_template(template_path).render(**kwargs)

    @staticmethod
    def clear_cache() -> None:
        _prompt_environment.cache_clear()
