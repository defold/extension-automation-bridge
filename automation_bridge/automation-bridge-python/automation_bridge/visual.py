"""Optional, dependency-free pixel observations layered above scene assertions.

The base client does not use pixels to decide node identity or visibility.
This module is imported only by explicit visual waits/assertions.
"""

import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

from .receipts import ScreenshotReceipt
from .waits import wait_until


Region = Union[Mapping[str, float], Sequence[float]]


@dataclass(frozen=True)
class VisualObservation:
    """Result of a stable/change wait with its final atomic screenshot receipt."""

    screenshot: ScreenshotReceipt
    difference: float
    consecutive_frames: int
    region: Tuple[int, int, int, int]


@dataclass(frozen=True)
class _Image:
    width: int
    height: int
    rgba: bytes


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def _read_png(source: Union[ScreenshotReceipt, str, Path, bytes]) -> _Image:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("visual comparison input is not a PNG")
    offset = 8
    width = height = 0
    compressed = bytearray()
    while offset + 12 <= len(data):
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + size]
        offset += size + 12
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise ValueError("visual comparisons require non-interlaced 8-bit RGBA PNGs")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    if not width or not height:
        raise ValueError("PNG has no dimensions")
    raw = zlib.decompress(bytes(compressed))
    stride = width * 4
    previous = bytearray(stride)
    rgba = bytearray()
    cursor = 0
    for _ in range(height):
        filter_type = raw[cursor]
        cursor += 1
        encoded = raw[cursor : cursor + stride]
        cursor += stride
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - 4] if index >= 4 else 0
            up = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 0:
                decoded = value
            elif filter_type == 1:
                decoded = value + left
            elif filter_type == 2:
                decoded = value + up
            elif filter_type == 3:
                decoded = value + ((left + up) // 2)
            elif filter_type == 4:
                decoded = value + _paeth(left, up, upper_left)
            else:
                raise ValueError(f"unsupported PNG filter {filter_type}")
            row[index] = decoded & 0xFF
        rgba.extend(row)
        previous = row
    return _Image(width, height, bytes(rgba))


def _region(value: Optional[Region], width: int, height: int) -> Tuple[int, int, int, int]:
    if value is None:
        return 0, 0, width, height
    if isinstance(value, Mapping):
        x, y, w, h = (value[key] for key in ("x", "y", "w", "h"))
    elif len(value) == 4:
        x, y, w, h = value
    else:
        raise ValueError("region must be a mapping or (x, y, w, h)")
    x = max(0, min(width, int(round(x))))
    y = max(0, min(height, int(round(y))))
    w = max(0, min(width - x, int(round(w))))
    h = max(0, min(height - y, int(round(h))))
    if w == 0 or h == 0:
        raise ValueError("visual comparison region is empty")
    return x, y, w, h


def difference(
    before: Union[ScreenshotReceipt, str, Path, bytes],
    after: Union[ScreenshotReceipt, str, Path, bytes],
    region: Optional[Region] = None,
    include_alpha: bool = False,
) -> float:
    """Return normalized mean absolute channel error in a top-left viewport region.

    Images are compared at native capture size without scaling. RGB channels are
    included by default; ``include_alpha=True`` includes alpha as a fourth channel.
    """
    first = _read_png(before)
    second = _read_png(after)
    if (first.width, first.height) != (second.width, second.height):
        raise ValueError("visual comparison images have different dimensions; scaling is not implicit")
    x, y, w, h = _region(region, first.width, first.height)
    channel_count = 4 if include_alpha else 3
    error = 0
    for row in range(y, y + h):
        start = (row * first.width + x) * 4
        end = start + w * 4
        for index in range(start, end, 4):
            for channel in range(channel_count):
                error += abs(first.rgba[index + channel] - second.rgba[index + channel])
    return error / float(w * h * channel_count * 255)


class VisualClient:
    """Explicit visual fallback package for an AutomationBridgeClient."""

    def __init__(self, bridge: Any):
        self.bridge = bridge

    def difference(
        self,
        before: Union[ScreenshotReceipt, str, Path, bytes],
        after: Union[ScreenshotReceipt, str, Path, bytes],
        region: Optional[Region] = None,
        include_alpha: bool = False,
    ) -> float:
        """Return the normalized pixel difference between two captures."""
        return difference(before, after, region=region, include_alpha=include_alpha)

    def wait_for_stable_frame(
        self,
        region: Optional[Region] = None,
        consecutive_frames: int = 3,
        tolerance: float = 0.01,
        timeout: float = 10.0,
        include_alpha: bool = False,
    ) -> VisualObservation:
        """Wait until consecutive rendered captures stay within normalized MAE tolerance."""
        if consecutive_frames < 2:
            raise ValueError("consecutive_frames must be at least 2")
        if not 0.0 <= tolerance <= 1.0:
            raise ValueError("tolerance must be from 0 through 1")
        previous = self.bridge.screenshot(wait=True)
        stable = 1
        last_difference = 1.0

        def sample() -> Optional[VisualObservation]:
            nonlocal previous, stable, last_difference
            current = self.bridge.screenshot(wait=True, after_frames=1)
            last_difference = difference(previous, current, region=region, include_alpha=include_alpha)
            stable = stable + 1 if last_difference <= tolerance else 1
            previous = current
            if stable < consecutive_frames:
                return None
            image = _read_png(current)
            return VisualObservation(current, last_difference, stable, _region(region, image.width, image.height))

        return wait_until(
            sample,
            timeout=timeout,
            interval=0.0,
            message=f"region did not remain stable for {consecutive_frames} frames; last difference={last_difference}",
        )

    def wait_for_region_change(
        self,
        before: Union[ScreenshotReceipt, str, Path, bytes],
        region: Optional[Region] = None,
        tolerance: float = 0.01,
        timeout: float = 10.0,
        include_alpha: bool = False,
    ) -> VisualObservation:
        """Wait until normalized MAE exceeds ``tolerance`` in the selected region."""
        started = time.monotonic()
        last_difference = 0.0

        def changed() -> Optional[VisualObservation]:
            nonlocal last_difference
            current = self.bridge.screenshot(wait=True, after_frames=1)
            last_difference = difference(before, current, region=region, include_alpha=include_alpha)
            if last_difference <= tolerance:
                return None
            image = _read_png(current)
            return VisualObservation(current, last_difference, 1, _region(region, image.width, image.height))

        return wait_until(
            changed,
            timeout=max(0.0, timeout - (time.monotonic() - started)),
            interval=0.0,
            message=f"region did not change beyond tolerance {tolerance}; last difference={last_difference}",
        )

    def assert_matches(
        self,
        expected: Union[ScreenshotReceipt, str, Path, bytes],
        actual: Union[ScreenshotReceipt, str, Path, bytes],
        region: Optional[Region] = None,
        tolerance: float = 0.01,
        include_alpha: bool = False,
    ) -> float:
        """Assert a pixel comparison and return its normalized error."""
        value = difference(expected, actual, region=region, include_alpha=include_alpha)
        if value > tolerance:
            raise AssertionError(f"visual difference {value:.6f} exceeded tolerance {tolerance:.6f}")
        return value
