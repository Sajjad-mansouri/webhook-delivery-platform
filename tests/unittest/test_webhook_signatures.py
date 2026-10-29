import hashlib
import hmac

import pytest
from app.security.webhook_signatures import (
    compute_signature,
    create_signature_headers,
    decrypt_signing_secret,
    encrypt_signing_secret,
    verify_signature,
)


class TestWebhookSignatures:
    def test_signature_matches_standard_hmac_sha256(self):
        secret = "test-signing-secret"
        timestamp = "1790000000"
        body = b'{"id":"evt-123","amount":2500}'

        signed_content = timestamp.encode("ascii") + b"." + body
        expected_digest = hmac.new(
            secret.encode("utf-8"),
            signed_content,
            hashlib.sha256,
        ).hexdigest()

        assert (
            compute_signature(
                secret,
                timestamp,
                body,
            )
            == f"v1={expected_digest}"
        )

    def test_valid_signature_verifies(self):
        secret = "test-signing-secret"
        timestamp = "1790000000"
        body = b'{"id":"evt-123"}'

        signature = compute_signature(secret, timestamp, body)

        assert verify_signature(
            secret,
            timestamp,
            body,
            signature,
            now=1790000000,
        )

    def test_modified_body_fails_verification(self):
        secret = "test-signing-secret"
        timestamp = "1790000000"
        body = b'{"amount":2500}'

        signature = compute_signature(secret, timestamp, body)

        assert not verify_signature(
            secret,
            timestamp,
            b'{"amount":9999}',
            signature,
            now=1790000000,
        )

    def test_wrong_secret_fails_verification(self):
        timestamp = "1790000000"
        body = b'{"id":"evt-123"}'
        signature = compute_signature(
            "correct-secret",
            timestamp,
            body,
        )

        assert not verify_signature(
            "different-secret",
            timestamp,
            body,
            signature,
            now=1790000000,
        )

    def test_expired_timestamp_fails_verification(self):
        timestamp = "1790000000"
        body = b'{"id":"evt-123"}'
        signature = compute_signature(
            "test-secret",
            timestamp,
            body,
        )

        assert not verify_signature(
            "test-secret",
            timestamp,
            body,
            signature,
            now=1790001000,
            tolerance_seconds=300,
        )

    def test_malformed_timestamp_fails_verification(self):
        assert not verify_signature(
            "test-secret",
            "not-a-timestamp",
            b"{}",
            "v1=invalid",
            now=1790000000,
        )

    def test_signature_headers_include_timestamp_and_signature(self):
        headers = create_signature_headers(
            "test-secret",
            b'{"id":"evt-123"}',
            timestamp=1790000000,
        )

        assert headers["X-HookPlatfom-Timestamp"] == "1790000000"
        assert headers["X-HookPlatfom-Signature"].startswith("v1=")

    def test_encryption_round_trip(self, monkeypatch):
        from app.config import settings
        from cryptography.fernet import Fernet
        from pydantic import SecretStr

        monkeypatch.setattr(
            settings,
            "webhook_encryption_key",
            SecretStr(Fernet.generate_key().decode("ascii")),
        )

        secret = "endpoint-secret-to-protect"

        encrypted = encrypt_signing_secret(secret)

        assert encrypted != secret
        assert decrypt_signing_secret(encrypted) == secret

    def test_invalid_encrypted_secret_is_rejected(self, monkeypatch):
        from app.config import settings
        from cryptography.fernet import Fernet
        from pydantic import SecretStr

        monkeypatch.setattr(
            settings,
            "webhook_encryption_key",
            SecretStr(Fernet.generate_key().decode("ascii")),
        )

        with pytest.raises(ValueError):
            decrypt_signing_secret("not-valid-fernet-ciphertext")
