from app.application.errors import PlatformError

__all__ = ["PlatformService", "PlatformError"]


def __getattr__(name):
    if name == 'PlatformService':
        from app.application.platform import PlatformService
        return PlatformService
    raise AttributeError(name)
