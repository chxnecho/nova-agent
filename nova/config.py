"""Configuration loading: YAML defaults + .env + environment variable overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PREFIX = "NOVA_"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Minimal .env loader (no external dependency). Returns loaded key/values."""
    env_path = path or PROJECT_ROOT / ".env"
    loaded: dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def _deep_get(d: dict[str, Any], dotted: str) -> Any:
    cur: Any = d
    for part in dotted.split("__"):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _deep_set(d: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split("__")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _coerce(value: str, original: Any) -> Any:
    """Coerce an env string to the type of the existing config value."""
    if isinstance(original, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(original, int):
        try:
            return int(value)
        except ValueError:
            return value
    if isinstance(original, float):
        try:
            return float(value)
        except ValueError:
            return value
    return value


class Config:
    """Nested configuration with attribute-style access: cfg.llm.model."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError as exc:  # pragma: no cover
            raise AttributeError(name) from exc
        if isinstance(value, dict):
            return Config(value)
        return value

    def get(self, dotted: str, default: Any = None) -> Any:
        value = _deep_get(self._data, dotted.replace(".", "__"))
        return default if value is None else value

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def __repr__(self) -> str:  # pragma: no cover
        # never dump secrets
        safe = {
            k: ("***" if "key" in k.lower() or "token" in k.lower() else v)
            for k, v in self._data.items()
        }
        return f"Config({safe!r})"


def load_config(config_path: Path | None = None) -> Config:
    """Load config/default.yaml, apply NOVA_* env overrides (nested via __)."""
    load_dotenv()
    path = config_path or PROJECT_ROOT / "config" / "default.yaml"
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    for key, value in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        dotted = key[len(ENV_PREFIX) :]
        if "__" not in dotted:
            # top-level simple vars like NOVA_LLM_PROVIDER map to known sections
            section_map = {
                "LLM_PROVIDER": "llm__provider",
                "LLM_MODEL": "llm__model",
                "LLM_BASE_URL": "llm__base_url",
                "LOG_LEVEL": "logging__level",
            }
            dotted = section_map.get(dotted, dotted.lower())
        existing = _deep_get(data, dotted)
        _deep_set(data, dotted, _coerce(value, existing) if existing is not None else value)

    return Config(data)


def api_key_for(cfg: Config) -> str:
    """Resolve the API key from env based on the configured provider.

    The `mock` provider needs no key at all — return "" so the offline
    mode works from the CLI (run/team/chat) without any API key set.
    """
    provider = cfg.get("llm.provider", "openrouter")
    if provider == "mock":
        return ""
    candidates = {
        "openrouter": ["OPENROUTER_API_KEY"],
        "openai-compatible": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
    }
    for name in candidates.get(provider, []):
        if os.environ.get(name):
            return os.environ[name]
    raise RuntimeError(
        f"No API key found for provider '{provider}'. "
        f"Set one of {candidates.get(provider, ['API_KEY'])} in your environment or .env file."
    )
