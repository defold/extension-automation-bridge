"""Typed snapshot wrappers for Automation Bridge scene elements."""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, TypedDict, Union


class ElementSelector(TypedDict, total=False):
    """Supported keyword filters for element queries and waits.

    ``type``, ``name``, ``text`` and ``url`` match substrings, ignoring case
    unless ``case_sensitive=True``. Their ``*_exact`` variants, identity fields,
    paths and application annotations use case-sensitive exact matching.
    ``limit`` is 0..500 (default 50); zero requests only counts. ``cursor`` is
    the opaque string returned by a page and takes precedence over ``offset``.
    Pages are live snapshots, not a frozen traversal across multiple requests.
    """

    id: str
    instance_id: str
    logical_id: str
    type: str
    type_exact: str
    name: str
    name_exact: str
    text: str
    text_exact: str
    url: str
    url_exact: str
    path: str
    kind: str
    automation_id: str
    localization_key: str
    role: str
    enabled: bool
    has_bounds: bool
    visible_and_enabled: bool
    visible: bool
    case_sensitive: bool
    include: Union[str, Iterable[str]]
    limit: int
    offset: int
    cursor: str


@dataclass(frozen=True)
class ElementPage:
    """One native query result, including pagination and snapshot evidence.

    Pass ``next_cursor`` into the next query with the same filters. Re-query
    after scene changes; cursors do not pin a snapshot. ``raw`` retains native
    diagnostics such as active collections and excluded matches.
    """

    elements: Tuple["Element", ...]
    matched: int
    total: int
    offset: int
    next_cursor: Optional[str]
    truncated: bool
    scene_sequence: int
    engine_frame: int
    raw: Mapping[str, Any]

    @classmethod
    def from_raw(cls, data: Mapping[str, Any]) -> "ElementPage":
        """Wrap a native /elements data object without dropping its metadata."""
        elements = tuple(Element(item) for item in data.get("elements", ()) if isinstance(item, dict))
        cursor = data.get("next_cursor")
        return cls(
            elements=elements,
            matched=int(data.get("matched", len(elements))),
            total=int(data.get("total", len(elements))),
            offset=int(data.get("offset", 0)),
            next_cursor=str(cursor) if cursor is not None else None,
            truncated=bool(data.get("truncated", False)),
            scene_sequence=int(data.get("scene_sequence", 0)),
            engine_frame=int(data.get("engine_frame", 0)),
            raw=dict(data),
        )

    @property
    def count(self) -> int:
        """Return the number of elements in this page, not the total matches."""
        return len(self.elements)


@dataclass(frozen=True)
class Bounds:
    """Screen, center, and normalized bounds for a runtime element snapshot."""

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
class Element:
    """Snapshot of one inspectable game object, component, or GUI element."""

    raw: Dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def snapshot_id(self) -> str:
        """Return the path-derived identity for this particular scene shape."""
        return self.raw.get("snapshot_id", self.id)

    @property
    def instance_id(self) -> Optional[str]:
        """Return Defold's instance identifier when this element has an HInstance."""
        return self.raw.get("instance_id")

    @property
    def instance_generation(self) -> Optional[int]:
        """Return Defold's allocation generation for the backing instance."""
        value = self.raw.get("instance_generation")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def logical_id(self) -> Optional[str]:
        """Return the bridge identity combining instance identifier and generation."""
        return self.raw.get("logical_id")

    @property
    def created_scene_sequence(self) -> Optional[int]:
        value = self.raw.get("created_scene_sequence")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def scene_sequence(self) -> int:
        return int(self.raw.get("scene_sequence", 0))

    @property
    def engine_frame(self) -> int:
        return int(self.raw.get("engine_frame", 0))

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
    def parent_id(self) -> Optional[str]:
        """Return the parent element id, if this snapshot has one."""
        return self.raw.get("parent")

    @property
    def text(self) -> Optional[str]:
        return self.raw.get("text")

    @property
    def url(self) -> Optional[str]:
        return self.raw.get("url")

    @property
    def automation_id(self) -> Optional[str]:
        """Return the stable application-supplied automation id, if annotated."""
        return self.raw.get("automation_id")

    @property
    def localization_key(self) -> Optional[str]:
        """Return the application-supplied localization key, if annotated."""
        return self.raw.get("localization_key")

    @property
    def role(self) -> Optional[str]:
        """Return the application-supplied semantic role, if annotated."""
        return self.raw.get("role")

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
    def children(self) -> List["Element"]:
        children = self.raw.get("children", [])
        if not isinstance(children, list):
            return []
        return [Element(child) for child in children if isinstance(child, dict)]

    def compact(self) -> str:
        """Return a one-line diagnostic summary for selector errors and logs."""
        center = self.center
        center_text = ""
        if center and "x" in center and "y" in center:
            center_text = f" center=({center['x']},{center['y']})"
        text = f" text={self.text!r}" if self.text is not None else ""
        return (
            f"id={self.id!r} logical_id={self.logical_id!r} name={self.name!r} type={self.type!r}{text} "
            f"automation_id={self.automation_id!r} "
            f"path={self.path!r} visible={self.visible} enabled={self.enabled}{center_text}"
        )
