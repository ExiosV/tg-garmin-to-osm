import re

BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

_EXTRA: list[str] = []


def add_secret(value: str) -> None:
    """Register a literal secret (Garmin password, OSM client secret, ...) to be stripped from log lines."""
    if value and len(value) >= 8 and str(value) not in _EXTRA:
        _EXTRA.append(str(value))


def redact(text) -> str:
    """Remove known secrets from a URL or an exception message before it is logged."""
    s = str(text)
    for secret in _EXTRA:
        s = s.replace(secret, "<redacted>")
    return BOT_TOKEN_RE.sub("bot<redacted>", s)
