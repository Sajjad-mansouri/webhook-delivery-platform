import hashlib

from app.security.api_keys import (
    API_KEY_PREFIX,
    generate_api_key,
    hash_api_key,
)


class TestAPIKeyGeneration:
    def test_generated_key_has_expected_prefix(self) -> None:
        key = generate_api_key()

        assert key.startswith(API_KEY_PREFIX)

    def test_generated_keys_are_different(self) -> None:
        first_key = generate_api_key()
        second_key = generate_api_key()

        assert first_key != second_key

    def test_generated_key_has_a_secret_after_prefix(self) -> None:
        key = generate_api_key()

        assert len(key) > len(API_KEY_PREFIX)

    def test_hash_is_sha256_hex_digest(self) -> None:
        key = generate_api_key()

        expected = hashlib.sha256(key.encode("utf-8")).hexdigest()

        assert hash_api_key(key) == expected
        assert len(hash_api_key(key)) == 64

    def test_hashing_same_key_is_deterministic(self) -> None:
        key = generate_api_key()

        assert hash_api_key(key) == hash_api_key(key)

    def test_different_keys_have_different_hashes(self) -> None:
        first_key = generate_api_key()
        second_key = generate_api_key()

        assert hash_api_key(first_key) != hash_api_key(second_key)

    def test_hash_is_not_the_plaintext_key(self) -> None:
        key = generate_api_key()

        assert hash_api_key(key) != key
