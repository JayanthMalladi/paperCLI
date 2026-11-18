"""Secret-hygiene and storage tests.

Covers:
  - .gitignore covers ``.env`` (a hard constraint: local env files must
    never be committed).
  - Committed config template contains only env-var *references*, never
    literal secret values.
  - The SQLite migration runner applies and is idempotent.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# ---- .gitignore ---------------------------------------------------------

def test_gitignore_covers_dotenv():
    gitignore = Path(__file__).resolve().parents[1] / ".gitignore"
    assert gitignore.exists(), ".gitignore must exist at the repo root"
    lines = [l.strip() for l in gitignore.read_text(encoding="utf-8").splitlines()]
    # ".env" must be a tracked ignore pattern (exact or as a glob).
    assert ".env" in lines or ".env*" in lines or "/.env" in lines, (
        ".gitignore does not cover .env"
    )


def test_no_committed_env_file_in_repo():
    """A real ``.env`` with values must not be committed.

    The repo ships an *example* (``.env.example``-style content only inside
    the Python template string); it never ships a populated ``.env``.
    """
    repo = Path(__file__).resolve().parents[1]
    # A populated .env at the repo root would contain real values; assert
    # that if one exists it is git-ignored (it is, per the test above).
    env_path = repo / ".env"
    if env_path.exists():
        # It must be on the git-ignored list, not a tracked file.
        gitignore = (repo / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gitignore


def test_config_template_uses_env_references_not_values():
    from papercli.config.templates import CONFIG_TEMPLATE

    # The template must reference env var names, never embed literal secrets.
    assert "PAPERS_EMAIL" in CONFIG_TEMPLATE
    assert "OPENALEX_API_KEY" in CONFIG_TEMPLATE
    assert "PAPERS_LLM_API_KEY" in CONFIG_TEMPLATE
    # Placeholder contact email, not a real one.
    assert "REPLACE_WITH_REAL_EMAIL" in CONFIG_TEMPLATE
    # No plausible literal key material in the template.
    for token in ("sk-", "Bearer ", "sk_live", "xoxb-"):
        assert token not in CONFIG_TEMPLATE


# ---- SQLite migration runner ------------------------------------------

def test_migration_runner_applies_once():
    from papercli.storage.migrations import (
        MigrationRunner,
        apply_migrations,
        discover_migrations,
        migration_count,
    )

    assert migration_count() >= 1
    migs = discover_migrations()
    assert all(m.name.endswith(".sql") for m in migs)

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "papercli.db"
        applied = apply_migrations(db)
        assert len(applied) >= 1
        # idempotent: second run applies nothing new
        assert apply_migrations(db) == []
        runner = MigrationRunner(db)
        assert runner.pending() == []


def test_migrations_table_created():
    import sqlite3
    from papercli.storage.migrations import apply_migrations

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "db.sqlite3"
        apply_migrations(db)
        conn = sqlite3.connect(str(db))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "schema_migrations" in tables
            assert "runs" in tables
            assert "tasks" in tables
            assert "evidence" in tables
        finally:
            conn.close()
