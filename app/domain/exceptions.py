class ApplicationError(Exception):
    code = "application_error"
    retryable = False


class VersionConflictError(ApplicationError):
    code = "version_conflict"


class ProviderError(ApplicationError):
    code = "provider_error"


class UnsupportedSourceError(ProviderError):
    code = "unsupported_source"


class InvalidUrlError(ProviderError):
    code = "invalid_url"


class SourceNotFoundError(ProviderError):
    code = "source_not_found"


class SourceAccessDeniedError(ProviderError):
    code = "source_access_denied"


class SourceRateLimitedError(ProviderError):
    code = "source_rate_limited"
    retryable = True


class DownloadError(ApplicationError):
    code = "download_error"
    retryable = True


class MediaValidationError(ApplicationError):
    code = "media_validation"


class MediaTooLargeError(MediaValidationError):
    code = "media_too_large"


class TooManyMediaError(MediaValidationError):
    code = "too_many_media"


class InvalidMediaSelectionError(MediaValidationError):
    code = "invalid_media_selection"


class PublishError(ApplicationError):
    code = "publish_error"


class UncertainPublishError(PublishError):
    code = "uncertain_publish"


class ChannelPermissionError(PublishError):
    code = "channel_permission"
    retryable = False


class DuplicatePublicationError(ApplicationError):
    code = "duplicate_publication"
