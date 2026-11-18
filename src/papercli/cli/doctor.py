"""``papers doctor`` preflight checks.

Checks (spec section 13), in order:
  1. config syntax        — the merged config loads and is well-formed.
  2. contact email        — contact.email is set and not the placeholder.
  3. provider credentials — per enabled provider, is the referenced env set?
  4. model endpoint       — reachability + minimal capability probe
                            (report-only when the key is missing).
  5. writable storage     — the storage dir can be created and written to.
  6. DB migration runner  — the SQLite migration framework applies cleanly.
  7. exporter deps        — which output formats' optional deps are present.
  8. redaction            — the whole serialized report is masked for secrets.

Doctor is a *report*, not a gate: it exits 0 whenever it runs to completion,
even when individual checks are warn/fail. That is what lets
``papers doctor --json`` run green on a fresh, unconfigured project. An
unexpected crash inside doctor still exits 1.

All output is masked with the process + .env secret redactor before it is
emitted, so no key or email value ever reaches stdout (spec section 16).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from papercli.cli.envelope import error, ok
from papercli.cli.errors import InternalError
from papercli.cli.redact import collect_process_secrets

__all__ = ["run_doctor", "CHECK_NAMES"]

CHECK_NAMES = [
    "config_syntax",
    "contact_email",
    "provider_credentials",
    "model_endpoint",
    "writable_storage",
    "db_migrations",
    "exporter_deps",
]

# Provider name -> credential env reference key, searched in the provider's
# config section. None means the provider needs no credential.
_PROVIDER_CRED_KEYS = ("api_key_env", "email_env", "key_env", "mailto_env")

# Output formats and the optional module needed to produce each (None = no dep).
_EXPORTER_DEPS: dict[str, str | None] = {
    "md": None,
    "bib": None,
    "csl": None,
    "csv": None,
    "pdf": "pypandoc",
    "docx": "docx",
}


def _resolve_credential(config: Any, env: dict[str, str]) -> tuple[str | None, bool]:
    """Return (env_var_name, is_present) for a provider credential reference."""
    for key in _PROVIDER_CRED_KEYS:
        ref = config.get(key)
        if isinstance(ref, str) and ref:
            present = bool((env.get(ref) or "").strip())
            return ref, present
    return None, True


def _check_config_syntax(cfg, redactor) -> dict[str, Any]:
    try:
        _ = cfg.data
        _ = cfg.sources
        detail = {"sources": list(cfg.sources), "top_sections": sorted(cfg.data.keys())}
        return {"name": "config_syntax", "status": "pass",
                "message": "Config loaded and merged.", "details": detail}
    except Exception as e:  # noqa: BLE001
        return {"name": "config_syntax", "status": "fail",
                "message": f"Config could not be loaded: {e}",
                "details": {"exception": type(e).__name__}}


def _check_contact_email(cfg, redactor) -> dict[str, Any]:
    email = cfg.get("contact.email") or ""
    placeholder = not email or "REPLACE" in email.upper()
    if placeholder:
        return {"name": "contact_email", "status": "warn",
                "message": "contact.email is not set (Unpaywall and the Crossref polite pool need a real address).",
                "details": {"configured": not placeholder and not _is_placeholder(email)}}
    return {"name": "contact_email", "status": "pass",
            "message": "contact.email is configured.",
            "details": {"configured": True}}


def _is_placeholder(v: str) -> bool:
    return (not v) or ("REPLACE" in v.upper()) or v.strip() in ("", "null")


def _check_provider_credentials(cfg, env) -> dict[str, Any]:
    providers = cfg.get("providers") or {}
    checks: dict[str, Any] = {}
    overall = "pass"
    for name, pdoc in providers.items():
        enabled = bool(pdoc.get("enabled", False)) if isinstance(pdoc, dict) else bool(pdoc)
        if not enabled:
            checks[name] = {"enabled": False, "status": "disabled",
                             "message": "Provider intentionally disabled."}
            continue
        env_ref, present = _resolve_credential(
            pdoc if isinstance(pdoc, dict) else {}, env
        )
        optional = name in ("openalex",)  # OpenAlex is usable keyless.
        if present:
            status = "pass"
            msg = "Credential present."
        elif optional:
            status = "warn"
            msg = f"Optional credential ({env_ref}) not set; provider works keyless with a smaller daily budget."
        else:
            status = "warn"
            msg = f"Required credential ({env_ref}) not set; provider will not authenticate."
            overall = "warn"
        checks[name] = {"enabled": True, "env_ref": env_ref, "present": present,
                        "status": status, "message": msg}
    if overall == "pass" and all(c.get("status") in ("pass", "disabled") for c in checks.values()):
        overall = "pass"
    return {"name": "provider_credentials", "status": overall,
            "message": "Provider credential readiness.", "details": checks}


def _check_model_endpoint(cfg, env, args) -> dict[str, Any]:
    base_url = (cfg.get("model.base_url") or "").strip()
    key_ref = cfg.get("model.api_key_env") or ""
    key = (env.get(key_ref) or "").strip() if key_ref else ""

    if _is_placeholder(base_url) or not base_url:
        return {"name": "model_endpoint", "status": "warn",
                "message": "Model endpoint is not configured (base_url is a placeholder).",
                "details": {"configured": False, "network_checked": False,
                            "key_present": bool(key), "key_env_ref": key_ref or None}}

    if not key:
        return {"name": "model_endpoint", "status": "warn",
                "message": f"Model endpoint configured but API key ({key_ref}) is missing; report-only, no capability probe run.",
                "details": {"configured": True, "network_checked": False,
                            "key_present": False, "report_only": True, "key_env_ref": key_ref}}

    if getattr(args, "skip_network", False):
        return {"name": "model_endpoint", "status": "warn",
                "message": f"Model endpoint configured; network check skipped (--skip-network).",
                "details": {"configured": True, "network_checked": False,
                            "key_present": True, "skipped": True, "key_env_ref": key_ref}}

    # Real reachability + minimal capability probe (bounded, short timeout).
    return _probe_model_endpoint(base_url, key, cfg)


def _probe_model_endpoint(base_url: str, key: str, cfg) -> dict[str, Any]:
    import httpx

    model = (cfg.get("model.model") or "").strip() or "probe"
    adapter = cfg.get("model.adapter") or "openai_compatible"
    timeout = float(cfg.get("execution.request_timeout_seconds") or 10)
    timeout = min(timeout, 10.0)  # keep the probe bounded.
    url = base_url.rstrip("/")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.post(f"{url}/chat/completions", json=payload, headers=headers)
        status = "pass" if 200 <= resp.status_code < 300 else "warn"
        return {"name": "model_endpoint", "status": status,
                "message": f"Model endpoint reachable (HTTP {resp.status_code}).",
                "details": {"configured": True, "network_checked": True,
                            "key_present": True, "http_status": resp.status_code,
                            "adapter": adapter}}
    except Exception as e:  # noqa: BLE001
        return {"name": "model_endpoint", "status": "warn",
                "message": f"Model endpoint probe failed: {type(e).__name__} ({e}).",
                "details": {"configured": True, "network_checked": True,
                            "key_present": True, "probe_error": type(e).__name__}}


def _check_writable_storage(args, cfg) -> dict[str, Any]:
    storage_dir = Path(args.storage_dir) if getattr(args, "storage_dir", None)         else Path.cwd() / ".papercli" / "storage"
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
        probe = storage_dir / ".papercli_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"name": "writable_storage", "status": "pass",
                "message": f"Storage dir is writable: {storage_dir}",
                "details": {"path": str(storage_dir), "writable": True}}
    except Exception as e:  # noqa: BLE001
        return {"name": "writable_storage", "status": "fail",
                "message": f"Storage dir is not writable: {e}",
                "details": {"path": str(storage_dir), "writable": False,
                            "exception": type(e).__name__}}


def _check_db_migrations(args) -> dict[str, Any]:
    from papercli.storage.migrations import apply_migrations, migration_count

    storage_dir = Path(args.storage_dir) if getattr(args, "storage_dir", None)         else Path.cwd() / ".papercli" / "storage"
    db_path = storage_dir / "papercli.db"
    total = migration_count()
    try:
        applied = apply_migrations(db_path)
        return {"name": "db_migrations", "status": "pass",
                "message": f"Applied {len(applied)} migration(s); {total} bundled total.",
                "details": {"db_path": str(db_path), "applied": [m.name for m in applied],
                            "bundled": total}}
    except Exception as e:  # noqa: BLE001
        return {"name": "db_migrations", "status": "fail",
                "message": f"Migration runner failed: {e}",
                "details": {"bundled": total, "exception": type(e).__name__}}


def _check_exporter_deps() -> dict[str, Any]:
    import importlib.util

    results: dict[str, Any] = {}
    overall = "pass"
    for fmt, module in _EXPORTER_DEPS.items():
        if module is None:
            results[fmt] = {"available": True, "module": None, "status": "pass"}
            continue
        available = importlib.util.find_spec(module) is not None
        results[fmt] = {"available": available, "module": module,
                        "status": "pass" if available else "warn"}
        if not available:
            overall = "warn"
    return {"name": "exporter_deps", "status": overall,
            "message": "Exporter dependency availability.",
            "details": results}


def run_doctor(args) -> dict[str, Any]:
    """Run all preflight checks and return the (masked) JSON envelope."""
    from papercli.config.loader import Config

    cfg = Config.load(cli_overrides={}, cwd=Path.cwd())
    env = {**cfg.env}  # .env values (names tracked; values stay local)
    # Also fold in a couple of PAPERS_* overrides for credential lookups.
    import os
    env.update({k: v for k, v in os.environ.items()
                if k in ("PAPERS_EMAIL", "OPENALEX_API_KEY", "PAPERS_LLM_API_KEY")
                and v})

    redactor = collect_process_secrets(extra=env)

    checks: list[dict[str, Any]] = [
        _check_config_syntax(cfg, redactor),
        _check_contact_email(cfg, redactor),
        _check_provider_credentials(cfg, env),
        _check_model_endpoint(cfg, env, args),
        _check_writable_storage(args, cfg),
        _check_db_migrations(args),
        _check_exporter_deps(),
    ]

    by_status = {"pass": 0, "warn": 0, "fail": 0}
    for c in checks:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1

    data = {
        "checks": checks,
        "summary": {
            "total": len(checks),
            "passed": by_status["pass"],
            "warnings": by_status["warn"],
            "failed": by_status["fail"],
            "healthy": by_status["fail"] == 0,
        },
        "schema_version": "1.0",
    }
    envelope = ok(data=data)

    # Redact the ENTIRE serialized envelope so no secret value leaks.
    masked = redactor.mask(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    # Re-emit through the standard emit path by returning the envelope; main()
    # will serialize. To guarantee redaction we replace the envelope's string
    # values via a masked re-parse.
    return json.loads(masked)
