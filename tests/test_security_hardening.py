# tests/test_security_hardening.py

import hashlib
import hmac
import time

import pytest
from app.security.api_keys import (
    api_key_matches,
    generate_api_key,
    hash_api_key,
)
from app.security.logging_utils import redact_header, redact_text
from app.security.receiver_verification import verify_webhook_signature
from app.security.ssrf import (
    UnsafeWebhookURLError,
    validate_webhook_url_syntax,
)


class TestAPIKeySecurity:
    def test_generated_api_key_has_expected_prefix(self) -> None:
        assert generate_api_key().startswith("wp_live_")

    def test_generated_api_keys_are_distinct(self) -> None:
        assert generate_api_key() != generate_api_key()

    def test_hash_is_deterministic(self) -> None:
        key = generate_api_key()
        assert hash_api_key(key) == hash_api_key(key)

    def test_raw_key_matches_its_hash(self) -> None:
        key = generate_api_key()
        assert api_key_matches(key, hash_api_key(key))

    def test_wrong_key_does_not_match(self) -> None:
        assert not api_key_matches(
            generate_api_key(),
            hash_api_key(generate_api_key()),
        )


class TestWebhookURLValidation:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/webhook",
            "https://localhost/webhook",
            "https://service.local/webhook",
            "https://127.0.0.1/webhook",
            "https://[::1]/webhook",
            "https://user:password@example.com/webhook",
            "https://example.com:8443/webhook",
            "https://example.com/webhook#fragment",
        ],
    )
    def test_rejects_disallowed_url(
        self,
        url: str,
    ) -> None:
        with pytest.raises(UnsafeWebhookURLError):
            validate_webhook_url_syntax(url)

    def test_accepts_https_hostname_syntax(self) -> None:
        # This checks URL syntax only, not whether the host is safe to connect to.
        validate_webhook_url_syntax("https://receiver.example.com/webhook")


class TestWebhookSignatureVerification:
    def test_accepts_valid_signature(self) -> None:
        secret = "test-secret"
        timestamp = str(int(time.time()))
        body = b'{"event":"order.created"}'

        digest = hmac.new(
            secret.encode(),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()

        assert verify_webhook_signature(
            secret=secret,
            timestamp_header=timestamp,
            signature_header=f"v1={digest}",
            body=body,
        )

    def test_rejects_tampered_body(self) -> None:
        secret = "test-secret"
        timestamp = str(int(time.time()))
        original_body = b'{"event":"order.created"}'

        digest = hmac.new(
            secret.encode(),
            timestamp.encode("ascii") + b"." + original_body,
            hashlib.sha256,
        ).hexdigest()

        assert not verify_webhook_signature(
            secret=secret,
            timestamp_header=timestamp,
            signature_header=f"v1={digest}",
            body=b'{"event":"order.deleted"}',
        )

    def test_rejects_expired_timestamp(self) -> None:
        timestamp = "100"
        body = b"payload"
        secret = "test-secret"

        digest = hmac.new(
            secret.encode(),
            timestamp.encode("ascii") + b"." + body,
            hashlib.sha256,
        ).hexdigest()

        assert not verify_webhook_signature(
            secret=secret,
            timestamp_header=timestamp,
            signature_header=f"v1={digest}",
            body=body,
            tolerance_seconds=300,
            now=1000,
        )

    def test_rejects_malformed_signature(self) -> None:
        assert not verify_webhook_signature(
            secret="test-secret",
            timestamp_header=str(int(time.time())),
            signature_header="v2=invalid",
            body=b"payload",
        )


class TestSensitiveDataRedaction:
    def test_redacts_api_key_header(self) -> None:
        assert (
            redact_header(
                "X-API-Key",
                "wp_live_sensitive-value",
            )
            == "[REDACTED]"
        )

    def test_preserves_non_sensitive_header(self) -> None:
        assert (
            redact_header(
                "Content-Type",
                "application/json",
            )
            == "application/json"
        )

    def test_redacts_common_secret_patterns(self) -> None:
        result = redact_text("password=hunter2 token=secret-token")

        assert "hunter2" not in result
        assert "secret-token" not in result
