import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_BASE_DELAY_SECONDS = 5.0
DEFAULT_MAX_DELAY_SECONDS = 3600.0
DEFAULT_JITTER_RATIO = 0.2

RETRYABLE_STATUS_CODES = {408, 429}


@dataclass(frozen=True, slots=True)
class RetryDecision:
    should_retry: bool
    reason: str


def classify_http_status(status_code: int) -> RetryDecision:
    if not 100 <= status_code <= 599:
        raise ValueError("HTTP status code must be between 100 and 599.")

    if 200 <= status_code < 300:
        return RetryDecision(False, "success")

    if status_code in RETRYABLE_STATUS_CODES or status_code >= 500:
        return RetryDecision(True, "retryable_http_error")

    return RetryDecision(False, "terminal_http_error")


def calculate_retry_delay(
    attempt_number: int,
    *,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    random_value: float | None = None,
) -> float:
    if attempt_number < 1:
        raise ValueError("attempt_number must be at least 1.")

    if base_delay_seconds <= 0:
        raise ValueError("base_delay_seconds must be positive.")

    if max_delay_seconds <= 0:
        raise ValueError("max_delay_seconds must be positive.")

    if jitter_ratio < 0 or jitter_ratio > 1:
        raise ValueError("jitter_ratio must be between 0 and 1.")

    if random_value is None:
        random_value = random.uniform(-1.0, 1.0)

    if not -1.0 <= random_value <= 1.0:
        raise ValueError("random_value must be between -1 and 1.")

    exponential_delay = min(
        max_delay_seconds,
        base_delay_seconds * (2 ** min(attempt_number - 1, 60)),
    )

    delay = exponential_delay * (1 + jitter_ratio * random_value)

    return max(0.0, min(max_delay_seconds, delay))


def next_retry_at(
    attempt_number: int,
    *,
    now: datetime | None = None,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
    jitter_ratio: float = DEFAULT_JITTER_RATIO,
    random_value: float | None = None,
) -> datetime:
    if now is None:
        now = datetime.now(UTC)

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")

    delay = calculate_retry_delay(
        attempt_number,
        base_delay_seconds=base_delay_seconds,
        max_delay_seconds=max_delay_seconds,
        jitter_ratio=jitter_ratio,
        random_value=random_value,
    )

    return now + timedelta(seconds=delay)
