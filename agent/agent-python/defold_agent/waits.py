"""Polling helpers for Defold Agent scripts."""

import time
from typing import Callable, Optional, TypeVar


T = TypeVar("T")


def wait_until(
    fn: Callable[[], T],
    timeout: float = 10.0,
    interval: float = 0.1,
    message: str = "condition not met before timeout",
) -> T:
    """Poll `fn` until it returns a truthy value or `timeout` expires."""
    deadline = time.monotonic() + timeout
    last_error: Optional[BaseException] = None

    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - preserve the last polling failure.
            last_error = exc
        time.sleep(interval)

    if last_error:
        raise AssertionError(f"{message}: {last_error}") from last_error
    raise AssertionError(message)
