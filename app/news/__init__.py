"""Extraction primitives for text-first news publications."""

from importlib import import_module

__all__ = [
    "ExtractedNewsSource",
    "ExtractionProgress",
    "ExtractionStage",
    "ForwardedTelegramPost",
    "NewsMedia",
    "NewsMediaKind",
    "NewsSourceFacade",
    "NewsSourceKind",
    "NewsSourceRequest",
    "PublicOnlyResolver",
    "classify_news_input",
]

_EXPORTS = {
    'classify_news_input': 'classifier',
    'NewsSourceFacade': 'facade',
    'PublicOnlyResolver': 'http',
    'ForwardedTelegramPost': 'telegram',
    **{name: 'models' for name in (
        'ExtractedNewsSource', 'ExtractionProgress', 'ExtractionStage', 'NewsMedia',
        'NewsMediaKind', 'NewsSourceKind', 'NewsSourceRequest',
    )},
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(name)
    value = getattr(import_module(f'{__name__}.{module}'), name)
    globals()[name] = value
    return value
