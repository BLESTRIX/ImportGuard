class Problem(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class LostLease(Exception):
    """The worker no longer owns this job; it must stop without updating it."""


class InjectedFailure(Exception):
    """Controlled development/test failure, never enabled in production."""
