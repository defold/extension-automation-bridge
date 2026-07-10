"""Focused public API for the Automation Bridge Python wrapper."""

from .client import (
    AutomationBridgeApiError,
    AutomationBridgeClient,
    AutomationBridgeError,
    HttpError,
    IncompatibleApiVersionError,
    InputExecutionError,
    InputReceipt,
    SelectorError,
    UnsupportedCapabilityError,
)
from .editor import EditorClient
from .events import CommandTimeout, Event, EventBufferOverflow, EventStream, StateSnapshot
from .nodes import Bounds, Node
from .profiler import ProfilerClient, ProfilerDataError, ResourceProfileEntry
from .receipts import ObservationReceipt, ScreenshotReceipt
from .waits import WaitTimeoutError, wait_until


__all__ = [
    "AutomationBridgeApiError",
    "AutomationBridgeClient",
    "AutomationBridgeError",
    "Bounds",
    "CommandTimeout",
    "EditorClient",
    "Event",
    "EventBufferOverflow",
    "EventStream",
    "HttpError",
    "IncompatibleApiVersionError",
    "InputExecutionError",
    "InputReceipt",
    "Node",
    "ObservationReceipt",
    "ProfilerClient",
    "ProfilerDataError",
    "ResourceProfileEntry",
    "ScreenshotReceipt",
    "SelectorError",
    "StateSnapshot",
    "UnsupportedCapabilityError",
    "WaitTimeoutError",
    "wait_until",
]
