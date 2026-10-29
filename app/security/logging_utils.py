import re

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-hookrelay-signature",
}

SENSITIVE_FIELD_NAMES = {
    "password",
    "secret",
    "signing_secret",
    "signing_secret_encrypted",
    "api_key",
    "token",
    "access_token",
    "refresh_token",
    "webhook_encryption_key",
}


def redact_header(name: str, value: str) -> str:
    """Return a safe replacement for a sensitive header value."""
    if name.lower() in SENSITIVE_HEADER_NAMES:
        return "[REDACTED]"

    return value


def redact_text(value: str) -> str:
    """Best-effort redaction for common credential patterns in text."""
    patterns = (
        r"(?i)(x-api-key\s*[:=]\s*)[^\s,;]+",
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"(?i)(password|secret|token)\s*[:=]\s*[^\s,;]+",
    )

    for pattern in patterns:
        value = re.sub(pattern, r"\1[REDACTED]", value)

    return value
