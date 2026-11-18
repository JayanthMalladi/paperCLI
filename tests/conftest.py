"""Shared pytest fixtures for the PaperCLI test suite.

The CLI is exercised through the Python interpreter (``python -c`` with a
small shim that imports ``papercli.cli.main``) so the suite runs without a
separately installed console script. Every subprocess call uses an argument
array (no shell) -- Windows-safe, no shlex, no shell interpolation.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _shim() -> str:
    """A tiny entry script that launches ``main()`` in a child interpreter."""
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from papercli.cli.main import main\n"
        "sys.exit(main())\n"
    )


def run_papers(*args, cwd=None, env_extra=None, env_strip=None):
    """Run the ``papers`` CLI; return ``(returncode, stdout, stderr)``.

    ``env_strip`` is a set of variable names to blank out in the child
    environment (used to guarantee no real secret leaks into tests).
    """
    argv = [sys.executable, "-c", _shim(), *args]
    env = os.environ.copy()
    if env_strip:
        for name in env_strip:
            env[name] = ""
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


@pytest.fixture()
def clean_env():
    """Names of secret variables to blank in child processes."""
    return {
        "PAPERS_EMAIL",
        "OPENALEX_API_KEY",
        "PAPERS_LLM_API_KEY",
        "PAPERS_LLM_BASE_URL",
    }


@pytest.fixture()
def tmp_project(tmp_path):
    """A fresh project dir with ``.papercli`` initialized via the CLI."""
    rc, out, err = run_papers("init", "--env", cwd=tmp_path)
    assert rc == 0, f"papers init failed: {err}\n{out}"
    return tmp_path
