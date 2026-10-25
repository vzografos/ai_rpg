"""Skeleton layout pin (M0): every module from the documented layout exists and imports,
the static page ships in-repo, and the adventure stub is valid JSON.

M1+ tests replace this as the modules grow — keep this file as the layout
guard.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The container image ships src/ + tests/ only; the repo-level docs
# (README, AGENTS, docker/, Makefile) live at the host checkout.
IN_IMAGE = not (REPO_ROOT / "Makefile").exists()

MODULES = [
    "rpg",
    "rpg.cli",
    "rpg.web.app",
    "rpg.llm.provider",
    "rpg.llm.prompts",
    "rpg.engine.dice",
    "rpg.engine.rules",
    "rpg.engine.combat",
    "rpg.engine.state",
    "rpg.engine.events",
    "rpg.engine.context",
    "rpg.engine.backstops",
    "rpg.dm.loop",
    "rpg.dm.tools",
    "rpg.dm.arbiter",
    "rpg.dm.spine",
    "rpg.content",
]


def test_all_skeleton_modules_import():
    for name in MODULES:
        importlib.import_module(name)


def test_static_page_ships_in_repo():
    page = REPO_ROOT / "src" / "rpg" / "web" / "static" / "index.html"
    assert page.exists()
    text = page.read_text(encoding="utf-8")
    assert "action-form" in text
    assert "text/event-stream" not in text  # the client parses frames, never a MIME string


def test_adventure_stub_is_valid_json_with_expected_keys():
    path = REPO_ROOT / "src" / "rpg" / "content" / "shadow_crown.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "placeholder"
    assert set(data) >= {"title", "spine", "locations", "npcs", "encounters"}


@pytest.mark.skipif(IN_IMAGE, reason="repo docs ship at the host checkout, not in the image")
def test_docker_layer_exists():
    assert (REPO_ROOT / "docker" / "Dockerfile").exists()
    assert (REPO_ROOT / "docker" / "compose.yml").exists()
    assert (REPO_ROOT / "Makefile").exists()
    assert (REPO_ROOT / ".env.example").exists()


@pytest.mark.skipif(IN_IMAGE, reason="repo docs ship at the host checkout, not in the image")
def test_repo_documents_where_the_roadmap_lives():
    pointer = "docs/Roadmap"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")
    assert pointer in readme
    assert pointer in agents
    assert pointer in docs
