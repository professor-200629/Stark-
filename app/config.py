"""Configuration. Every value is optional — STARK degrades gracefully."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


@dataclass(frozen=True)
class Settings:
    # Hindsight
    hindsight_base_url: str = field(default_factory=lambda: _env("HINDSIGHT_BASE_URL"))
    hindsight_api_key: str = field(default_factory=lambda: _env("HINDSIGHT_API_KEY"))
    bank_id: str = field(default_factory=lambda: _env("HINDSIGHT_BANK_ID", "stark-oncall"))

    # LLM
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-120b"))
    groq_base_url: str = field(
        default_factory=lambda: _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    )

    # App
    host: str = field(default_factory=lambda: _env("STARK_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("STARK_PORT", "8000")))
    state_dir: Path = field(default_factory=lambda: Path(_env("STARK_STATE_DIR", ".stark")))

    @property
    def hindsight_enabled(self) -> bool:
        return bool(self.hindsight_base_url)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)


settings = Settings()
