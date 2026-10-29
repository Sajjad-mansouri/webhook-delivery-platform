import hashlib
import hmac
import time


def verify_webhook_signature(
    *,
    secret: str,
    timestamp_header: str,
    signature_header: str,
    body: bytes,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds cannot be negative.")

    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        return False

    current_time = int(time.time()) if now is None else now

    if abs(current_time - timestamp) > tolerance_seconds:
        return False

    if not signature_header.startswith("v1="):
        return False

    supplied_digest = signature_header[3:]

    if len(supplied_digest) != 64:
        return False

    signed_content = timestamp_header.encode("ascii") + b"." + body
    expected_digest = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_digest, supplied_digest)
