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

# A secret value containing JSON-escape characters. It is NOT a real
# credential. Its raw form AND its JSON-escaped rendering must both be
# absent from doctor output (defect D4: masking happens per string value,
# before serialization, not on the serialized text).
ESCAPED_SECRET = 'PROBE"\\x"ESCAPED_SECRET_VALUE_9f3k'


def test_doctor_redacts_escaped_secret(tmp_project):
    (tmp_project / ".env").write_text(
        f"PAPERS_LLM_API_KEY={ESCAPED_SECRET}\n",
        encoding="utf-8",
    )
    rc, out, err = run_papers(
        "doctor", "--json", "--skip-network",
        cwd=tmp_project,
        env_extra={"PAPERS_LLM_API_KEY": ESCAPED_SECRET},
    )
    assert rc == 0
    blob = out + err
    # The raw value must not leak.
    assert ESCAPED_SECRET not in blob
    # And neither may its JSON-escaped rendering (the D4 failure mode).
    import json as _json
    assert _json.dumps(ESCAPED_SECRET) not in blob
    assert _json.dumps(ESCAPED_SECRET)[1:-1] not in blob


def test_redact_structure_masks_nested_strings():
    from papercli.cli.redact import MaskingRedactor, redact_structure

    red = MaskingRedactor([ESCAPED_SECRET, "plain_secret_abc"])
    structure = {
        "checks": [{"message": f"key={ESCAPED_SECRET}", "path": "/x"}],
        "list": [ESCAPED_SECRET, 7, {"inner": "plain_secret_abc"}],
        "null": None,
    }
    masked = redact_structure(structure, red)
    import json as _json
    serialized = _json.dumps(masked)
    assert ESCAPED_SECRET not in serialized
    assert _json.dumps(ESCAPED_SECRET) not in serialized
    assert "plain_secret_abc" not in serialized
    # Non-string values and keys are preserved.
    assert masked["checks"][0]["path"] == "/x"
    assert masked["list"][1] == 7
    assert masked["null"] is None
