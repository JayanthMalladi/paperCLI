"""Typed CLI failures mapped to the published exit-code table.

Exit codes (spec section 6):

    0   command succeeded
    1   unexpected/internal failure
    2   input/configuration/preflight failure
    3   reserved (removed legacy verification code; never repurposed silently)
    4   retrieval unavailable / no matching result
    5   network, access, or provider rate-limit error
    6   batch or foreground run completed partially
    7   run paused / limit reached
    130 interrupted (SIGINT)

Each exception carries the error ``code`` to place in the JSON envelope and
the process exit code. The CLI layer converts these to a single error
envelope on stdout and exits with the mapped code.
"""

from __future__ import annotations

__all__ = [
    "CliError",
    "ConfigError",
    "NotImplemented",
    "RetrievalUnavailable",
    "NetworkError",
    "RunPartial",
    "RunPaused",
    "InternalError",
    "EXIT_CODES",
]

EXIT_CODES: dict[int, str] = {
    0: "command succeeded",
    1: "unexpected/internal failure",
    2: "input/configuration/preflight failure",
    3: "reserved (removed legacy verification code)",
    4: "retrieval unavailable / no matching result",
    5: "network, access, or provider rate-limit error",
    6: "batch or foreground run completed partially",
    7: "run paused / limit reached",
    130: "interrupted",
}


class CliError(Exception):
    """Base class for all handled CLI failures."""

    code = "INTERNAL"
    exit_code = 1
    retryable = False

    def __init__(
        self,
        message: str,
        details: dict | None = None,
        *,
        code: str | None = None,
        exit_code: int | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code is not None:
            self.code = code
        if exit_code is not None:
            self.exit_code = exit_code
        if retryable is not None:
            self.retryable = retryable


class ConfigError(CliError):
    """Input/configuration/preflight failure (exit 2)."""

    code = "CONFIG_REQUIRED"
    exit_code = 2


class NotImplemented(CliError):  # noqa: A001 -- published contract name
    """A command surface that is not implemented in this build phase (exit 2)."""

    code = "NOT_IMPLEMENTED"
    exit_code = 2


class RetrievalUnavailable(CliError):
    """Retrieval unavailable or no matching result (exit 4)."""

    code = "RETRIEVAL_UNAVAILABLE"
    exit_code = 4


class NetworkError(CliError):
    """Network, access, or provider rate-limit error (exit 5)."""

    code = "NETWORK_ERROR"
    exit_code = 5
    retryable = True


class RunPartial(CliError):
    """Run completed partially (exit 6)."""

    code = "RUN_PARTIAL"
    exit_code = 6


class RunPaused(CliError):
    """Run paused or a limit was reached (exit 7)."""

    code = "RUN_PAUSED"
    exit_code = 7


class InternalError(CliError):
    """Unexpected/internal failure (exit 1)."""

    code = "INTERNAL_ERROR"
    exit_code = 1
