"""Helpers for Defold's built-in engine profiler endpoints."""

from dataclasses import dataclass
from typing import List

from .client import AutomationBridgeError, HttpError, request_raw


@dataclass(frozen=True)
class ResourceProfileEntry:
    """One resource row returned by Defold's `/resources_data` profiler endpoint."""

    name: str
    type: str
    size: int
    size_on_disc: int
    ref_count: int


class ProfilerDataError(AutomationBridgeError):
    """Raised when a profiler endpoint returns malformed or unexpected binary data."""

    pass


class ProfilerClient:
    """Client for Defold's built-in engine profiler endpoints."""

    def __init__(self, port: int, timeout: float = 10.0):
        """Create a profiler client for an already-known Defold engine service `port`."""
        self.port = int(port)
        self.timeout = timeout
        self.base_url = f"http://127.0.0.1:{self.port}"

    def resources(self) -> List[ResourceProfileEntry]:
        """Return loaded resources from Defold's `/resources_data` endpoint, largest first."""
        status, body = request_raw(f"{self.base_url}/resources_data", timeout=self.timeout)
        if status < 200 or status >= 300:
            preview = body[:200].decode("utf-8", "replace")
            raise HttpError(
                "GET",
                f"{self.base_url}/resources_data",
                f"unexpected status {status}: {preview}",
                status=status,
            )
        return sorted(parse_resources_data(body), key=lambda resource: resource.size, reverse=True)


class _BinaryReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def eof(self) -> bool:
        return self.offset >= len(self.data)

    def read_string(self, label: str) -> str:
        size = self.read_u16(f"{label} length")
        end = self.offset + size
        if end > len(self.data):
            raise ProfilerDataError(f"truncated profiler data while reading {label}")
        value = self.data[self.offset:end]
        self.offset = end
        return value.decode("utf-8", "replace")

    def read_u16(self, label: str) -> int:
        return self._read_int(label, 2)

    def read_u32(self, label: str) -> int:
        return self._read_int(label, 4)

    def _read_int(self, label: str, size: int) -> int:
        end = self.offset + size
        if end > len(self.data):
            raise ProfilerDataError(f"truncated profiler data while reading {label}")
        value = int.from_bytes(self.data[self.offset:end], "little", signed=False)
        self.offset = end
        return value


def parse_resources_data(data: bytes) -> List[ResourceProfileEntry]:
    """Parse the custom binary payload returned by Defold's `/resources_data` endpoint."""
    reader = _BinaryReader(data)
    tag = reader.read_string("resource profiler tag")
    if tag != "RESS":
        raise ProfilerDataError(f"unexpected resource profiler tag: {tag!r}")

    resources: List[ResourceProfileEntry] = []
    while not reader.eof():
        resources.append(
            ResourceProfileEntry(
                name=reader.read_string("resource name"),
                type=reader.read_string("resource type"),
                size=reader.read_u32("resource size"),
                size_on_disc=reader.read_u32("resource size on disc"),
                ref_count=reader.read_u32("resource reference count"),
            )
        )
    return resources
