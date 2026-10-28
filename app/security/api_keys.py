import hashlib
import secrets

API_KEY_PREFIX = "wp_live_"


def generate_api_key() -> str:
    """Generate a new, high-entropy API key."""
    secret = secrets.token_urlsafe(32)
    return f"{API_KEY_PREFIX}{secret}"


def hash_api_key(api_key: str) -> str:
    """Return the SHA-256 hex digest of an API key."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
