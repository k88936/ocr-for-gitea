"""Typed application errors with stable process exit codes."""

from __future__ import annotations


class AppError(Exception):
    """Base class for all expected, user-facing failures."""

    exit_code = 1
    label = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def format(self) -> str:
        text = f"{self.label}: {self}"
        if self.hint:
            text += f"\nhint: {self.hint}"
        return text


class ConfigError(AppError):
    """Bad or missing configuration / credentials."""

    exit_code = 3
    label = "configuration error"


class GiteaError(AppError):
    """The Gitea API could not be reached or returned an error."""

    exit_code = 4
    label = "gitea error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status = status
        self.url = url


class GitError(AppError):
    """A local git operation failed."""

    exit_code = 5
    label = "git error"


class OcrError(AppError):
    """The OpenCodeReview CLI failed or produced unparsable output."""

    exit_code = 6
    label = "ocr error"
