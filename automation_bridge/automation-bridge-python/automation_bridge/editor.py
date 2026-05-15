"""Client for the Defold editor HTTP server used to build and find the engine."""

import re
import time
from pathlib import Path
from typing import Optional, Union

from .client import AutomationBridgeError, request_json
from .waits import wait_until


_AUTOMATION_BRIDGE_ENDPOINT_TEXT = "Automation Bridge endpoint registered"
_ENGINE_SERVICE_PORT_PATTERNS = (
    re.compile(r"Engine service started on port (\d+)"),
    re.compile(r"Log server started on port (\d+)"),
)
_REMOTERY_URL_PATTERN = re.compile(r"Initialized Remotery \((ws://[^)\s]+)\)")


class EditorClient:
    """Small wrapper around the Defold editor HTTP API for one project."""

    def __init__(self, root: Union[str, Path], port: Optional[int] = None):
        self.root = Path(root).resolve()
        if port is None:
            port_path = self.root / ".internal" / "editor.port"
            if not port_path.exists():
                raise FileNotFoundError(f"Defold editor port file is missing: {port_path}")
            port = int(port_path.read_text(encoding="utf-8").strip())
        self.port = int(port)
        self.base_url = f"http://localhost:{self.port}"
        self._engine_service_port: Optional[int] = self._read_cached_engine_service_port()
        self._remotery_url: Optional[str] = self._read_cached_remotery_url()
        self._last_build_had_engine_service_port: Optional[bool] = None

    @classmethod
    def from_project(cls, root: Union[str, Path] = ".") -> "EditorClient":
        """Create a client by reading `.internal/editor.port` from `root`."""
        return cls(root)

    def build(self, timeout: float = 60.0) -> None:
        """Build/run the project and wait until the Automation Bridge HTTP endpoint registers."""
        previous_lines = self.console_lines()
        previous_registration_count = self._endpoint_registered_count(previous_lines)
        previous_registration_ports = self._latest_registration_engine_service_ports(previous_lines)
        previous_port = self.engine_service_port()
        if previous_port is not None:
            self._engine_service_port = previous_port
        _, response = request_json(f"{self.base_url}/command/build", method="POST", timeout=timeout)
        if not response.get("success"):
            issues = response.get("issues", [])
            raise AutomationBridgeError(f"Defold build failed: {issues}")

        try:
            wait_until(
                lambda: self._has_fresh_endpoint_registration(
                    previous_registration_count,
                    previous_registration_ports,
                ),
                timeout=timeout,
                interval=0.1,
                message="Defold build completed, but Automation Bridge endpoint did not register",
            )
        except AssertionError as exc:
            raise AutomationBridgeError(str(exc)) from exc
        self._last_build_had_engine_service_port = self.latest_registration_has_engine_service_port()
        time.sleep(0.2)

    def console_lines(self) -> list:
        """Return current editor console lines."""
        _, response = request_json(f"{self.base_url}/console", timeout=10.0)
        return response.get("lines", [])

    def engine_service_port(self) -> Optional[int]:
        """Return the service port before endpoint registration, or the cached reused port."""
        ports = self.engine_service_ports()
        return ports[0] if ports else None

    def engine_service_ports(self) -> list:
        """Return candidate engine service ports from current logs, then the cached port."""
        candidates = self.latest_registration_engine_service_ports()

        if self._engine_service_port is not None:
            self._append_port_candidate(candidates, self._engine_service_port)
        return candidates

    def latest_registration_engine_service_ports(self) -> list:
        """Return ports logged before the latest Automation Bridge endpoint registration only."""
        return self._latest_registration_engine_service_ports(self.console_lines())

    def remotery_url(self) -> Optional[str]:
        """Return the Remotery websocket URL from current logs, then the cached URL."""
        urls = self.remotery_urls()
        return urls[0] if urls else None

    def remotery_urls(self) -> list:
        """Return candidate Remotery websocket URLs from current logs, then the cached URL."""
        candidates = self.latest_registration_remotery_urls()
        if self._remotery_url is not None:
            self._append_unique_candidate(candidates, self._remotery_url)
        return candidates

    def latest_registration_remotery_urls(self) -> list:
        """Return Remotery websocket URLs logged before the latest endpoint registration only."""
        return self._latest_registration_remotery_urls(self.console_lines())

    @classmethod
    def _latest_registration_engine_service_ports(cls, lines: list) -> list:
        search_lines = cls._latest_registration_window(lines)
        if search_lines is None:
            search_lines = lines
        candidates = []
        for line in reversed(search_lines):
            for candidate_pattern in _ENGINE_SERVICE_PORT_PATTERNS:
                match = candidate_pattern.search(line)
                if match:
                    cls._append_port_candidate(candidates, int(match.group(1)))
        return candidates

    @classmethod
    def _latest_registration_remotery_urls(cls, lines: list) -> list:
        search_lines = cls._latest_registration_window(lines)
        if search_lines is None:
            search_lines = lines
        candidates = []
        for line in reversed(search_lines):
            match = _REMOTERY_URL_PATTERN.search(line)
            if match:
                cls._append_unique_candidate(candidates, match.group(1))
        return candidates

    def latest_registration_has_engine_service_port(self) -> bool:
        """Return whether the latest endpoint registration had a fresh port nearby."""
        return bool(self.latest_registration_engine_service_ports())

    def last_build_had_engine_service_port(self) -> Optional[bool]:
        """Return whether the last `build()` saw a fresh port before registration."""
        return self._last_build_had_engine_service_port

    @staticmethod
    def _endpoint_registered_count(lines: list) -> int:
        return sum(1 for line in lines if _AUTOMATION_BRIDGE_ENDPOINT_TEXT in line)

    def endpoint_registered_count(self) -> int:
        """Return the number of Automation Bridge endpoint registrations in the editor console."""
        return self._endpoint_registered_count(self.console_lines())

    def _has_fresh_endpoint_registration(self, previous_count: int, previous_ports: list) -> bool:
        lines = self.console_lines()
        current_count = self._endpoint_registered_count(lines)
        if current_count > previous_count:
            return True
        current_ports = self._latest_registration_engine_service_ports(lines)
        return bool(current_count and current_ports and current_ports != previous_ports)

    @staticmethod
    def _latest_registration_window(lines: list) -> Optional[list]:
        endpoint_index: Optional[int] = None
        previous_endpoint_index: Optional[int] = None
        for index, line in enumerate(lines):
            if _AUTOMATION_BRIDGE_ENDPOINT_TEXT in line:
                previous_endpoint_index = endpoint_index
                endpoint_index = index

        if endpoint_index is None:
            return None
        start_index = previous_endpoint_index + 1 if previous_endpoint_index is not None else 0
        return lines[start_index:endpoint_index]

    def remember_engine_service_port(self, port: int) -> None:
        """Remember a validated Automation Bridge API port for reused-engine builds."""
        self._engine_service_port = int(port)
        self._write_cached_engine_service_port(self._engine_service_port)

    def remember_remotery_url(self, url: str) -> None:
        """Remember the Remotery websocket URL for reused-engine builds."""
        self._remotery_url = url
        self._write_cached_remotery_url(url)

    @staticmethod
    def _append_port_candidate(candidates: list, port: int) -> None:
        if port not in candidates:
            candidates.append(port)

    @staticmethod
    def _append_unique_candidate(candidates: list, value: str) -> None:
        if value not in candidates:
            candidates.append(value)

    @property
    def _engine_service_port_cache_path(self) -> Path:
        return self.root / ".internal" / "automation_bridge.engine.port"

    def _read_cached_engine_service_port(self) -> Optional[int]:
        path = self._engine_service_port_cache_path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _write_cached_engine_service_port(self, port: int) -> None:
        path = self._engine_service_port_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{port}\n", encoding="utf-8")

    @property
    def _remotery_url_cache_path(self) -> Path:
        return self.root / ".internal" / "automation_bridge.remotery.url"

    def _read_cached_remotery_url(self) -> Optional[str]:
        path = self._remotery_url_cache_path
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def _write_cached_remotery_url(self, url: str) -> None:
        path = self._remotery_url_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{url}\n", encoding="utf-8")
