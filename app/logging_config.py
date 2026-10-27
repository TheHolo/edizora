import logging
import re
from typing import ClassVar


class SecretFilter(logging.Filter):
    patterns: ClassVar[tuple[re.Pattern[str], ...]] = (
        re.compile(
            r"(?i)([\"']?(?:authorization|cookie|(?:[a-z0-9]+_)*(?:token|api_key|cookies)|password)"
            r"[\"']?\s*[:=]\s*)(?:bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)"
        ),
        re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"),
    )

    @classmethod
    def redact(cls, message: str) -> str:
        message = re.sub(r"(?im)(\b(?:cookie|pixiv_cookies)\s*[:=])[^\r\n]*", r"\1 [REDACTED]", message)
        message = cls.patterns[0].sub(r"\1[REDACTED]", message)
        message = cls.patterns[1].sub("[REDACTED]", message)
        return cls.patterns[2].sub(r"\1[REDACTED]@", message)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = self.redact(record.getMessage()), ()
        return True


class SecretFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Filters only see the message. Tracebacks are appended by the formatter.
        return SecretFilter.redact(super().format(record))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretFilter())
    handler.setFormatter(SecretFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
