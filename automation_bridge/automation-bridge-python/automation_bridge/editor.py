"""Client for the Defold editor HTTP server used to build and find the engine."""

import configparser
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .client import AutomationBridgeError, HttpError, request_json, request_raw
from .waits import wait_until


_AUTOMATION_BRIDGE_ENDPOINT_TEXT = "Automation Bridge endpoint registered"
_ENGINE_SERVICE_PORT_PATTERNS = (
    re.compile(r"Engine service started on port (\d+)"),
    re.compile(r"Log server started on port (\d+)"),
)
_REMOTERY_URL_PATTERN = re.compile(r"Initialized Remotery \((ws://[^)\s]+)\)")


@dataclass(frozen=True)
class DefoldInstallation:
    """One Defold installation discovered through the editor registry."""

    launcher_path: Path
    install_path: Path
    last_launched_at: str


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
        self._cached_engine_identity = self._read_cached_engine_identity()
        self._remotery_url: Optional[str] = self._read_cached_remotery_url()
        self._last_build_had_engine_service_port: Optional[bool] = None
        self._lifecycle_events = []

    @classmethod
    def from_project(
        cls,
        root: Union[str, Path] = ".",
        *,
        start_if_needed: bool = True,
        timeout: float = 30.0,
        launcher: Optional[Union[str, Path]] = None,
    ) -> "EditorClient":
        """Connect to this project's editor, launching Defold when necessary."""
        project_root = Path(root).resolve()
        try:
            client = cls(project_root)
        except (FileNotFoundError, ValueError):
            client = None
        if client is not None and client.is_running(timeout=min(1.0, timeout)):
            client._record_lifecycle("editor_reused", port=client.port)
            return client
        if not start_if_needed:
            if client is not None:
                return client
            raise FileNotFoundError(
                f"Defold editor port file is missing: {project_root / '.internal' / 'editor.port'}"
            )

        project_file = project_root / "game.project"
        if not project_file.is_file():
            raise FileNotFoundError(f"Defold project file is missing: {project_file}")
        launcher_path = Path(launcher).expanduser().resolve() if launcher is not None else cls.latest_installation().launcher_path
        if not launcher_path.is_file():
            raise FileNotFoundError(f"Defold launcher is missing: {launcher_path}")
        process = subprocess.Popen(
            [str(launcher_path), str(project_file)],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )

        def ready() -> Optional["EditorClient"]:
            try:
                candidate = cls(project_root)
                return candidate if candidate.is_running(timeout=min(1.0, timeout)) else None
            except (AutomationBridgeError, FileNotFoundError, OSError, ValueError):
                return None

        client = wait_until(
            ready,
            timeout=timeout,
            interval=0.1,
            message=f"Defold editor did not start for {project_root}",
            retry_exceptions=(AutomationBridgeError,),
        )
        client._record_lifecycle(
            "editor_started",
            launcher=str(launcher_path),
            process_id=process.pid,
            port=client.port,
        )
        return client

    @classmethod
    def installations(cls) -> list[DefoldInstallation]:
        """Return registered Defold installations, newest launch first."""
        registry_path = cls.installation_registry_path()
        try:
            value = json.loads(registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise AutomationBridgeError(f"cannot read Defold installation registry {registry_path}: {exc}") from exc
        if not isinstance(value, list):
            raise AutomationBridgeError(f"Defold installation registry is not an array: {registry_path}")
        installations = []
        for item in value:
            if not isinstance(item, dict):
                continue
            launcher_path = item.get("launcherPath")
            install_path = item.get("installPath")
            last_launched_at = item.get("lastLaunchedAt")
            if not all(isinstance(field, str) and field for field in (launcher_path, install_path, last_launched_at)):
                continue
            launcher = Path(launcher_path).expanduser()
            if not launcher.is_file():
                continue
            installations.append(
                DefoldInstallation(
                    launcher_path=launcher,
                    install_path=Path(install_path).expanduser(),
                    last_launched_at=last_launched_at,
                )
            )
        return sorted(installations, key=lambda installation: installation.last_launched_at, reverse=True)

    @classmethod
    def latest_installation(cls) -> DefoldInstallation:
        """Return the most recently launched registered Defold installation."""
        installations = cls.installations()
        if not installations:
            raise AutomationBridgeError(
                f"no Defold installation was found in {cls.installation_registry_path()}"
            )
        return installations[0]

    @staticmethod
    def installation_registry_path() -> Path:
        """Return the platform-specific registry path introduced by Defold #12699."""
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "Defold" / "installations.json"
        if sys.platform == "win32":
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                raise AutomationBridgeError("LOCALAPPDATA is not set")
            return Path(base) / "Defold" / "installations.json"
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "Defold" / "installations.json"

    def is_running(self, timeout: float = 1.0) -> bool:
        """Return whether this project's recorded editor port serves the editor API."""
        try:
            status, _ = request_json(f"{self.base_url}/openapi.json", timeout=timeout)
            return 200 <= status < 300
        except AutomationBridgeError:
            return False

    def build(self, timeout: float = 60.0) -> None:
        """Build/run the project and wait until the Automation Bridge HTTP endpoint registers."""
        self._record_lifecycle("editor_build_started")
        previous_lines = self.console_lines()
        previous_registration_count = self._endpoint_registered_count(previous_lines)
        previous_registration_ports = self._latest_registration_engine_service_ports(previous_lines)
        previous_port = self.engine_service_port()
        if previous_port is not None:
            self._engine_service_port = previous_port
        try:
            _, response = request_json(f"{self.base_url}/command/build", method="POST", timeout=timeout)
        except Exception as exc:
            self._record_lifecycle("editor_build_failed", error=str(exc))
            raise
        if not response.get("success"):
            issues = response.get("issues", [])
            self._record_lifecycle("editor_build_failed", issues=issues)
            raise AutomationBridgeError(f"Defold build failed: {issues}")
        self._record_lifecycle("editor_build_completed")

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
            self._record_lifecycle("new_engine_registration_failed", error=str(exc))
            raise AutomationBridgeError(str(exc)) from exc
        self._record_lifecycle("new_engine_registered")
        self._last_build_had_engine_service_port = self._latest_registration_has_engine_service_port()
        time.sleep(0.2)

    def console_lines(self) -> list:
        """Return current editor console lines."""
        _, response = request_json(f"{self.base_url}/console", timeout=10.0)
        return response.get("lines", [])

    def preview(
        self,
        path: Union[str, Path],
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        resolution_multiplier: Optional[float] = None,
        timeout: float = 30.0,
    ) -> bytes:
        """Render a scene resource through the editor and return PNG bytes.

        Supported resources are those with a scene view, such as collections,
        game objects, GUI scenes, particle effects, and tile maps. Omitted
        dimensions use the project display dimensions. Use
        ``resolution_multiplier`` for a smaller project-aspect-ratio preview.
        """
        if resolution_multiplier is not None:
            if width is not None or height is not None:
                raise ValueError("resolution_multiplier is mutually exclusive with width and height")
            if (
                not isinstance(resolution_multiplier, (int, float))
                or isinstance(resolution_multiplier, bool)
                or not 0.01 <= float(resolution_multiplier) <= 1.0
            ):
                raise ValueError("preview resolution_multiplier must be from 0.01 through 1.0")
            display_width, display_height = self._project_display_size()
            width = max(1, round(display_width * float(resolution_multiplier)))
            height = max(1, round(display_height * float(resolution_multiplier)))
        params = {}
        for name, value in (("width", width), ("height", height)):
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4096:
                    raise ValueError(f"preview {name} must be an integer from 1 through 4096")
                params[name] = value
        project_path = str(path).replace("\\", "/").lstrip("/")
        if not project_path:
            raise ValueError("preview path must identify a project resource")
        encoded_path = urllib.parse.quote(project_path, safe="/")
        url = f"{self.base_url}/preview/{encoded_path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, body = request_raw(url, timeout=timeout)
        if status < 200 or status >= 300:
            message = body[:300].decode("utf-8", "replace").strip() or f"unexpected status {status}"
            raise HttpError("GET", url, message, status=status)
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HttpError("GET", url, "editor preview did not return a PNG", status=status)
        return body

    def _project_display_size(self) -> tuple[int, int]:
        config = configparser.ConfigParser(interpolation=None, strict=False)
        project_path = self.root / "game.project"
        try:
            with project_path.open(encoding="utf-8") as stream:
                config.read_file(stream)
            width = config.getint("display", "width", fallback=960)
            height = config.getint("display", "height", fallback=640)
        except (OSError, configparser.Error, ValueError) as exc:
            raise AutomationBridgeError(f"cannot read project display dimensions from {project_path}: {exc}") from exc
        if width <= 0 or height <= 0:
            raise AutomationBridgeError(
                f"project display dimensions must be positive, got {width}x{height}"
            )
        return width, height

    def engine_service_port(self) -> Optional[int]:
        """Return the service port before endpoint registration, or the cached reused port."""
        ports = self._engine_service_ports()
        return ports[0] if ports else None

    def _engine_service_ports(self) -> list:
        """Return candidate engine service ports from current logs, then the cached port."""
        candidates = self._current_registration_engine_service_ports()

        if self._engine_service_port is not None:
            self._append_port_candidate(candidates, self._engine_service_port)
        return candidates

    def _current_registration_engine_service_ports(self) -> list:
        """Return ports logged before the latest Automation Bridge endpoint registration only."""
        return self._latest_registration_engine_service_ports(self.console_lines())

    def _remotery_url_value(self) -> Optional[str]:
        """Return the Remotery websocket URL from current logs, then the cached URL."""
        urls = self._remotery_urls()
        return urls[0] if urls else None

    def _remotery_urls(self) -> list:
        """Return candidate Remotery websocket URLs from current logs, then the cached URL."""
        candidates = self._current_registration_remotery_urls()
        if self._remotery_url is not None:
            self._append_unique_candidate(candidates, self._remotery_url)
        return candidates

    def _current_registration_remotery_urls(self) -> list:
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

    def _latest_registration_has_engine_service_port(self) -> bool:
        """Return whether the latest endpoint registration had a fresh port nearby."""
        return bool(self._current_registration_engine_service_ports())

    @staticmethod
    def _endpoint_registered_count(lines: list) -> int:
        return sum(1 for line in lines if _AUTOMATION_BRIDGE_ENDPOINT_TEXT in line)

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

    def _remember_engine_service_port(
        self,
        port: int,
        engine_instance_id: Optional[str] = None,
        project_identity: Optional[str] = None,
    ) -> None:
        """Remember a validated port and identity so stale port reuse is detectable."""
        self._engine_service_port = int(port)
        self._write_cached_engine_service_port(self._engine_service_port)
        self._cached_engine_identity = {
            "port": self._engine_service_port,
            "engine_instance_id": engine_instance_id,
            "project_identity": project_identity,
        }
        self._write_cached_engine_identity(self._cached_engine_identity)

    def _validate_cached_engine_health(self, port: int, health: dict, fresh_build: bool = False) -> bool:
        """Reject a cached-only port when it now belongs to a different engine/project.

        Fresh editor registrations are authoritative and replace the cache. When attaching
        without a build, an instance mismatch means the operating system reused the port.
        """
        cached = self._cached_engine_identity
        if not isinstance(cached, dict) or cached.get("port") != int(port):
            return True
        identity = health.get("identity", {}) if isinstance(health, dict) else {}
        if not isinstance(identity, dict):
            return not cached.get("engine_instance_id")
        cached_project = cached.get("project_identity")
        current_project = identity.get("project_identity")
        if cached_project and current_project and cached_project != current_project:
            self._record_lifecycle("cached_port_rejected", port=port, reason="project_identity_mismatch")
            return False
        cached_instance = cached.get("engine_instance_id")
        current_instance = identity.get("engine_instance_id")
        if not fresh_build and cached_instance and current_instance != cached_instance:
            self._record_lifecycle("cached_port_rejected", port=port, reason="engine_instance_mismatch")
            return False
        return True

    def _record_lifecycle(self, stage: str, **details: object) -> None:
        """Record an observable editor/bootstrap lifecycle transition."""
        event = {"stage": stage, "state": "completed", "monotonic": time.monotonic()}
        event.update(details)
        self._lifecycle_events.append(event)

    @property
    def lifecycle_events(self) -> list:
        """Return a copy of editor/bootstrap lifecycle events observed by this client."""
        return [dict(event) for event in self._lifecycle_events]

    def _remember_remotery_url(self, url: str) -> None:
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
    def _engine_identity_cache_path(self) -> Path:
        return self.root / ".internal" / "automation_bridge.engine.identity.json"

    def _read_cached_engine_identity(self) -> Optional[dict]:
        try:
            value = json.loads(self._engine_identity_cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, dict) else None

    def _write_cached_engine_identity(self, value: dict) -> None:
        path = self._engine_identity_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

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
