"""Typed snapshot wrappers for Defold Agent node responses."""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


@dataclass(frozen=True)
class Bounds:
    """Screen, center, and normalized bounds for a runtime node snapshot."""

    raw: Mapping[str, Any]

    @property
    def screen(self) -> Mapping[str, Any]:
        return self.raw.get("screen", {})

    @property
    def center(self) -> Mapping[str, Any]:
        return self.raw.get("center", {})

    @property
    def normalized(self) -> Mapping[str, Any]:
        return self.raw.get("normalized", {})

    @property
    def x(self) -> Optional[float]:
        return self.screen.get("x")

    @property
    def y(self) -> Optional[float]:
        return self.screen.get("y")

    @property
    def w(self) -> Optional[float]:
        return self.screen.get("w")

    @property
    def h(self) -> Optional[float]:
        return self.screen.get("h")


@dataclass(frozen=True)
class Node:
    """Snapshot wrapper around one node object returned by `/agent/v1`."""

    raw: Dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def name(self) -> str:
        return self.raw.get("name", "")

    @property
    def type(self) -> str:
        return self.raw.get("type", "")

    @property
    def kind(self) -> str:
        return self.raw.get("kind", "")

    @property
    def path(self) -> str:
        return self.raw.get("path", "")

    @property
    def parent(self) -> Optional[str]:
        return self.raw.get("parent")

    @property
    def parent_id(self) -> Optional[str]:
        """Return the parent node id, if this snapshot has one."""
        return self.parent

    @property
    def text(self) -> Optional[str]:
        return self.raw.get("text")

    @property
    def url(self) -> Optional[str]:
        return self.raw.get("url")

    @property
    def visible(self) -> bool:
        return bool(self.raw.get("visible"))

    @property
    def enabled(self) -> bool:
        return bool(self.raw.get("enabled"))

    @property
    def bounds(self) -> Optional[Bounds]:
        bounds = self.raw.get("bounds")
        if not isinstance(bounds, dict):
            return None
        return Bounds(bounds)

    @property
    def center(self) -> Optional[Mapping[str, Any]]:
        bounds = self.bounds
        if not bounds:
            return None
        return bounds.center

    @property
    def children(self) -> List["Node"]:
        children = self.raw.get("children", [])
        if not isinstance(children, list):
            return []
        return [Node(child) for child in children if isinstance(child, dict)]

    def compact(self) -> str:
        """Return a one-line diagnostic summary for selector errors and logs."""
        center = self.center
        center_text = ""
        if center and "x" in center and "y" in center:
            center_text = f" center=({center['x']},{center['y']})"
        text = f" text={self.text!r}" if self.text is not None else ""
        return (
            f"id={self.id!r} name={self.name!r} type={self.type!r}{text} "
            f"path={self.path!r} visible={self.visible} enabled={self.enabled}{center_text}"
        )
