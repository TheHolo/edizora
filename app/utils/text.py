import re
from html import unescape
from html.parser import HTMLParser

_WHITESPACE = re.compile(r"\s+")
_PROVIDER_LABELS = {"deviantart": "DeviantArt", "pixiv": "Pixiv"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li"}:
            self.parts.append(" ")


class _TelegramTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def telegram_html_length(value: str) -> int:
    """Count visible text after parsing entities, preserving whitespace and newlines."""
    parser = _TelegramTextExtractor()
    parser.feed(value)
    parser.close()
    return len("".join(parser.parts))


def plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return _WHITESPACE.sub(" ", unescape("".join(parser.parts))).strip()


def shorten(value: str, limit: int = 240) -> str:
    value = plain_text(value)
    if limit <= 1:
        return ""
    if len(value) <= limit:
        return value
    cut = limit - 1
    candidate = value[:cut].rstrip()
    if cut < len(value) and not value[cut].isspace() and " " in candidate:
        candidate = candidate.rsplit(" ", 1)[0]
    return candidate.rstrip(".,;:! ") + "…"


def provider_label(value: str) -> str:
    return _PROVIDER_LABELS.get(value.casefold(), value.title())
