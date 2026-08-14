from __future__ import annotations


class ApiError(Exception):
    """An error safe to return to the browser."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.public_message = message


class FileRejected(Exception):
    """A file failed a declared-type, signature, MIME, or parser check."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.public_message = message


class ExtractionFailed(Exception):
    """A validated file could not be extracted or OCRed."""


class MaasUnavailable(Exception):
    """The OpenAI-compatible MaaS request failed."""


class StructuredOutputFailed(Exception):
    """MaaS repeatedly returned output that failed the strict schema."""

