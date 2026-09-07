"""Cooperative cancellation shared by Python automation and host adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time
from typing import Iterator, Optional


class OperationCancelled(RuntimeError):
    """An operation stopped at a cancellation checkpoint.

    Cancellation does not imply that an already submitted native operation was
    undone. ``cleanup_error`` preserves a failed cancellation request, including
    the native refusal to preempt a running Lua callback.
    """

    def __init__(self, reason: str = "operation cancelled"):
        self.reason = reason
        self.cleanup_error: Optional[BaseException] = None
        super().__init__(reason)


class CancellationToken:
    """A one-shot signal that another thread may cancel.

    Create one token per operation and enter ``engine.cancellation_scope(token)``
    in the thread doing the work. Call ``token.cancel()`` from the controlling
    thread. Polling and delays stop cooperatively; an in-flight HTTP request is
    bounded by its transport timeout and is not forcibly interrupted.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "operation cancelled"

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation was requested."""
        return self._event.is_set()

    def cancel(self, reason: str = "operation cancelled") -> None:
        """Request cancellation, preserving the first reason."""
        if not isinstance(reason, str) or not reason:
            raise ValueError("cancellation reason must be a non-empty string")
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise OperationCancelled when this token has been cancelled."""
        if self.cancelled:
            raise OperationCancelled(self._reason)


_current_token: ContextVar[Optional[CancellationToken]] = ContextVar(
    "automation_bridge_cancellation", default=None
)


@contextmanager
def cancellation_scope(token: CancellationToken) -> Iterator[CancellationToken]:
    """Apply a token to synchronous automation calls in this context.

    Scopes restore the previous token on exit. They are local to the execution
    context, so one agent's cancellation does not cancel another agent's work.
    Native input waits request release through their existing cleanup paths.
    Use ``game.cancellation_scope(token)`` to also release this client's queued
    input if cancellation happens while waiting for scene or application state.
    """
    if not isinstance(token, CancellationToken):
        raise TypeError("token must be a CancellationToken")
    token.raise_if_cancelled()
    previous = _current_token.set(token)
    try:
        yield token
        token.raise_if_cancelled()
    finally:
        _current_token.reset(previous)


def check_cancelled() -> None:
    token = _current_token.get()
    if token is not None:
        token.raise_if_cancelled()


def cancellation_active() -> bool:
    return _current_token.get() is not None


def cancellable_sleep(seconds: float) -> None:
    token = _current_token.get()
    if token is None:
        time.sleep(seconds)
    else:
        token.raise_if_cancelled()
        token._event.wait(seconds)
        token.raise_if_cancelled()
