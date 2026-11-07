"""Domain errors carry a remediation code, never a guessed value."""

from __future__ import annotations


class DomainError(ValueError):
    def __init__(self, code: str, message: str, remediation: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation

    def to_dict(self) -> dict[str, str]:
        payload = {"code": self.code, "message": self.message}
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload
