import logging

from app.logging_config import SecretFilter, SecretFormatter


def test_secrets_are_redacted():
    record = logging.LogRecord(
        "x",
        logging.INFO,
        "",
        0,
        (
            'token=abc Authorization: Bearer api-secret '
            '"lease_token": "lease-secret" cookie=session-secret '
            "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI"
        ),
        (),
        None,
    )
    assert SecretFilter().filter(record)
    assert "abc" not in record.getMessage()
    assert "api-secret" not in record.getMessage()
    assert "lease-secret" not in record.getMessage()
    assert "session-secret" not in record.getMessage()
    assert "123456:" not in record.getMessage()


def test_traceback_and_lmstudio_credentials_are_redacted():
    error = RuntimeError("HOME_WORKER_LMSTUDIO_API_KEY=private-key https://user:password-value@host")
    record = logging.LogRecord("x", logging.ERROR, "", 0, "request failed", (), (RuntimeError, error, None))
    formatted = SecretFormatter("%(message)s").format(record)
    assert "private-key" not in formatted
    assert "password-value" not in formatted
    assert "[REDACTED]" in formatted
