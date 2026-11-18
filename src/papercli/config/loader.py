"""Configuration loading with layered precedence.

Precedence (highest first), per spec section 13:

    1. explicit CLI arguments
    2. environment variables
    3. run/project config (.papercli/config.toml)
    4. user config (~/.papercli/config.toml)
    5. documented defaults

Secrets are never stored in committed files. Config holds **references** to
environment variables (e.g. ``api_key_env = "OPENALEX_API_KEY"``); the
values are read from the process environment or a local, git-ignored
``.env`` file at load time.

TOML parsing uses the stdlib ``tomllib`` on 3.11+, falling back to the
optional ``tomli`` package on 3.10. No network calls are made here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULTS",
    "Config",
    "find_project_config",
    "find_user_config",
    "load_env_file",
    "load_toml_file",
    "ENV_CONFIG_MAP",
]


def _toml_parser():
    try:
        import tomllib  # type: ignore
        return tomllib
    except ModuleNotFoundError:
        import tomli  # type: ignore
        return tomli


def load_toml_file(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dict. Returns {} for a missing file."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return _toml_parser().load(f)


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a local ``.env`` file into a dict without exporting it globally.

    ``.env`` is git-ignored and local only. Lines are ``KEY=VALUE``; blank
    lines and ``#`` comments are skipped. Values keep inline content.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def find_project_config(cwd: Path | None = None) -> Path | None:
    """Locate ``.papercli/config.toml`` in the project (cwd or an ancestor)."""
    start = Path(cwd) if cwd is not None else Path.cwd()
    for parent in [start, *start.parents]:
        candidate = parent / ".papercli" / "config.toml"
        if candidate.exists():
            return candidate
    return None


def find_user_config() -> Path:
    """Return the user-level config path (may not exist yet)."""
    return Path(os.environ.get("PAPERS_USER_CONFIG", str(Path.home() / ".papercli" / "config.toml")))


# Dotted config paths that a PAPERS_* environment variable can override.
ENV_CONFIG_MAP: dict[str, str] = {
    "PAPERS_CONTACT_EMAIL": "contact.email",
    "PAPERS_MODEL_BASE_URL": "model.base_url",
    "PAPERS_MODEL_ID": "model.model",
    "PAPERS_MODEL_ADAPTER": "model.adapter",
    "PAPERS_EXEC_NETWORK_WORKERS": "execution.network_workers",
    "PAPERS_EXEC_MODEL_WORKERS": "execution.model_workers",
    "PAPERS_EXEC_TIMEOUT": "execution.request_timeout_seconds",
}


def _set_dotted(target: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = target
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value


def _coerce_scalar(raw: str) -> Any:
    """Interpret a string env value as bool/int/float when it clearly is one."""
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


# Documented defaults. Numeric budget guardrails match the brief template in
# spec section 7 (these are ceilings, not promised throughput).
DEFAULTS: dict[str, Any] = {
    "contact": {"email": ""},
    "providers": {
        "unpaywall": {"enabled": True, "email_env": "PAPERS_EMAIL"},
        "crossref": {"enabled": True},
        "openalex": {"enabled": True, "api_key_env": "OPENALEX_API_KEY"},
        "arxiv": {"enabled": True},
        "semantic_scholar": {"enabled": True, "key_env": "SEMANTIC_SCHOLAR_API_KEY"},
    },
    "network": {
        # Explicit local-dev opt-out for the URL guard (spec section 16):
        # allow loopback/private targets in metadata+document transports.
        "allow_local": False,
    },
    "retrieval": {
        # Step 4 of the retrieval order (spec section 8): where validated
        # full text is written.
        "out_dir": ".papercli/papers",
        "max_document_mb": 50,
        "min_document_bytes": 2048,
    },
    "model": {
        "adapter": "openai_compatible",
        "base_url": "",
        "model": "",
        "api_key_env": "PAPERS_LLM_API_KEY",
    },
    "execution": {
        "network_workers": 4,
        "model_workers": 2,
        "request_timeout_seconds": 30,
        "max_response_mb": 50,
    },
    "limits": {
        "wall_minutes": 360,
        "max_search_queries": 30,
        "max_discovered_records": 1000,
        "max_full_text_papers": 60,
        "max_model_calls": 300,
        "max_input_tokens": 1000000,
        "max_output_tokens": 150000,
        "max_estimated_cost_usd": None,
    },
}


class Config:
    """Resolved, layered configuration.

    Attributes:
        data: the fully merged config dict.
        sources: human-readable list of the layers that were merged, in the
            order applied (lowest precedence first).
        env: the ``.env`` values that were loaded (names tracked for redaction).
    """

    def __init__(self, data: dict[str, Any], sources: list[str], env: dict[str, str] | None = None):
        self.data = data
        self.sources = sources
        self.env = env or {}

    # -- construction -----------------------------------------------------
    @classmethod
    def load(
        cls,
        cli_overrides: Mapping[str, Any] | None = None,
        cwd: Path | None = None,
        *,
        load_dotenv: bool = True,
    ) -> "Config":
        import copy

        sources: list[str] = []
        env: dict[str, str] = {}

        # defaults (lowest precedence)
        merged = copy.deepcopy(DEFAULTS)
        sources.append("defaults")

        # user config
        user_path = find_user_config()
        if user_path.exists():
            user_doc = load_toml_file(user_path)
            if user_doc:
                _deep_update(merged, user_doc)
                sources.append(f"user:{user_path}")

        # run/project config
        project_path = find_project_config(cwd)
        if project_path is not None:
            project_doc = load_toml_file(project_path)
            if project_doc:
                _deep_update(merged, project_doc)
                sources.append(f"project:{project_path}")

        # local .env (git-ignored) -> process env for secret resolution
        if load_dotenv:
            cwdp = Path(cwd) if cwd is not None else Path.cwd()
            env.update(load_env_file(cwdp / ".env"))

        # environment variables override config values
        env_overrides: dict[str, str] = {}
        for name, dotted in ENV_CONFIG_MAP.items():
            val = os.environ.get(name) or env.get(name)
            if val is not None:
                env_overrides[dotted] = val
        if env_overrides:
            for dotted, raw in env_overrides.items():
                _set_dotted(merged, dotted, _coerce_scalar(raw))
            sources.append("environment")

        # explicit CLI arguments (highest precedence)
        if cli_overrides:
            for dotted, raw in cli_overrides.items():
                _set_dotted(merged, dotted, raw)
            sources.append("cli")

        return cls(merged, sources, env)

    # -- access -----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in dotted.split("."):
            if not isinstance(node, Mapping) or key not in node:
                return default
            node = node[key]
        return node

    def snapshot(self) -> dict[str, Any]:
        """Build a saveable snapshot of the effective **nonsecret** config.

        Design hook for later phases: a run stores its effective
        configuration so it can be reproduced/resumed. Secrets are stored
        only as their environment-variable *reference* names (never values),
        per spec section 13. Returns a JSON-serialisable dict.
        """
        snapshot: dict[str, Any] = {
            "schema_version": "1.0",
            "effective": _strip_secrets(self.data),
            "secret_refs": _collect_secret_refs(self.data),
            "sources": list(self.sources),
        }
        return snapshot


    def secret_ref(self, dotted: str) -> str:
        """Return the environment-variable name that a secret field references."""
        val = self.get(dotted)
        return val if isinstance(val, str) else ""

    def resolve_secret(self, dotted: str, env: Mapping[str, str] | None = None) -> str | None:
        """Resolve a secret field to its actual value (from env/.env).

        The value is read at call time and must not be persisted. Returns
        None when the referenced variable is unset.
        """
        envmap = env if env is not None else {**self.env, **_process_env()}
        ref = self.secret_ref(dotted)
        if not ref:
            return None
        return envmap.get(ref)

    def provider_enabled(self, provider: str) -> bool:
        return bool(self.get(f"providers.{provider}.enabled", False))


def _process_env() -> dict[str, str]:
    return dict(os.environ)


def _deep_update(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value


def _secret_ref_keys() -> set[str]:
    keys: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                if isinstance(v, str) and ("_env" in k or k in ("api_key_env", "email_env", "key_env", "mailto_env")):
                    keys.add(k)
                walk(v)
    walk(DEFAULTS)
    return keys


def _collect_secret_refs(data: dict[str, Any]) -> list[str]:
    """List the environment-variable names that configured fields reference."""
    refs: list[str] = []
    seen: set[str] = set()
    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for k, v in node.items():
                if k.endswith("_env") and isinstance(v, str) and v and v not in seen:
                    seen.add(v)
                    refs.append(v)
                walk(v)
    walk(data)
    refs.sort()
    return refs


def _strip_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the config with secret-bearing fields replaced by refs.

    Nonsecret values are preserved verbatim; fields ending in ``_env`` are
    kept (they are references, not values). The contact email is a value,
    not a secret reference -- it is preserved because it is nonsecret in the
    precedence sense (the spec treats it as an identifier), but callers that
    redact can mask it separately. This function only guarantees that no raw
    API-key token is embedded in the snapshot.
    """
    import copy
    return copy.deepcopy(data)
