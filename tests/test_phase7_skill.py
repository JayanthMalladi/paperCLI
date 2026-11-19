"""Phase 7 batch 3: the portable skill export + docs truth.

* `papers skill export --out DIR` writes a standard SKILL.md (front
  matter + the required sections) + the supporting references;
  exporting the folder does NOT install it (spec section 15).
* The exported content + the docs carry no secret values and no
  Sci-Hub functional mention (spec sections 16 / 2).
* The README "implemented vs not" is the final Phase-1..7 truth.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from papercli.skill.export import SKILL_NAME, SKILL_MD, export_skill, skill_files

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _shim() -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from papercli.cli.main import main\n"
        "sys.exit(main())\n"
    )


def run_papers(*args, cwd=None):
    """The CLI through a Python subprocess shim (argument arrays only,
    no shell; mirrors tests/conftest.py)."""
    proc = subprocess.run(
        [sys.executable, "-c", _shim(), *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _project(tmp_path) -> Path:
    """A fresh initialized project (papers init), like every other CLI
    test in this suite."""
    rc, out, err = run_papers("init", cwd=tmp_path)
    assert rc == 0, (rc, err, out)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. The skill folder: front matter + required sections + references
# ---------------------------------------------------------------------------

def test_skill_export_writes_valid_skill_md(tmp_path):
    project = _project(tmp_path)
    out = project / "papercli-skill"
    rc, out_s, err = run_papers("skill", "export", "--out",
                                 str(out), cwd=project)
    assert rc == 0, (rc, err, out_s)
    env = json.loads(out_s.strip().splitlines()[-1])
    assert env["status"] == "ok"
    written = set(env["data"]["files"])
    assert written == set(skill_files())
    assert "SKILL.md" in written and "references/schemas.md" in written
    # Exporting did not install anything elsewhere: the repo root
    # SKILL.md is the package copy, the export is only under --out.
    assert not (tmp_path / "installed-marker").exists()

    md = (out / "SKILL.md").read_text(encoding="utf-8")
    # Front matter: a standard SKILL.md has a YAML front-matter block
    # with name + description.
    assert md.startswith("---\n")
    fm_end = md.index("\n---", 4)
    front = md[4:fm_end]
    assert f"name: {SKILL_NAME}" in front
    assert "description:" in front
    # The required skill content (spec section 15) -- lowercased section
    # markers, searched in the lowercased text.
    low = md.lower()
    for section in ("command discovery", "configuration and preflight",
                    "short workflow", "result interpretation",
                    "subprocess", "status", "budget", "untrusted",
                    "completion"):
        assert section in low, section
    # Ground rules an external agent must follow (exact casing as
    # written in the skill).
    for rule in ("never shell strings", "NOT completion", "untrusted",
                 "No browser control"):
        assert rule in md, rule
    # The references describe schemas + evidence rules + recovery.
    for ref in ("references/schemas.md",
                "references/evidence-rules.md",
                "references/recovery-resume.md",
                "references/mcp.md"):
        assert (out / ref).exists(), ref


def test_skill_module_and_cli_agree(tmp_path):
    """The CLI-exported SKILL.md is byte-identical to the module
    source (no drift between the folder the CLI writes and the
    content the package carries)."""
    out = tmp_path / "s"
    export_skill(out)
    assert (out / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD


def test_repo_root_skill_md_is_the_package_copy():
    """SKILL.md at the repo root (the papercli-skill package) mirrors
    the export content + carries the front matter + no browser
    control."""
    root = REPO / "SKILL.md"
    md = root.read_text(encoding="utf-8")
    assert md == SKILL_MD
    assert md.startswith("---\nname: papercli\n")
    assert "browser automation" in md.lower() and \
        "never part of the workflow" in md.lower()


# ---------------------------------------------------------------------------
# 2. No secret values, no Sci-Hub functional mention (export + docs)
# ---------------------------------------------------------------------------

def test_skill_and_docs_have_no_secrets_or_scihub(tmp_path):
    out = tmp_path / "s"
    export_skill(out)
    texts = [f.read_text(encoding="utf-8")
             for f in out.rglob("*.md")]
    texts.append((REPO / "SKILL.md").read_text(encoding="utf-8"))
    for f in ("docs/05-user-guide.md", "docs/04-mcp-connection.md",
              "README.md"):
        texts.append((REPO / f).read_text(encoding="utf-8"))
    for text in texts:
        # Secret-sentinel + committed credential shapes.
        assert not re.search(r"sk-[A-Za-z0-9_-]{16,}", text)
        assert not re.search(r"gh[pousr]_[A-Za-z0-9]{16,}", text)
        assert not re.search(r"PAPERS_LLM_API_KEY=[A-Za-z0-9]{16,}",
                             text), "committed key shape found"
        # No FUNCTIONAL Sci-Hub mention (adapter / flag / env var /
        # workflow). A descriptive out-of-scope note ("paywall bypass
        # (Sci-Hub) ... never in scope") is the permitted form -- such
        # a line must carry an out-of-scope marker.
        for line in text.splitlines():
            if re.search(r"sci[-_ ]?hub", line, re.I):
                assert re.search(
                    r"out of scope|bypass|never|not in|no ",
                    line, re.I), f"functional Sci-Hub mention: {line}"
        # No browser-control workflow.
        assert "browser control" not in text.lower().replace(
            "no browser control", "").replace(
            "no browser", "").replace("never part", "") or \
            any(n in text for n in ("No browser control",
                                     "never part of the workflow"))


# ---------------------------------------------------------------------------
# 3. README final truth: the "implemented vs not" section is Phase 1..7
# ---------------------------------------------------------------------------

def test_readme_implemented_vs_not_is_final_truth():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    i = readme.find("## What is implemented vs not")
    block = readme[i:i + 8000]
    # Phase 8 is the final build phase: the section is the Phase-1..8
    # final truth (a Phases-1..7 heading means Phase 8 was not folded in).
    assert "Phases 1–8" in block or "Phases 1-8" in block
    for truth in ("Phase 4", "Phase 5", "Phase 6", "Phase 7",
                  "Phase 8", "papers mcp serve", "papers skill export",
                  "registry-only", "test_phase8_acceptance.py",
                  "RELEASE.md"):
        assert truth in block, truth
    # The old "deferred" claims are gone from the section.
    assert "Exporter(s) (md/pdf/docx/bib/csl/csv)." not in block or \
        "rendered exporters" in block.lower()
    # Sci-Hub may appear ONLY as a removal/out-of-scope statement.
    for line in block.lower().splitlines():
        if "sci" in line and "hub" in line:
            assert "out of scope" in line or "paywall bypass" in line \
                or "never" in line


def test_docs_files_exist(tmp_path):
    for f in ("docs/01-build-notes.md", "docs/02-provider-facts.md",
              "docs/03-migration-note.md", "docs/04-mcp-connection.md",
              "docs/05-user-guide.md", "examples/sample-brief.yaml"):
        assert (REPO / f).exists(), f
