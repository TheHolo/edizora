"""Shared API errors without importing the publication runtime."""


class PlatformError(Exception):
    def __init__(self, status, code, message, *, current_version=None, current=None, field_errors=None):
        super().__init__(message)
        self.status = self.status_code = status
        self.code, self.message = code, message
        self.current_version, self.current = current_version, current
        self.field_errors = field_errors
