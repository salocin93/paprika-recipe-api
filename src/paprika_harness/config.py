"""Configuration loading from environment variables / a ``.env`` file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class LLMConfig:
    """Connection settings for an OpenAI-compatible local LLM server."""

    base_url: str | None
    api_key: str | None
    model: str | None
    max_rpm: float | None = None  # client-side requests-per-minute cap

    @property
    def is_configured(self) -> bool:
        return bool(self.base_url and self.model)

    def require(self) -> "LLMConfig":
        """Return self, or raise if the LLM settings are incomplete."""
        missing = [
            name
            for name, value in (
                ("LOCAL_LLM_BASE_URL", self.base_url),
                ("LOCAL_LLM_MODEL", self.model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"Missing LLM settings: {', '.join(missing)}")
        return self


@dataclass(frozen=True)
class Settings:
    recipes_dir: Path
    category_map_file: Path
    llm: LLMConfig
    paprika_email: str | None = field(default=None, repr=False)
    paprika_password: str | None = field(default=None, repr=False)


def _path(value: str, base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p).resolve()


def _opt(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from ``env_file`` (default: ``<project>/.env``) and the environment.

    Real environment variables take precedence over values in the file.
    Relative paths are resolved against the project root.
    """
    env_file = env_file or PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    return Settings(
        recipes_dir=_path(os.getenv("RECIPES_DIR", "recipes"), PROJECT_ROOT),
        category_map_file=_path(
            os.getenv("CATEGORY_MAP_FILE", "category_map.yaml"), PROJECT_ROOT
        ),
        llm=LLMConfig(
            base_url=_opt("LOCAL_LLM_BASE_URL"),
            api_key=_opt("LOCAL_LLM_API_KEY"),
            model=_opt("LOCAL_LLM_MODEL"),
            max_rpm=float(_opt("LOCAL_LLM_MAX_RPM") or 0) or None,
        ),
        paprika_email=_opt("PAPRIKA_EMAIL"),
        paprika_password=_opt("PAPRIKA_PASSWORD"),
    )
