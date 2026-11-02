"""Payload redaction and scoped suppression of sensitive OAuth SDK diagnostics."""

import logging
import re
from contextlib import contextmanager
from threading import RLock
from typing import Any

_DIAGNOSTIC_LOCK = RLock()


@contextmanager
def suppress_oauth_diagnostics(prefixes: tuple[str, ...], provider: str):
    """Suppress sensitive SDK records during OAuth, preserving unrelated logging."""
    with _DIAGNOSTIC_LOCK:
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):
            record = previous(*args, **kwargs)
            if any(record.name == p or record.name.startswith(p + ".") for p in prefixes):
                record.msg = f"{provider} OAuth SDK diagnostic suppressed"
                record.args = ()
                record.exc_info = record.exc_text = record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)
        try:
            yield
        finally:
            logging.setLogRecordFactory(previous)


PATTERNS = [
    re.compile(r"(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]+)"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:xox[baprs]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{16,})\b"),
    re.compile(r"(?i)\bBearer\s+[^\s\"']+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S),
    re.compile(r"""(?ix)(?<![\w]) ["']?
        (password|api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|aws_secret_access_key)
        ["']? \s*[:=]\s* (?: "(?:\\.|[^"\\])*" | '(?:\\.|[^'\\])*' | [^\s,;]+ )"""),
    re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@"),
]
SECRET_KEYS = {"password", "api_key", "client_secret", "access_token", "refresh_token"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in SECRET_KEYS else redact(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = pattern.sub("[REDACTED]", value)
    return value
