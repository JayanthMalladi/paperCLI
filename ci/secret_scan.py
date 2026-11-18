"""CI secret scan: grep the committed tree and git history for key-shaped values.

Run by the "Fail on committed key patterns" step in
``.github/workflows/ci.yml``. Any match (or any git failure) exits
nonzero so the check can never be a silent no-op.

The patterns are generic credential *shapes*, not values from the
git-ignored local .env (real secret values are not committable):

  sk-[A-Za-z0-9_-]{16,}             OpenAI-style API keys
  xox[baprs]-[A-Za-z0-9-]{16,}      Slack bot/user/app/refresh tokens
  gh[pousr]_[A-Za-z0-9]{16,}        GitHub PAT / OAuth / server / refresh tokens
  OPENALEX_API_KEY=[A-Za-z0-9]{16,}    OpenAlex key committed as an env value
  PAPERS_LLM_API_KEY=[A-Za-z0-9]{16,}  LLM endpoint key committed as an env value

Usage:
    python ci/secret_scan.py [repo-root]

Exit codes: 0 clean, 1 match found or git error.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Generic credential shapes the repo must never contain (committable tree
#: or git history). The scan is a substring regex over text.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-skill", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{16,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}")),
    ("openalex-key", re.compile(r"OPENALEX_API_KEY=[A-Za-z0-9]{16,}")),
    ("llm-endpoint-key", re.compile(r"PAPERS_LLM_API_KEY=[A-Za-z0-9]{16,}")),
]


def _git(root: Path, *args: str) -> str:
    """Run git and return decoded stdout.

    Binary fixtures in history (PDFs, etc.) make ``git log -p`` emit
    non-UTF-8 bytes; read in binary and decode with ``replace`` so no byte
    sequence can crash the scan (mirrors the tree scan's decode below).
    """
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8", "replace")


def _scan_text(text: str, where: str, hits: list[str]) -> None:
    """Append matches to ``hits``; show only the match head, never a full value."""
    for label, rx in PATTERNS:
        for m in rx.finditer(text):
            head = m.group(0)[:6] + "..."
            hits.append(f"{where}: pattern {label!r} matched ({head})")


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192]


def scan_tree(root: Path, hits: list[str]) -> None:
    """Scan every tracked (committed) file in the working tree."""
    for rel in _git(root, "ls-files").splitlines():
        if not rel:
            continue
        path = root / rel
        try:
            blob = path.read_bytes()
        except OSError as e:
            hits.append(f"tree: cannot read {rel}: {e}")
            continue
        if _is_binary(blob):
            continue
        _scan_text(blob.decode("utf-8", "replace"), f"tree {rel}", hits)


def scan_history(root: Path, hits: list[str]) -> None:
    """Scan the full committed history (all refs) for introduced values."""
    _scan_text(_git(root, "log", "--all", "-p", "--no-color"), "history", hits)


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    if not (root / ".git").exists():
        print(f"error: {root} is not a git repository", file=sys.stderr)
        return 1
    hits: list[str] = []
    try:
        scan_tree(root, hits)
        scan_history(root, hits)
    except (RuntimeError, OSError) as e:
        print(f"error: secret scan could not complete: {e}", file=sys.stderr)
        return 1
    if hits:
        print("error: possible committed credential pattern detected:")
        for h in hits:
            print(f"  {h}")
        return 1
    print("secret scan clean: no key-shaped values in tree or history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
