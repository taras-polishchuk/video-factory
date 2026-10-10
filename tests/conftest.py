"""Pytest fixtures shared by the test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Pre-import api.main at session start so the module-level
# `app = create_app()` (line 1558 of api/main.py, used to support
# `uvicorn api.main:app` for production) runs BEFORE any test
# fixture sets VIDEO_FACTORY_DB. Without this pre-import the
# module-level create_app() races with the test-side
# monkeypatch.setenv("VIDEO_FACTORY_DB", str(tmp_path/"vf.db"))
# and writes a plaintext vf.db at the test path before the
# cipher Repo is constructed by `make_app(db_key=OLD_KEY)`,
# which then fails with `PlaintextDBRefused`. The session
# import runs once with the operator's real env (or empty
# defaults) so module-level state is fully resolved before
# any test fixture starts monkeypatching.
@pytest.fixture(scope="session", autouse=True)
def _prime_api_main_module_level_app():
    import api.main  # noqa: F401 - side-effect import
    yield


@pytest.fixture
def schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "video-pack.schema.json"


@pytest.fixture
def schema(schema_path: Path) -> dict:
    return json.loads(schema_path.read_text())


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "artifacts"


def make_offline_pack_args():
    """Standard offline-demo arguments as plain dicts."""
    concept = {
        "title": "Why your coffee tastes better at altitude",
        "logline": "Short explainer about extraction and water boiling point.",
        "tone": "curious, calm, factual",
        "audience": "curious home baristas",
    }
    research = [
        {
            "text": "Water boils at lower temperatures at high altitude.",
            "source_title": "NIST Chemistry WebBook",
            "source_url": "https://webbook.nist.gov/chemistry/",
            "accessed_at": "2026-09-30T10:00:00Z",
            "confidence": "verified",
        }
    ]
    script = {
        "full_text": "Coffee tastes different up here. Water boils cooler.",
        "approved_by": "test-approver",
    }
    voice = {
        "provider": "mock",
        "voice_id": "mock_en_neutral",
        "ssml_or_text": script["full_text"],
    }
    captions = [
        {"start_seconds": 0.0, "end_seconds": 2.5, "text": "Coffee tastes different up here."},
        {"start_seconds": 2.5, "end_seconds": 6.0, "text": "Water boils cooler."},
    ]
    scenes = [
        {"scene_id": "sc_a", "order": 1, "target_duration_seconds": 4.0, "visual_intent": "Hook."},
        {"scene_id": "sc_b", "order": 2, "target_duration_seconds": 6.0, "visual_intent": "Body."},
    ]
    return concept, research, script, voice, captions, scenes