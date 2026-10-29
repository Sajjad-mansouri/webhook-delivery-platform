import hashlib
import hmac
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

SIGNATURE_HEADER = "X-HookPlatfom-Signature"
TIMESTAMP_HEADER = "X-HookPlatfom-Timestamp"
SIGNATURE_VERSION = "v1"


def generate_signing_secret() -> str:
    """Generate a high-entropy secret for one webhook endpoint."""
    return secrets.token_urlsafe(32)


def _get_fernet() -> Fernet:
    key = settings.webhook_encryption_key

    if key is None:
        raise RuntimeError("WEBHOOK_ENCRYPTION_KEY must be configured.")

    return Fernet(key.get_secret_value().encode("ascii"))


def encrypt_signing_secret(secret: str) -> str:
    encrypted = _get_fernet().encrypt(secret.encode("utf-8"))
    return encrypted.decode("ascii")


def decrypt_signing_secret(encrypted_secret: str) -> str:
    try:
        decrypted = _get_fernet().decrypt(encrypted_secret.encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ValueError("Unable to decrypt webhook signing secret.") from exc

    return decrypted.decode("utf-8")


def compute_signature(
    secret: str,
    timestamp: str,
    body: bytes,
) -> str:
    """
    Sign the timestamp and exact HTTP body bytes.

    The receiver must use the same byte sequence and format.
    """
    signed_content = timestamp.encode("ascii") + b"." + body

    digest = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()

    return f"{SIGNATURE_VERSION}={digest}"


def create_signature_headers(
    secret: str,
    body: bytes,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    if timestamp is None:
        timestamp = int(time.time())

    timestamp_text = str(timestamp)

    return {
        TIMESTAMP_HEADER: timestamp_text,
        SIGNATURE_HEADER: compute_signature(
            secret,
            timestamp_text,
            body,
        ),
    }


def verify_signature(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> bool:
    """Verify signature and reject timestamps outside the allowed window."""
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds cannot be negative.")

    if now is None:
        now = int(time.time())

    try:
        parsed_timestamp = int(timestamp)
    except (TypeError, ValueError):
        return False

    if abs(now - parsed_timestamp) > tolerance_seconds:
        return False

    expected = compute_signature(secret, timestamp, body)

    return hmac.compare_digest(expected, signature)
