"""Shared service transport error; independent of App execution clients."""

class RuntimeError(Exception):
    """Raised when the runtime call fails at the HTTP level."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"runtime {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
