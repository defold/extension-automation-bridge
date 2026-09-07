"""Discoverable application contracts supplied by the running game."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple, Union


Schema = Union[Mapping[str, Any], bool]


@dataclass(frozen=True)
class ApplicationEntry:
    """A registered command, published/declared state, or declared event.

    ``contract`` contains application-authored descriptions and JSON Schema
    metadata. Schemas describe intended payloads; the bridge does not validate
    payloads against them. Empty metadata is valid for undocumented registrations.
    """

    kind: str
    name: str
    contract: Mapping[str, Any]

    @property
    def description(self) -> Optional[str]:
        """Return the application-authored description, when declared."""
        return self.contract.get("description")

    @property
    def input_schema(self) -> Optional[Schema]:
        """Return a command's argument schema, including boolean schemas."""
        return self.contract.get("input_schema")

    @property
    def output_schema(self) -> Optional[Schema]:
        """Return a command's result schema, when declared."""
        return self.contract.get("output_schema")

    @property
    def schema(self) -> Optional[Schema]:
        """Return a state's value or an event's data schema, when declared."""
        return self.contract.get("schema")


@dataclass(frozen=True)
class ApplicationCatalogPage:
    """One catalog page with engine identity and metadata revision.

    Each page is consistent. Restart pagination if ``revision`` or
    ``engine_instance_id`` changes between pages. A declared state can precede its
    first publication; an event declaration does not imply an emitted event.
    """

    entries: Tuple[ApplicationEntry, ...]
    matched: int
    offset: int
    next_cursor: Optional[str]
    revision: int
    engine_instance_id: str
    raw: Mapping[str, Any]

    @property
    def count(self) -> int:
        """Return the number of entries on this page."""
        return len(self.entries)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "ApplicationCatalogPage":
        """Wrap a native catalog response while retaining its source metadata."""
        return cls(
            entries=tuple(ApplicationEntry(item["kind"], item["name"], item["contract"]) for item in raw["entries"]),
            matched=int(raw["matched"]),
            offset=int(raw["offset"]),
            next_cursor=raw.get("next_cursor"),
            revision=int(raw["revision"]),
            engine_instance_id=str(raw["engine_instance_id"]),
            raw=raw,
        )
