"""Doctor/config redaction tests.

Spec section 16: no key or email value may appear in logs, errors, JSON,
or any other output. These tests set a recognizable test secret, run the
CLI, and assert the value never appears in stdout or stderr.
"""

from __future__ import annotations

import json

from tests.conftest import run_papers

# A value that is unmistakable if it leaks. It is NOT a real credential.
TEST_SECRET_XYZ = "TEST_SECRET_XYZ_VALUE_DO_NOT_LEAK"
TEST_SECRET_EMAIL = "test.secret.xyz@leak.example.org"


def _assert_no_leak(stdout: str, stderr: str, *forbidden: str) -> None:
    blob = stdout + stderr
    for value in forbidden:
        assert value not in blob, f"secret value leaked into output: {value!r}"


def test_doctor_redacts_test_secret(tmp_project):
    """Set test secrets in env + .env; doctor must not echo either."""
    # Write the test secret into the project .env so doctor's env merge sees it.
    (tmp_project / ".env").write_text(
        f"PAPERS_LLM_API_KEY={TEST_SECRET_XYZ}\n"
        f"PAPERS_EMAIL={TEST_SECRET_EMAIL}\n",
        encoding="utf-8",
    )
    env_extra = {"PAPERS_LLM_API_KEY": TEST_SECRET_XYZ,
                 "PAPERS_EMAIL": TEST_SECRET_EMAIL,
                 "PAPERS_LLM_BASE_URL": "https://example.endpoint"}
    rc, out, err = run_papers(
        "doctor", "--json", "--skip-network",
        cwd=tmp_project, env_extra=env_extra,
    )
    assert rc == 0
    env = json.loads(out)
    assert env["status"] == "ok"
    _assert_no_leak(out, err, TEST_SECRET_XYZ, TEST_SECRET_EMAIL)
    # The masked placeholder should appear where the key was referenced.
    assert "PAPERS_LLM_API_KEY" in out  # the *name* is allowed to appear
    # The raw value must not be anywhere in the serialized envelope.
    assert TEST_SECRET_XYZ not in out


def test_config_show_redacts_secrets(tmp_project):
    (tmp_project / ".env").write_text(
        f"PAPERS_LLM_API_KEY={TEST_SECRET_XYZ}\n",
        encoding="utf-8",
    )
    env_extra = {"PAPERS_LLM_API_KEY": TEST_SECRET_XYZ}
    rc, out, err = run_papers(
        "config", "show", "--redact",
        cwd=tmp_project, env_extra=env_extra,
    )
    assert rc == 0
    _assert_no_leak(out, err, TEST_SECRET_XYZ)
    # Redaction flag is reported.
    env = json.loads(out)
    assert env["data"]["redacted"] is True


def test_doctor_masking_redactor_unit():
    from papercli.cli.redact import MaskingRedactor, collect_process_secrets

    red = MaskingRedactor(["TEST_SECRET_XYZ_VALUE_DO_NOT_LEAK", "abc123def456"])
    assert red.mask("key=TEST_SECRET_XYZ_VALUE_DO_NOT_LEAK") == "key=***"
    assert "TEST_SECRET_XYZ_VALUE_DO_NOT_LEAK" not in red.mask(
        "the secret is TEST_SECRET_XYZ_VALUE_DO_NOT_LEAK end"
    )
    # A value that is not tracked passes through untouched.
    assert red.mask("hello world") == "hello world"


def test_collect_process_secrets_tracks_cred_vars(monkeypatch):
    import os
    from papercli.cli.redact import collect_process_secrets

    os.environ["PAPERS_TEST_TOKEN"] = "SECRET_TOKEN_A1B2C3"
    try:
        red = collect_process_secrets()
        assert "SECRET_TOKEN_A1B2C3" not in red.mask("SECRET_TOKEN_A1B2C3")
    finally:
        del os.environ["PAPERS_TEST_TOKEN"]
