import os
from unittest.mock import patch

import pytest

from knowler_engine.app_settings import get_app_settings, resolve_llm_provider, update_app_settings
from knowler_engine.storage.db import open_global_db


@pytest.mark.asyncio
async def test_app_settings_round_trip(tmp_path):
    db = await open_global_db(tmp_path / "support")
    try:
        settings = await update_app_settings(
            db,
            {
                "active_project_root_path": "/Users/test/research",
                "llm_provider": "openai",
                "default_model_tier": "best",
                "privacy_mode": "local_only",
                "obsidian_vault_path": "Research Vault",
            },
        )

        assert settings["active_project_root_path"] == "/Users/test/research"
        assert settings["llm_provider"] == "openai"
        assert settings["default_model_tier"] == "best"
        assert settings["privacy_mode"] == "local_only"
        assert settings["obsidian_vault_path"] == "Research Vault"

        persisted = await get_app_settings(db)
        assert persisted == settings
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resolve_llm_provider_uses_saved_setting(tmp_path):
    db = await open_global_db(tmp_path / "support")
    try:
        await update_app_settings(db, {"llm_provider": "openai"})

        with patch.dict(os.environ, {}, clear=True):
            assert await resolve_llm_provider(db) == "openai"
    finally:
        await db.close()
