# frozen error taxonomy (plan §4). Implementations grow; names do not.
class ConsoleError(Exception):
    """Base for all console application errors."""


class ValidationError(ConsoleError):
    """Form/input validation failed. Surface: 422 + inline errors + summary."""

    def __init__(self, errors: dict[str, str] | None = None, summary: str = ""):
        self.errors = errors or {}
        self.summary = summary
        super().__init__(summary or "validation failed")


class ConflictError(ConsoleError):
    """Stale edit (row content changed since load). Surface: 409 + banner."""


class KamailioUnavailable(ConsoleError):
    """RPC socket/timeout/fault. Surface: pending-reload intent + banner."""


class SidecarUnavailable(ConsoleError):
    """console.sqlite (sidecar) unreadable. Surface: destructive ops refused."""


class DBBusy(ConsoleError):
    """SQLITE_BUSY after busy_timeout. Surface: retry indicator."""


class ReloadError(ConsoleError):
    """A reload RPC returned a fault. Surface: toast naming table + verbatim message."""

    def __init__(self, table: str, message: str):
        self.table = table
        self.message = message
        super().__init__(f"reload of {table} failed: {message}")
