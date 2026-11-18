"""Config precedence and loader tests.

Precedence (highest first): CLI args > environment > project config >
user config > defaults. These tests isolate each layer and assert the
merge order.
"""

from __future__ import annotations
import json
import os
from pathlib import Path

from papercli.config.loader import (
    Config,
    DEFAULTS,
    ENV_CONFIG_MAP,
    find_project_config,
    load_env_file,
    load_toml_file,
)


def test_defaults_present():
    # Budget guardrails from spec sec.7 must be the documented defaults.
    lim = DEFAULTS["limits"]
    assert lim["wall_minutes"] == 360
    assert lim["max_search_queries"] == 30
    assert lim["max_discovered_records"] == 1000
    assert lim["max_full_text_papers"] == 60
    assert lim["max_model_calls"] == 300
    assert lim["max_input_tokens"] == 1000000
    assert lim["max_output_tokens"] == 150000
    assert lim["max_estimated_cost_usd"] is None


def test_env_file_parse(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "PAPERS_EMAIL=a@b.c\n"
        "OPENALEX_API_KEY=key123\n"
        "PAPERS_LLM_BASE_URL=https://x.example\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env_file)
    assert loaded["PAPERS_EMAIL"] == "a@b.c"
    assert loaded["OPENALEX_API_KEY"] == "key123"
    assert loaded["PAPERS_LLM_BASE_URL"] == "https://x.example"
    # comments/blank lines are not keys
    assert "#" not in loaded


def test_env_file_missing_returns_empty(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == {}


def test_project_config_precedence(tmp_path, monkeypatch):
    # Project config sets a value; environment must override it.
    pdir = tmp_path / ".papercli"
    pdir.mkdir()
    (pdir / "config.toml").write_text(
        "[model]\nbase_url = \"https://project.example\"\n"
        "model = \"project-model\"\n",
        encoding="utf-8",
    )
    # No user config interference.
    monkeypatch.setenv("PAPERS_USER_CONFIG", str(tmp_path / "does-not-exist.toml"))
    monkeypatch.delenv("PAPERS_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("PAPERS_MODEL_ID", raising=False)

    cfg = Config.load(cli_overrides={}, cwd=tmp_path, load_dotenv=False)
    assert cfg.get("model.base_url") == "https://project.example"

    # Now an environment variable must beat the project config.
    monkeypatch.setenv("PAPERS_MODEL_BASE_URL", "https://env.example")
    cfg2 = Config.load(cli_overrides={}, cwd=tmp_path, load_dotenv=False)
    assert cfg2.get("model.base_url") == "https://env.example"


def test_cli_overrides_beat_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERS_USER_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("PAPERS_MODEL_ID", "env-model")
    cfg = Config.load(cli_overrides={"model.model": "cli-model"},
                      cwd=tmp_path, load_dotenv=False)
    assert cfg.get("model.model") == "cli-model"
    # The env layer was also present; CLI still wins.
    assert "environment" in cfg.sources or "cli" in cfg.sources


def test_user_config_below_project(tmp_path, monkeypatch):
    # user config lower than project config.
    user = tmp_path / "user.toml"
    user.write_text('[execution]\nnetwork_workers = 99\n', encoding="utf-8")
    monkeypatch.setenv("PAPERS_USER_CONFIG", str(user))

    pdir = tmp_path / ".papercli"
    pdir.mkdir()
    (pdir / "config.toml").write_text(
        "[execution]\nnetwork_workers = 7\n", encoding="utf-8",
    )
    cfg = Config.load(cli_overrides={}, cwd=tmp_path, load_dotenv=False)
    # project (7) beats user (99).
    assert cfg.get("execution.network_workers") == 7


def test_find_project_config_ancestor(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    found = find_project_config(nested)
    assert found is None
    (tmp_path / ".papercli").mkdir()
    (tmp_path / ".papercli" / "config.toml").write_text("{}", encoding="utf-8")
    found = find_project_config(nested)
    assert found is not None
    assert found == tmp_path / ".papercli" / "config.toml"


def test_env_map_covers_documented_vars():
    # The documented PAPERS_* override variables must be mapped.
    assert "PAPERS_CONTACT_EMAIL" in ENV_CONFIG_MAP
    assert "PAPERS_MODEL_BASE_URL" in ENV_CONFIG_MAP
    assert ENV_CONFIG_MAP["PAPERS_CONTACT_EMAIL"] == "contact.email"
    assert ENV_CONFIG_MAP["PAPERS_MODEL_BASE_URL"] == "model.base_url"


def test_secret_resolution(tmp_path, monkeypatch):
    pdir = tmp_path / ".papercli"
    pdir.mkdir()
    (pdir / "config.toml").write_text(
        "[providers.openalex]\nenabled = true\napi_key_env = \"OPENALEX_API_KEY\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PAPERS_USER_CONFIG", str(tmp_path / "nope.toml"))
    monkeypatch.setenv("OPENALEX_API_KEY", "realkey-abc")
    cfg = Config.load(cli_overrides={}, cwd=tmp_path, load_dotenv=False)
    assert cfg.provider_enabled("openalex") is True
    assert cfg.resolve_secret("providers.openalex.api_key_env") == "realkey-abc"
    # snapshot must reference the variable, not embed the value
    snap = cfg.snapshot()
    assert "OPENALEX_API_KEY" in snap["secret_refs"]
    assert "realkey-abc" not in json.dumps(snap)

