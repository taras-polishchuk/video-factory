"""Error types. Hierarchy: VideoFactoryError is the root."""


class VideoFactoryError(Exception):
    """Base. All custom errors derive from this."""


class InvalidStateTransition(VideoFactoryError):
    """Raised when a state machine transition is not allowed."""


class ProviderDisabled(VideoFactoryError):
    """Live provider path attempted without LIVE_PROVIDER_TESTS + budget cap."""


class ProviderError(VideoFactoryError):
    """Provider returned a non-retryable failure."""


class BudgetExceeded(VideoFactoryError):
    """Estimated or actual cost exceeds the configured ceiling."""


class TeardownUnsupported(VideoFactoryError):
    """Renderer cannot drive teardown itself; manual gate required."""