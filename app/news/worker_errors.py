class HomeWorkerError(Exception):
    retryable = False


class VpsApiError(HomeWorkerError):
    pass


class TransientVpsApiError(VpsApiError):
    retryable = True


class VpsAuthenticationError(VpsApiError):
    pass


class LeaseLostError(VpsApiError):
    """The task was cancelled, reassigned, or its lease expired."""


class LlmError(HomeWorkerError):
    pass


class TransientLlmError(LlmError):
    retryable = True


class LlmOutputError(TransientLlmError):
    pass


class LlmOutputTruncatedError(LlmOutputError):
    """A complete response did not fit the model's output limit."""


class OllamaError(LlmError):
    pass


class TransientOllamaError(OllamaError):
    retryable = True


class OllamaOutputError(OllamaError):
    retryable = True


class SourceTooLargeError(OllamaError):
    pass
