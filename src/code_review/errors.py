class ReviewBackendError(Exception):
    """A backend failed to produce findings."""

    def __init__(
        self, message: str, *, retryable: bool = False, usage_limited: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.usage_limited = usage_limited
