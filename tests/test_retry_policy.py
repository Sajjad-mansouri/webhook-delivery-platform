from datetime import UTC, datetime, timedelta

import pytest
from app.worker.retry import (
    calculate_retry_delay,
    classify_http_status,
    next_retry_at,
)


class TestClassifyHTTPStatus:
    @pytest.mark.parametrize(
        ("status_code", "expected_retry", "expected_reason"),
        [
            (200, False, "success"),
            (204, False, "success"),
            (301, False, "terminal_http_error"),
            (400, False, "terminal_http_error"),
            (401, False, "terminal_http_error"),
            (404, False, "terminal_http_error"),
            (408, True, "retryable_http_error"),
            (429, True, "retryable_http_error"),
            (500, True, "retryable_http_error"),
            (503, True, "retryable_http_error"),
        ],
    )
    def test_classifies_http_status(
        self,
        status_code: int,
        expected_retry: bool,
        expected_reason: str,
    ) -> None:
        decision = classify_http_status(status_code)

        assert decision.should_retry is expected_retry
        assert decision.reason == expected_reason

    @pytest.mark.parametrize("status_code", [99, 600, 999])
    def test_rejects_invalid_status_codes(
        self,
        status_code: int,
    ) -> None:
        with pytest.raises(ValueError):
            classify_http_status(status_code)


class TestCalculateRetryDelay:
    @pytest.mark.parametrize(
        ("attempt_number", "expected"),
        [
            (1, 5.0),
            (2, 10.0),
            (3, 20.0),
            (4, 40.0),
        ],
    )
    def test_exponential_backoff_without_jitter(
        self,
        attempt_number: int,
        expected: float,
    ) -> None:
        delay = calculate_retry_delay(
            attempt_number,
            jitter_ratio=0,
        )

        assert delay == expected

    def test_applies_positive_jitter(self) -> None:
        delay = calculate_retry_delay(
            1,
            jitter_ratio=0.2,
            random_value=1.0,
        )

        assert delay == 6.0

    def test_applies_negative_jitter(self) -> None:
        delay = calculate_retry_delay(
            1,
            jitter_ratio=0.2,
            random_value=-1.0,
        )

        assert delay == 4.0

    def test_caps_exponential_delay(self) -> None:
        delay = calculate_retry_delay(
            20,
            base_delay_seconds=5,
            max_delay_seconds=60,
            jitter_ratio=0,
        )

        assert delay == 60.0

    @pytest.mark.parametrize(
        "attempt_number",
        [0, -1],
    )
    def test_rejects_invalid_attempt_number(
        self,
        attempt_number: int,
    ) -> None:
        with pytest.raises(ValueError):
            calculate_retry_delay(attempt_number)

    def test_rejects_invalid_jitter_value(self) -> None:
        with pytest.raises(ValueError):
            calculate_retry_delay(
                1,
                random_value=1.5,
            )


class TestNextRetryAt:
    def test_returns_future_datetime(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)

        result = next_retry_at(
            1,
            now=now,
            jitter_ratio=0,
        )

        assert result == now + timedelta(seconds=5)

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError):
            next_retry_at(
                1,
                now=datetime(2026, 1, 1),
            )
