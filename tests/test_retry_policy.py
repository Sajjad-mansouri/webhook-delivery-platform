from datetime import UTC, datetime, timedelta

import pytest
from app.worker.retry import (
    calculate_retry_delay,
    classify_http_status,
    next_retry_at,
)


class TestClassifyHTTPStatus:
    @pytest.mark.parametrize("status_code", [200, 201, 204])
    def test_success_responses_are_not_retryable(self, status_code):
        decision = classify_http_status(status_code)

        assert decision.should_retry is False
        assert decision.reason == "success"

    @pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503])
    def test_temporary_errors_are_retryable(self, status_code):
        decision = classify_http_status(status_code)

        assert decision.should_retry is True
        assert decision.reason == "retryable_http_error"

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    def test_other_client_errors_are_terminal(self, status_code):
        decision = classify_http_status(status_code)

        assert decision.should_retry is False
        assert decision.reason == "terminal_http_error"


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
    def test_exponential_backoff_without_jitter(self, attempt_number, expected):
        actual = calculate_retry_delay(
            attempt_number,
            jitter_ratio=0,
        )

        assert actual == expected

    def test_delay_never_exceeds_maximum(self):
        actual = calculate_retry_delay(
            20,
            max_delay_seconds=60,
            jitter_ratio=0,
        )

        assert actual == 60

    def test_positive_jitter_is_capped(self):
        actual = calculate_retry_delay(
            4,
            max_delay_seconds=30,
            jitter_ratio=0.2,
            random_value=1.0,
        )

        assert actual == 30

    def test_negative_jitter_does_not_make_delay_negative(self):
        actual = calculate_retry_delay(
            1,
            base_delay_seconds=1,
            jitter_ratio=1,
            random_value=-1.0,
        )

        assert actual == 0

    @pytest.mark.parametrize("attempt_number", [0, -1])
    def test_attempt_number_must_be_positive(self, attempt_number):
        with pytest.raises(ValueError):
            calculate_retry_delay(attempt_number)

    def test_invalid_jitter_is_rejected(self):
        with pytest.raises(ValueError):
            calculate_retry_delay(1, jitter_ratio=1.1)


class TestNextRetryAt:
    def test_retry_time_uses_calculated_delay(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)

        actual = next_retry_at(
            2,
            now=now,
            jitter_ratio=0,
        )

        assert actual == now + timedelta(seconds=10)

    def test_naive_datetime_is_rejected(self):
        with pytest.raises(ValueError):
            next_retry_at(
                1,
                now=datetime(2026, 1, 1),
            )
