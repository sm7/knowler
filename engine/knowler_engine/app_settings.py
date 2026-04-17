from __future__ import annotations

import json
import os
from typing import Any

from knowler_engine.storage.db import Database

DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "active_project_root_path": None,
    "llm_provider": "anthropic",
    "default_model_tier": "balanced",
    "privacy_mode": "local_first",
    "obsidian_vault_path": "",
}

_APP_SETTING_KEYS = {
    "active_project_root_path": "ui.active_project_root_path",
    "llm_provider": "llm.provider",
    "default_model_tier": "llm.default_model_tier",
    "privacy_mode": "privacy.mode",
    "obsidian_vault_path": "obsidian.vault_path",
}

_VALID_MODEL_TIERS = {"fast", "balanced", "best"}
_VALID_PRIVACY_MODES = {"local_first", "restricted_cloud", "local_only"}
_VALID_PROVIDERS = {"anthropic", "openai"}


def api_key_fallback_key(provider: str) -> str:
    return f"llm.api_key_fallback.{_normalize_provider(provider)}"


async def get_setting(db: Database, key: str, default: Any = None) -> Any:
    row = await db.fetchone("SELECT value_json FROM app_settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


async def set_setting(db: Database, key: str, value: Any) -> None:
    await db.execute(
        """
        INSERT INTO app_settings(key, value_json, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
          value_json=excluded.value_json,
          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        (key, json.dumps(value)),
    )
    await db.commit()


async def set_settings(db: Database, values: dict[str, Any]) -> None:
    if not values:
        return
    await db.executemany(
        """
        INSERT INTO app_settings(key, value_json, updated_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        ON CONFLICT(key) DO UPDATE SET
          value_json=excluded.value_json,
          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        """,
        [(key, json.dumps(value)) for key, value in values.items()],
    )
    await db.commit()


async def delete_setting(db: Database, key: str) -> None:
    await db.execute("DELETE FROM app_settings WHERE key=?", (key,))
    await db.commit()


async def get_app_settings(db: Database) -> dict[str, Any]:
    settings = dict(DEFAULT_APP_SETTINGS)
    for public_key, db_key in _APP_SETTING_KEYS.items():
        settings[public_key] = await get_setting(db, db_key, settings[public_key])
    settings["llm_provider"] = _normalize_provider(settings.get("llm_provider"))
    settings["default_model_tier"] = _normalize_model_tier(
        settings.get("default_model_tier")
    )
    settings["privacy_mode"] = _normalize_privacy_mode(settings.get("privacy_mode"))
    settings["obsidian_vault_path"] = _normalize_text(settings.get("obsidian_vault_path"))
    settings["active_project_root_path"] = _normalize_optional_path(
        settings.get("active_project_root_path")
    )
    return settings


async def update_app_settings(db: Database, values: dict[str, Any]) -> dict[str, Any]:
    persisted: dict[str, Any] = {}
    for public_key, value in values.items():
        db_key = _APP_SETTING_KEYS.get(public_key)
        if not db_key:
            continue
        if public_key == "llm_provider":
            persisted[db_key] = _normalize_provider(value)
        elif public_key == "default_model_tier":
            persisted[db_key] = _normalize_model_tier(value)
        elif public_key == "privacy_mode":
            persisted[db_key] = _normalize_privacy_mode(value)
        elif public_key == "obsidian_vault_path":
            persisted[db_key] = _normalize_text(value)
        elif public_key == "active_project_root_path":
            persisted[db_key] = _normalize_optional_path(value)
    await set_settings(db, persisted)
    return await get_app_settings(db)


async def resolve_llm_provider(db: Database) -> str:
    if provider := os.environ.get("KNOWLER_LLM_PROVIDER"):
        return _normalize_provider(provider)
    settings = await get_app_settings(db)
    return _normalize_provider(settings.get("llm_provider"))


async def resolve_api_key(db: Database, provider: str) -> tuple[str | None, str]:
    provider = _normalize_provider(provider)

    if key := os.environ.get("KNOWLER_LLM_API_KEY"):
        return key, "env"

    env_var = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
    if key := os.environ.get(env_var):
        return key, "env"

    try:
        import keyring

        if key := keyring.get_password("Knowler", provider):
            return key, "keychain"
    except Exception:
        pass

    fallback = await get_setting(db, api_key_fallback_key(provider), None)
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip(), "app_db"

    return None, "none"


def _normalize_provider(provider: Any) -> str:
    if isinstance(provider, str) and provider in _VALID_PROVIDERS:
        return provider
    return DEFAULT_APP_SETTINGS["llm_provider"]


def _normalize_model_tier(tier: Any) -> str:
    if isinstance(tier, str) and tier in _VALID_MODEL_TIERS:
        return tier
    return DEFAULT_APP_SETTINGS["default_model_tier"]


def _normalize_privacy_mode(mode: Any) -> str:
    if isinstance(mode, str) and mode in _VALID_PRIVACY_MODES:
        return mode
    return DEFAULT_APP_SETTINGS["privacy_mode"]


def _normalize_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_optional_path(value: Any) -> str | None:
    normalized = _normalize_text(value)
    return normalized or None
