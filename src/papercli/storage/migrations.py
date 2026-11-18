"""Minimal SQLite migration runner.

Later phases plug in their migrations by adding numbered ``NNNN.sql`` files
to ``papercli/storage/migrations/``. The runner tracks applied migrations in
a ``schema_migrations`` table and applies unapplied ones in order inside a
transaction.

Windows-friendly: uses only ``sqlite3`` and ``pathlib``. The database file
path is always an absolute path; no POSIX-only calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

__all__ = [
    "MIGRATIONS_DIR",
    "Migration",
    "MigrationRunner",
    "discover_migrations",
    "apply_migrations",
    "migration_count",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Return numbered ``NNNN.sql`` migrations in ascending order."""
    d = directory or MIGRATIONS_DIR
    found: list[Migration] = []
    if not d.exists():
        return found
    for p in sorted(d.glob("[0-9][0-9][0-9][0-9]*.sql")):
        try:
            version = int(p.name[:4])
        except ValueError:
            continue
        found.append(Migration(version=version, name=p.name, path=p))
    found.sort(key=lambda m: m.version)
    return found


class MigrationRunner:
    """Applies pending migrations to a SQLite database."""

    def __init__(self, db_path: Path | str, migrations: Iterable[Migration] | None = None):
        self.db_path = Path(db_path)
        self.migrations = list(migrations) if migrations is not None else discover_migrations()

    def applied(self) -> set[int]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY NOT NULL,"
                " name TEXT NOT NULL,"
                " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
            return {int(r[0]) for r in rows}
        finally:
            conn.close()

    def pending(self) -> list[Migration]:
        done = self.applied()
        return [m for m in self.migrations if m.version not in done]

    def run(self) -> list[Migration]:
        """Apply all pending migrations. Returns the list just applied."""
        pending = self.pending()
        if not pending:
            return []
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            for m in pending:
                sql = m.path.read_text(encoding="utf-8")
                with conn:  # one transaction per migration
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                        (m.version, m.name),
                    )
            return pending
        finally:
            conn.close()


def apply_migrations(db_path: Path | str) -> list[Migration]:
    """One-shot convenience: apply all pending migrations to ``db_path``."""
    return MigrationRunner(db_path).run()


def migration_count() -> int:
    """Total number of migrations shipped with the package."""
    return len(discover_migrations())
