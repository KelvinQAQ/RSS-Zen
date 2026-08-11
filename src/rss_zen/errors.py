"""Application errors with safe messages and retry metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class AppError(Exception):
    """An error suitable for both CLI output and structured logs."""

    code: str
    message: str
    retryable: bool = False
    cause: Exception | None = None

    def __str__(self) -> str:
        return self.message


class ConfigurationError(AppError):
    """Configuration cannot be parsed, validated, or resolved."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__("invalid_configuration", message, retryable=False, cause=cause)
