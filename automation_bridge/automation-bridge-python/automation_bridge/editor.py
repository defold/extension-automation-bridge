"""Public editor discovery, OpenAPI wrappers, and engine bootstrap."""

from __future__ import annotations

import configparser
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, TYPE_CHECKING, Union

from .client import AutomationBridgeError, HttpError, request_json, request_raw
from .preferences import PreferenceKey, Preferences
from .waits import WaitTimeoutError, wait_until
from .diagnostics import DiagnosticCheck, DoctorReport
from .cancellation import cancellable_sleep, check_cancelled

if TYPE_CHECKING:
    from .client import Client as EngineClient


_AUTOMATION_BRIDGE_ENDPOINT_TEXT = "Automation Bridge endpoint registered"
_AUTOMATION_BRIDGE_REPOSITORY = "https://github.com/defold/extension-automation-bridge"
_AUTOMATION_BRIDGE_LATEST_RELEASE_API = "https://api.github.com/repos/defold/extension-automation-bridge/releases/latest"
_AUTOMATION_BRIDGE_PYTHON_DIRECTORY = "automation-bridge-python"
_EDITOR_ABSENCE_GRACE_PERIOD = 5.0
_DEPENDENCY_LINE_PATTERN = re.compile(r"^(\s*dependencies#(\d+)\s*=\s*)(.*?)([ \t]*(?:\r\n|\n|\r)?)$")
_SECTION_PATTERN = re.compile(r"^\s*\[([^]]+)\]\s*(?:\r\n|\n|\r)?$")
_ENGINE_SERVICE_PORT_PATTERNS = (
    re.compile(r"Engine service started on port (\d+)"),
    re.compile(r"Log server started on port (\d+)"),
)
_REMOTERY_URL_PATTERN = re.compile(r"Initialized Remotery \((ws://[^)\s]+)\)")
_SUPPORTED_COMMANDS = frozenset({
    "build", "run", "compile", "clean-build", "build-html5", "fetch-libraries", "hot-reload",
    "rebundle", "reload-extensions", "reload-stylesheets", "debugger-start",
    "debugger-stop", "debugger-break", "debugger-continue", "debugger-detach",
    "debugger-step-into", "debugger-step-out", "debugger-step-over",
})
_SUPPORTED_PATHS = frozenset({
    ("/command/{command}", "post"),
    ("/bob", "post"),
    ("/console", "get"),
    ("/console/stream", "get"),
    ("/prefs/{path}", "get"),
    ("/prefs/{path}", "post"),
    ("/preview/{path}", "get"),
    ("/ref", "get"),
})
_EXCLUDED_COMMANDS = frozenset({
    "asset-portal", "documentation", "donate-page", "editor-logs",
    "engine-profiler", "engine-resource-profiler", "issues", "report-issue",
    "report-suggestion", "support-forum", "show-build-errors", "show-console",
    "show-curve-editor", "toggle-pane-bottom", "toggle-pane-left", "toggle-pane-right",
})
_EXCLUDED_PATHS = frozenset({("/eval", "post")})
_COMMAND_MINIMUM_VERSIONS = {"compile": "1.13.2", "run": "1.13.2"}


Error = AutomationBridgeError


class NotRunningError(Error):
    """Raised when no healthy editor is available for a project."""


class LaunchError(Error):
    """Raised when the Defold editor cannot be launched."""


class UnsupportedOperationError(Error):
    """Raised when the connected editor does not advertise an operation."""

    def __init__(self, message: str, *, minimum_version: Optional[str] = None):
        self.minimum_version = minimum_version
        if minimum_version is not None:
            message += f"; supported from Defold {minimum_version}. Upgrade the connected editor to use this feature"
        super().__init__(message)


class CommandError(Error):
    """Raised when an editor command is rejected or fails."""


class AutomationBridgeUpdateError(Error):
    """Raised when the project dependency or copied Python wrapper cannot be updated."""


class BuildError(CommandError):
    """A build operation failed; ``issues`` and ``result`` retain diagnostics."""

    def __init__(self, issues: Sequence["BuildIssue"], *, result: Optional["BuildResult"] = None):
        self.issues = tuple(issues)
        self.result = result
        super().__init__(f"Defold build failed: {list(self.issues)!r}")


def _macos_gui_launch_is_sandboxed() -> bool:
    """Return whether Codex marked this macOS process as Seatbelt-restricted."""
    return sys.platform == "darwin" and bool(os.environ.get("CODEX_SANDBOX"))


def _validate_automation_bridge_tag(tag: str) -> tuple[str, str]:
    if not isinstance(tag, str):
        raise TypeError("Automation Bridge version must be a string")
    normalized_tag = tag.strip()
    normalized_version = normalized_tag[1:] if normalized_tag.startswith("v") else normalized_tag
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", normalized_version):
        raise ValueError(
            "Automation Bridge version must be an exact release such as '2.1.0'"
        )
    return normalized_version, normalized_tag


def _resolve_automation_bridge_release(
    version: Optional[str],
    timeout: float,
) -> tuple[str, str]:
    if version is not None:
        return _validate_automation_bridge_tag(version)
    try:
        status, release = request_json(
            _AUTOMATION_BRIDGE_LATEST_RELEASE_API,
            timeout=min(float(timeout), 30.0),
        )
    except (AutomationBridgeError, OSError, TypeError, ValueError) as exc:
        raise AutomationBridgeUpdateError(
            f"cannot resolve the latest Automation Bridge release: {exc}"
        ) from exc
    if status < 200 or status >= 300:
        raise AutomationBridgeUpdateError(
            f"cannot resolve the latest Automation Bridge release: HTTP {status}"
        )
    tag = release.get("tag_name") if isinstance(release, Mapping) else None
    try:
        return _validate_automation_bridge_tag(tag)
    except (TypeError, ValueError) as exc:
        raise AutomationBridgeUpdateError(
            f"latest Automation Bridge release returned an invalid tag: {tag!r}"
        ) from exc


def _is_automation_bridge_dependency(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == "github.com"
        and parsed.path.startswith("/defold/extension-automation-bridge/")
    )


def _update_automation_bridge_dependency(text: str, dependency_url: str) -> tuple[str, Optional[str]]:
    lines = text.splitlines(keepends=True)
    section: Optional[str] = None
    project_start: Optional[int] = None
    project_end = len(lines)
    dependency_lines = []
    bridge_lines = []

    for index, line in enumerate(lines):
        section_match = _SECTION_PATTERN.match(line)
        if section_match:
            next_section = section_match.group(1).strip().lower()
            if section == "project" and next_section != "project":
                project_end = index
            section = next_section
            if section == "project":
                if project_start is not None:
                    raise ValueError("game.project contains more than one [project] section")
                project_start = index
            continue
        if section != "project":
            continue
        dependency_match = _DEPENDENCY_LINE_PATTERN.match(line)
        if not dependency_match:
            continue
        value = dependency_match.group(3).strip()
        dependency_lines.append((index, int(dependency_match.group(2))))
        if _is_automation_bridge_dependency(value):
            bridge_lines.append((index, dependency_match, value))

    if project_start is None:
        raise ValueError("game.project has no [project] section")
    if len(bridge_lines) > 1:
        raise ValueError("game.project contains multiple Automation Bridge dependencies")

    if bridge_lines:
        index, match, previous_url = bridge_lines[0]
        lines[index] = f"{match.group(1)}{dependency_url}{match.group(4)}"
        return "".join(lines), previous_url

    newline = "\r\n" if "\r\n" in text else "\n"
    next_number = max((number for _, number in dependency_lines), default=-1) + 1
    insertion = f"dependencies#{next_number} = {dependency_url}{newline}"
    insert_at = dependency_lines[-1][0] + 1 if dependency_lines else project_end
    if insert_at > 0 and lines and not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += newline
    lines.insert(insert_at, insertion)
    return "".join(lines), None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _automation_bridge_archive_path(project_root: Path, dependency_url: str) -> Path:
    library_directory = project_root / ".internal" / "lib"
    url_hash = hashlib.sha1(dependency_url.encode("utf-8")).hexdigest()
    candidates = [
        path
        for path in library_directory.glob(f"{url_hash}-*.zip")
        if path.is_file()
    ]
    if not candidates:
        plain_archive = library_directory / f"{url_hash}.zip"
        if plain_archive.is_file():
            candidates.append(plain_archive)
    if not candidates:
        raise AutomationBridgeUpdateError(
            f"Defold reported the dependency fetched, but its archive is missing from {library_directory}"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _read_project_configuration(root: Path) -> configparser.ConfigParser:
    configuration = configparser.ConfigParser(interpolation=None)
    try:
        configuration.read_string((root / "game.project").read_text(encoding="utf-8"))
    except configparser.Error as exc:
        raise ValueError(f"invalid game.project: {exc}") from exc
    return configuration


def _bridge_dependency(configuration: configparser.ConfigParser) -> str:
    values = configuration.items("project") if configuration.has_section("project") else ()
    dependencies = [value.strip() for key, value in values if key.startswith("dependencies#") and _is_automation_bridge_dependency(value.strip())]
    if not dependencies:
        raise AutomationBridgeUpdateError("Automation Bridge dependency is missing; configure one dependency")
    if len(dependencies) != 1:
        raise AutomationBridgeUpdateError("Automation Bridge dependency is ambiguous; configure exactly one dependency")
    return dependencies[0]


def _replace_python_wrapper(archive_path: Path, destination: Path) -> None:
    transaction_root = Path(
        tempfile.mkdtemp(prefix=".automation-bridge-update-", dir=destination.parent)
    )
    staged = transaction_root / _AUTOMATION_BRIDGE_PYTHON_DIRECTORY
    backup = transaction_root / "previous"
    moved_previous = False
    installed = False
    marker = "/automation_bridge/automation-bridge-python/"
    try:
        with zipfile.ZipFile(archive_path) as archive:
            selected = []
            for info in archive.infolist():
                normalized_name = "/" + info.filename.replace("\\", "/").lstrip("/")
                if marker not in normalized_name:
                    continue
                relative_text = normalized_name.split(marker, 1)[1]
                if not relative_text:
                    continue
                relative = Path(relative_text)
                if relative.is_absolute() or ".." in relative.parts:
                    raise AutomationBridgeUpdateError(
                        f"unsafe Python wrapper path in {archive_path}: {info.filename}"
                    )
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise AutomationBridgeUpdateError(
                        f"symbolic links are not allowed in the Python wrapper: {info.filename}"
                    )
                selected.append((info, relative))

            required = Path("automation_bridge") / "__init__.py"
            if not any(relative == required for _, relative in selected):
                raise AutomationBridgeUpdateError(
                    f"fetched archive does not contain {_AUTOMATION_BRIDGE_PYTHON_DIRECTORY}: {archive_path}"
                )

            for info, relative in selected:
                target = staged / relative
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    os.chmod(target, mode)

        if destination.exists():
            destination.rename(backup)
            moved_previous = True
        staged.rename(destination)
        installed = True
    except BaseException:
        if installed and destination.exists():
            shutil.rmtree(destination)
            installed = False
        if moved_previous and backup.exists():
            backup.rename(destination)
            moved_previous = False
        raise
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)


class PreviewError(Error):
    """Raised when an editor preview cannot be rendered."""


class PreferenceError(Error):
    """Raised when a preference request is invalid or rejected."""


@dataclass(frozen=True)
class SourcePosition:
    line: int
    character: int


@dataclass(frozen=True)
class SourceRange:
    start: SourcePosition
    end: SourcePosition


@dataclass(frozen=True)
class BuildIssue:
    """An editor diagnostic; source positions use zero-based LSP coordinates."""

    severity: str
    message: str
    resource: Optional[str] = None
    range: Optional[SourceRange] = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "BuildIssue":
        for name in ("severity", "message"):
            if not isinstance(raw.get(name), str):
                raise ValueError(f"build issue {name} must be a string")
        if "resource" in raw and not isinstance(raw["resource"], str):
            raise ValueError("build issue resource must be a string")
        raw_range = raw.get("range")
        source_range = None
        if "range" in raw:
            if not isinstance(raw_range, Mapping):
                raise ValueError("build issue range must be an object")
            positions = []
            for name in ("start", "end"):
                position = raw_range.get(name)
                if not isinstance(position, Mapping) or any(
                    type(position.get(key)) is not int or position[key] < 0
                    for key in ("line", "character")
                ):
                    raise ValueError(f"build issue range.{name} requires non-negative integer line and character")
                positions.append(SourcePosition(position["line"], position["character"]))
            source_range = SourceRange(*positions)
        return cls(
            severity=raw["severity"],
            message=raw["message"],
            resource=raw.get("resource"),
            range=source_range,
        )


@dataclass(frozen=True)
class BuildResult:
    """Editor completion evidence for a build or command.

    ``completed`` is true only when the editor returned a structured build
    result. Legacy text/empty acknowledgements have ``completed=False`` and
    ``success=None``. Warnings may accompany success. ``target_url`` is optional
    even after a successful launch. ``raw`` preserves additional response fields.
    """

    command: str
    status: int
    completed: bool
    success: Optional[bool]
    issues: tuple[BuildIssue, ...]
    target_url: Optional[str]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class LibraryResult:
    uri: str
    success: bool
    message: Optional[str] = None


@dataclass(frozen=True)
class FetchLibrariesResult:
    success: bool
    libraries: tuple[LibraryResult, ...]


@dataclass(frozen=True)
class AutomationBridgeUpdateResult:
    version: str
    dependency_url: str
    previous_dependency_url: Optional[str]
    dependency_was_present: bool
    dependency_changed: bool
    wrapper_path: Path
    wrapper_was_present: bool
    fetch: FetchLibrariesResult


@dataclass(frozen=True)
class ConsoleRegion:
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ConsoleSnapshot:
    lines: tuple[str, ...]
    regions: tuple[ConsoleRegion, ...]


class ConsoleStream:
    """Context-managed iterator over the editor's streaming console response."""

    def __init__(self, response: Any):
        self._response = response

    def __enter__(self) -> "ConsoleStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        line = self.readline()
        if line is None:
            raise StopIteration
        return line

    def readline(self, timeout: Optional[float] = None) -> Optional[str]:
        if self._response is None:
            return None
        if timeout is not None:
            try:
                self._response.fp.raw._sock.settimeout(timeout)
            except (AttributeError, OSError):
                pass
        try:
            line = self._response.readline()
        except TimeoutError:
            return None
        if not line:
            return None
        return line.decode("utf-8", "replace").rstrip("\r\n")

    def close(self) -> None:
        response = self._response
        self._response = None
        if response is not None:
            response.close()


@dataclass(frozen=True)
class Installation:
    """One Defold installation discovered through the editor registry."""

    launcher_path: Path
    install_path: Path
    last_launched_at: str


@dataclass(frozen=True)
class CommandInfo:
    """An advertised editor command supported by this wrapper.

    ``parameters`` retains OpenAPI parameter descriptions and schemas. Legacy
    command-enum metadata is normalized to a concrete ``path``; UI-only commands
    outside this wrapper's supported surface are excluded from discovery.
    """

    name: str
    path: str
    summary: str
    description: str
    parameters: tuple[Mapping[str, Any], ...]
    raw: Mapping[str, Any]


class Commands:
    def __init__(self, client: "Client"):
        self._client = client

    def catalog(self, *, refresh: bool = False) -> tuple[CommandInfo, ...]:
        """List supported commands from either editor OpenAPI format.

        The first call reads OpenAPI; later calls reuse it. Pass ``refresh=True``
        after editor capabilities change. Discovery does not execute commands.
        """
        if refresh:
            self._client._check_connection(timeout=10.0)
        return tuple(info for name in sorted(_SUPPORTED_COMMANDS) if (info := self._client._command_info(name)) is not None)

    def supports(self, command: str, *, parameter: Optional[str] = None) -> bool:
        """Check advertisement of a supported command or one of its query parameters."""
        info = self._client._command_info(command)
        return info is not None and (parameter is None or any(
            item.get("name") == parameter and item.get("in") == "query" for item in info.parameters
        ))

    def fetch_libraries(self, timeout: float = 60.0) -> FetchLibrariesResult:
        status, response = self._client._json_command("fetch-libraries", timeout)
        libraries = tuple(
            LibraryResult(
                uri=str(item.get("uri", "")),
                success=bool(item.get("success", False)),
                message=str(item["message"]) if item.get("message") is not None else None,
            )
            for item in response.get("libraries", ())
            if isinstance(item, Mapping)
        )
        result = FetchLibrariesResult(bool(response.get("success", False)), libraries)
        if status >= 400:
            raise CommandError(f"fetch-libraries failed: {response!r}")
        return result

    def hot_reload(self, timeout: float = 60.0) -> None:
        """Hot reload; inspect ``project.last_command_result`` for completion.

        Defold 1.13.2 waits and reports build issues. Defold 1.13.1 only
        acknowledges the request; acknowledgement does not establish completion.
        """
        self._client._empty_command("hot-reload", timeout)

    def rebundle(self, timeout: float = 60.0) -> None:
        self._client._empty_command("rebundle", timeout)

    def reload_extensions(self, timeout: float = 60.0) -> None:
        self._client._empty_command("reload-extensions", timeout)

    def reload_stylesheets(self, timeout: float = 60.0) -> None:
        self._client._empty_command("reload-stylesheets", timeout)


class Debugger:
    def __init__(self, client: "Client"):
        self._client = client

    def _run(self, name: str, timeout: float) -> None:
        self._client._empty_command(f"debugger-{name}", timeout)

    def start(self, timeout: float = 60.0) -> None:
        """Start debugging; Defold 1.13.2 reports completion in last_command_result."""
        self._run("start", timeout)

    def stop(self, timeout: float = 10.0) -> None:
        self._run("stop", timeout)

    def break_(self, timeout: float = 10.0) -> None:
        self._run("break", timeout)

    def continue_(self, timeout: float = 10.0) -> None:
        self._run("continue", timeout)

    def detach(self, timeout: float = 10.0) -> None:
        self._run("detach", timeout)

    def step_into(self, timeout: float = 10.0) -> None:
        self._run("step-into", timeout)

    def step_out(self, timeout: float = 10.0) -> None:
        self._run("step-out", timeout)

    def step_over(self, timeout: float = 10.0) -> None:
        self._run("step-over", timeout)


class Console:
    def __init__(self, client: "Client"):
        self._client = client

    def read(self) -> ConsoleSnapshot:
        self._client._require_operation("/console", "get")
        status, response = request_json(f"{self._client.base_url}/console", timeout=10.0)
        if status >= 400:
            raise HttpError("GET", f"{self._client.base_url}/console", str(response), status=status)
        return ConsoleSnapshot(
            tuple(str(line) for line in response.get("lines", ())),
            tuple(ConsoleRegion(region) for region in response.get("regions", ()) if isinstance(region, Mapping)),
        )

    def stream(
        self,
        *,
        connect_timeout: float = 10.0,
        read_timeout: Optional[float] = None,
    ) -> ConsoleStream:
        self._client._require_operation("/console/stream", "get")
        url = f"{self._client.base_url}/console/stream"
        try:
            response = urllib.request.urlopen(url, timeout=connect_timeout)
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError("GET", url, str(exc)) from exc
        stream = ConsoleStream(response)
        if read_timeout is not None:
            try:
                response.fp.raw._sock.settimeout(read_timeout)
            except (AttributeError, OSError):
                pass
        return stream


class Reference:
    def __init__(self, client: "Client"):
        self._client = client

    def search(
        self,
        *,
        environment: Optional[str] = None,
        language: Optional[str] = None,
        query: Optional[str] = None,
    ) -> list[dict]:
        self._client._require_operation("/ref", "get")
        params = {key: value for key, value in (("environment", environment), ("language", language), ("q", query)) if value is not None}
        url = f"{self._client.base_url}/ref"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, body = request_raw(url, timeout=10.0)
        if status >= 400:
            raise HttpError("GET", url, body.decode("utf-8", "replace"), status=status)
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, list):
            raise HttpError("GET", url, "reference response was not an array", status=status)
        return [dict(item) for item in value if isinstance(item, Mapping)]


class Preview:
    def __init__(self, client: "Client"):
        self._client = client

    def render(
        self,
        path: Union[str, Path],
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        resolution_multiplier: Optional[float] = None,
        timeout: float = 30.0,
    ) -> bytes:
        return self._client._render_preview(
            path,
            width=width,
            height=height,
            resolution_multiplier=resolution_multiplier,
            timeout=timeout,
        )


class Client:
    """Small wrapper around the Defold editor HTTP API for one project."""

    def __init__(self, root: Union[str, Path], port: Optional[int] = None):
        self.root = Path(root).resolve()
        if port is None:
            port = self._read_editor_port(self.root)
        self.port = int(port)
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._engine_service_port: Optional[int] = self._read_cached_engine_service_port()
        self._cached_engine_identity = self._read_cached_engine_identity()
        self._remotery_url: Optional[str] = self._read_cached_remotery_url()
        self._last_build_had_engine_service_port: Optional[bool] = None
        self._last_command_result: Optional[BuildResult] = None
        self._lifecycle_events = []
        self._openapi_document: Optional[dict] = None
        self.commands = Commands(self)
        self.debugger = Debugger(self)
        self.console = Console(self)
        self.reference = Reference(self)
        self.preview = Preview(self)
        self.preferences = Preferences(self)

    @property
    def last_command_result(self) -> Optional[BuildResult]:
        """Latest build/command result, including failures; None before a response.

        Build-and-run helpers still return an engine client. HTML5, hot reload,
        and debugger helpers retain their existing None return. Read this
        property immediately after those calls to inspect completion and issues.
        It is cleared when the next command is sent, including transport failure.
        """
        return self._last_command_result

    @staticmethod
    def _read_editor_port(project_root: Path) -> int:
        port_path = project_root / ".internal" / "editor.port"
        if not port_path.exists():
            raise FileNotFoundError(f"Defold editor port file is missing: {port_path}")
        return int(port_path.read_text(encoding="utf-8").strip())

    def update_automation_bridge(
        self,
        version: Optional[str] = None,
        *,
        timeout: float = 60.0,
    ) -> AutomationBridgeUpdateResult:
        """Install or update the bridge dependency and copied Python wrapper.

        Omit ``version`` to use GitHub's latest stable release, or provide an
        exact release to pin the project.

        The complete project-root ``automation-bridge-python`` directory is
        replaced from the fetched extension archive. Restart Python after this
        method returns before running automation through the updated wrapper.
        """
        normalized_version, release_tag = _resolve_automation_bridge_release(version, timeout)
        dependency_url = (
            f"{_AUTOMATION_BRIDGE_REPOSITORY}/archive/refs/tags/"
            f"{release_tag}.zip"
        )
        project_path = self.root / "game.project"
        try:
            original_project = project_path.read_bytes()
            project_text = original_project.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise AutomationBridgeUpdateError(f"cannot read {project_path}: {exc}") from exc

        try:
            updated_text, previous_url = _update_automation_bridge_dependency(
                project_text,
                dependency_url,
            )
        except ValueError as exc:
            raise AutomationBridgeUpdateError(f"cannot update {project_path}: {exc}") from exc

        updated_project = updated_text.encode("utf-8")
        dependency_changed = updated_project != original_project
        wrapper_path = self.root / _AUTOMATION_BRIDGE_PYTHON_DIRECTORY
        wrapper_was_present = wrapper_path.exists()
        if wrapper_path.is_symlink() or (wrapper_was_present and not wrapper_path.is_dir()):
            raise AutomationBridgeUpdateError(
                f"refusing to replace non-directory Python wrapper path: {wrapper_path}"
            )

        if dependency_changed:
            _atomic_write_bytes(project_path, updated_project)

        try:
            fetch = self.commands.fetch_libraries(timeout=timeout)
            matching_library = next(
                (library for library in fetch.libraries if library.uri == dependency_url),
                None,
            )
            if matching_library is not None and not matching_library.success:
                detail = f": {matching_library.message}" if matching_library.message else ""
                raise AutomationBridgeUpdateError(
                    f"Defold failed to fetch {dependency_url}{detail}"
                )
            if matching_library is None and not fetch.success:
                raise AutomationBridgeUpdateError(
                    f"Defold did not report a successful fetch for {dependency_url}"
                )

            archive_path = _automation_bridge_archive_path(self.root, dependency_url)
            _replace_python_wrapper(archive_path, wrapper_path)
        except BaseException as exc:
            if dependency_changed:
                try:
                    _atomic_write_bytes(project_path, original_project)
                except OSError as rollback_error:
                    raise AutomationBridgeUpdateError(
                        f"Automation Bridge update failed and {project_path} could not be restored: "
                        f"{rollback_error}"
                    ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit, AutomationBridgeUpdateError)):
                raise
            raise AutomationBridgeUpdateError(
                f"cannot install Automation Bridge {normalized_version}: {exc}"
            ) from exc

        return AutomationBridgeUpdateResult(
            version=normalized_version,
            dependency_url=dependency_url,
            previous_dependency_url=previous_url,
            dependency_was_present=previous_url is not None,
            dependency_changed=dependency_changed,
            wrapper_path=wrapper_path,
            wrapper_was_present=wrapper_was_present,
            fetch=fetch,
        )

    @classmethod
    def _open_project(
        cls,
        root: Union[str, Path] = ".",
        *,
        start_if_needed: bool = True,
        timeout: float = 30.0,
        launcher: Optional[Union[str, Path]] = None,
    ) -> "Client":
        """Connect to this project's editor, launching Defold when necessary."""
        check_cancelled()
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and greater than zero")
        project_root = Path(root).resolve()
        try:
            client = cls._wait_for_editor(
                project_root,
                timeout=timeout,
                allow_absent=start_if_needed,
                message=f"Could not connect to Defold editor for {project_root}",
            )
        except WaitTimeoutError as exc:
            raise NotRunningError(str(exc)) from exc
        if client is not None:
            client._record_lifecycle("editor_reused", port=client.port)
            return client

        project_file = project_root / "game.project"
        if not project_file.is_file():
            raise FileNotFoundError(f"Defold project file is missing: {project_file}")
        launcher_path = Path(launcher).expanduser().resolve() if launcher is not None else cls._latest_installation().launcher_path
        if not launcher_path.is_file():
            raise FileNotFoundError(f"Defold launcher is missing: {launcher_path}")
        if _macos_gui_launch_is_sandboxed():
            raise LaunchError(
                "Defold cannot be launched from this restricted macOS sandbox; "
                "start Defold manually or rerun the Automation Bridge bootstrap "
                "with escalated/unsandboxed execution"
            )
        try:
            process = subprocess.Popen(
                [str(launcher_path), str(project_file)],
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            raise LaunchError(f"cannot launch Defold from {launcher_path}: {exc}") from exc

        client = cls._wait_for_editor(
            project_root,
            timeout=timeout,
            message=f"Defold editor did not start for {project_root}",
        )
        assert client is not None
        client._record_lifecycle(
            "editor_started",
            launcher=str(launcher_path),
            process_id=process.pid,
            port=client.port,
        )
        return client

    @classmethod
    def _wait_for_editor(
        cls,
        project_root: Path,
        *,
        timeout: float,
        message: str,
        allow_absent: bool = False,
    ) -> Optional["Client"]:
        """Wait for discovery; return None after consistent evidence of absence."""
        started = time.monotonic()
        deadline = started + timeout
        absence_deadline = started + min(_EDITOR_ABSENCE_GRACE_PERIOD, timeout)
        absent = object()
        last_refused_port = None
        unresolved_error = None

        def ready():
            nonlocal last_refused_port, unresolved_error
            port = None
            try:
                # The editor can publish or replace its port while we wait.
                port = cls._read_editor_port(project_root)
                candidate = cls(project_root, port=port)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if allow_absent and unresolved_error is None and port == last_refused_port:
                        return absent
                    return None
                candidate._check_connection(timeout=remaining)
                return candidate
            except (FileNotFoundError, ValueError) as exc:
                failure = exc
                if port is not None:
                    unresolved_error = exc
            except HttpError as exc:
                failure = exc
                cause = exc.__cause__
                if isinstance(cause, urllib.error.URLError):
                    cause = cause.reason
                # Only a refused connection establishes that nobody is listening.
                # A timeout, denied connection, or HTTP error may be a live editor.
                if isinstance(cause, ConnectionRefusedError):
                    last_refused_port = port
                else:
                    unresolved_error = exc
            except OSError as exc:
                failure = exc
                unresolved_error = exc
            # Keep the failure that prevents a launch, even if later probes only
            # observe a missing file or a refused connection.
            if unresolved_error is not None:
                raise unresolved_error
            if allow_absent and time.monotonic() >= absence_deadline:
                return absent
            raise failure

        result = wait_until(
            ready,
            timeout=timeout,
            interval=0.1,
            message=message,
            retry_exceptions=(HttpError, OSError, ValueError),
        )
        return None if result is absent else result

    @classmethod
    def _installations(cls) -> list[Installation]:
        """Return registered Defold installations, newest launch first."""
        registry_path = cls._installation_registry_path()
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
                Installation(
                    launcher_path=launcher,
                    install_path=Path(install_path).expanduser(),
                    last_launched_at=last_launched_at,
                )
            )
        return sorted(installations, key=lambda installation: installation.last_launched_at, reverse=True)

    @classmethod
    def _latest_installation(cls) -> Installation:
        """Return the most recently launched registered Defold installation."""
        installations = cls._installations()
        if not installations:
            raise AutomationBridgeError(
                f"no Defold installation was found in {cls._installation_registry_path()}"
            )
        return installations[0]

    @staticmethod
    def _installation_registry_path() -> Path:
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

    def _is_running(self, timeout: float = 1.0) -> bool:
        """Return whether this project's recorded editor port serves the editor API."""
        try:
            self._check_connection(timeout=timeout)
            return True
        except AutomationBridgeError:
            return False

    def _check_connection(self, *, timeout: float) -> None:
        """Fetch the editor API, preserving the cause of failed discovery."""
        url = f"{self.base_url}/openapi.json"
        status, document = request_json(url, timeout=timeout)
        if status < 200 or status >= 300:
            raise HttpError("GET", url, f"HTTP {status}: {document}", status=status)
        self._openapi_document = document

    def _openapi(self) -> dict:
        if self._openapi_document is None:
            self._check_connection(timeout=10.0)
        return self._openapi_document

    def _require_operation(self, path: str, method: str) -> Mapping[str, Any]:
        if (path, method.lower()) not in _SUPPORTED_PATHS:
            raise UnsupportedOperationError(f"editor operation is outside the supported API: {method.upper()} {path}")
        operation = self._openapi().get("paths", {}).get(path, {}).get(method.lower())
        if not isinstance(operation, Mapping):
            raise UnsupportedOperationError(
                f"editor does not advertise {method.upper()} {path}",
                minimum_version="1.13.2" if path == "/bob" else None,
            )
        return operation

    def _command_info(self, command: str) -> Optional[CommandInfo]:
        if not isinstance(command, str) or command not in _SUPPORTED_COMMANDS:
            return None
        paths = self._openapi().get("paths", {})
        path = f"/command/{command}"
        path_item = paths.get(path, {})
        operation = path_item.get("post") if isinstance(path_item, Mapping) else None
        if not isinstance(operation, Mapping):
            path_item = paths.get("/command/{command}", {})
            operation = path_item.get("post") if isinstance(path_item, Mapping) else None
            if not isinstance(operation, Mapping):
                return None
            advertised = any(
                isinstance(item, Mapping) and item.get("name") == "command"
                and isinstance(item.get("schema"), Mapping)
                and command in item["schema"].get("enum", ())
                for item in (*path_item.get("parameters", ()), *operation.get("parameters", ()))
            )
            if not advertised:
                return None
        # Operation-level declarations override parameters shared by a path.
        parameters = {
            (item.get("name"), item.get("in")): dict(item)
            for item in (*path_item.get("parameters", ()), *operation.get("parameters", ()))
            if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item.get("name") != "command"
        }
        return CommandInfo(command, path, str(operation.get("summary", "")), str(operation.get("description", "")), tuple(parameters.values()), dict(operation))

    def _require_command(self, command: str) -> CommandInfo:
        info = self._command_info(command)
        if info is None:
            raise UnsupportedOperationError(
                f"editor does not advertise supported command {command!r}",
                minimum_version=_COMMAND_MINIMUM_VERSIONS.get(command),
            )
        return info

    def _empty_command(self, command: str, timeout: float) -> None:
        check_cancelled()
        self._require_command(command)
        url = f"{self.base_url}/command/{command}"
        self._last_command_result = None
        status, body = request_raw(url, method="POST", timeout=timeout)
        if body.strip() in (b"", b"200 OK", b"202 Accepted"):
            if status < 200 or status >= 300:
                raise CommandError(f"{command} failed with HTTP {status}")
            self._last_command_result = BuildResult(command, status, False, None, (), None, {})
            return
        try:
            response = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            if status < 200 or status >= 300:
                raise CommandError(f"{command} failed with HTTP {status}: {body.decode('utf-8', 'replace')}") from exc
            raise HttpError("POST", url, "invalid command result JSON", status=status) from exc
        self._accept_build_result(command, url, status, response)

    def _accept_build_result(self, command: str, url: str, status: int, response: Any) -> BuildResult:
        try:
            if not isinstance(response, Mapping) or type(response.get("success")) is not bool:
                raise ValueError("build result requires a boolean success")
            raw_issues = response.get("issues", [])
            if not isinstance(raw_issues, list) or any(not isinstance(item, Mapping) for item in raw_issues):
                raise ValueError("build result issues must be an array of objects")
            issues = tuple(BuildIssue.from_raw(item) for item in raw_issues)
            target_url = None
            if "target" in response:
                target = response["target"]
                if not isinstance(target, Mapping) or not isinstance(target.get("url"), str) or not target["url"]:
                    raise ValueError("build result target requires a non-empty URL string")
                target_url = target["url"]
        except ValueError as exc:
            raise HttpError("POST", url, str(exc), status=status) from exc
        result = BuildResult(command, status, True, response["success"], issues, target_url, dict(response))
        self._last_command_result = result
        if status < 200 or status >= 300 or not result.success:
            raise BuildError(issues, result=result)
        return result

    def _json_command(self, command: str, timeout: float) -> tuple[int, dict]:
        check_cancelled()
        self._require_command(command)
        self._last_command_result = None
        return request_json(f"{self.base_url}/command/{command}", method="POST", timeout=timeout)

    def compile(self, *, timeout: float = 60.0) -> BuildResult:
        """Compile project resources and Lua without launching or bundling.

        Supported from Defold 1.13.2. Older editors raise
        UnsupportedOperationError; this never falls back to a launching command.
        Return completion and diagnostics, or raise BuildError with the result.
        Use build_and_run() directly when a runtime is needed; it compiles too.
        """
        status, response = self._json_command("compile", timeout)
        return self._accept_build_result("compile", f"{self.base_url}/command/compile", status, response)

    def bob(
        self,
        *,
        options: Optional[Mapping[str, Any]] = None,
        commands: Sequence[str] = (),
        timeout: float = 300.0,
    ) -> BuildResult:
        """Build or bundle through the editor's Bob endpoint without launching.

        Supported from Defold 1.13.2. ``options`` uses Bob CLI keys without
        ``--``; repeatable values are arrays. For example, pass
        ``options={"platform": "wasm-web", "archive": True}`` and
        ``commands=("build", "bundle")`` to bundle HTML5. ``options={"help": True}``
        prints available options to the editor console. Bob decides output paths
        and defaults. Output is available through ``project.console.read()``.

        Authentication reads ``.internal/editor.token`` on every call. Missing
        or rejected credentials raise CommandError. Requests are not retried:
        after a timeout, inspect the console before repeating a build. Return
        completion and diagnostics, or raise BuildError with the result.
        """
        if options is not None and (not isinstance(options, Mapping) or any(not isinstance(key, str) for key in options)):
            raise ValueError("Bob options must be an object with string keys")
        if not isinstance(commands, Sequence) or isinstance(commands, (str, bytes)) or any(not isinstance(command, str) for command in commands):
            raise ValueError("Bob commands must be a sequence of strings")
        body = {"options": dict(options) if options is not None else {}, "commands": list(commands)}
        # Validate before authentication or execution, including non-finite values.
        try:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Bob options must contain valid JSON values") from exc
        check_cancelled()
        self._require_operation("/bob", "post")
        token_path = self.root / ".internal" / "editor.token"
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise CommandError(f"cannot read editor authentication token at {token_path}; reopen the project in Defold 1.13.2 or later") from None
        if not token or any(char.isspace() or not 33 <= ord(char) <= 126 for char in token):
            raise CommandError(f"invalid editor authentication token at {token_path}; reopen the project")
        self._last_command_result = None
        url = f"{self.base_url}/bob"
        try:
            status, response = request_json(
                url, method="POST", timeout=timeout, data=data,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            )
        except HttpError as exc:
            if exc.status not in (401, 403):
                raise
            raise CommandError("Bob authentication was rejected; reconnect to the editor and check .internal/editor.token") from None
        if status in (401, 403):
            raise CommandError("Bob authentication was rejected; reconnect to the editor and check .internal/editor.token")
        return self._accept_build_result("bob", url, status, response)

    def _run_focus(self, command: str, focus: Optional[bool]) -> Optional[bool]:
        if focus is not None and type(focus) is not bool:
            raise ValueError("focus must be a boolean or None")
        self._require_command(command)
        if self.commands.supports(command, parameter="focus"):
            return False if focus is None else focus
        if focus is False:
            raise UnsupportedOperationError(
                f"editor does not advertise focus control for {command!r}",
                minimum_version="1.13.2",
            )
        # Legacy commands launch with focus. Do not send parameters they ignore.
        return None

    def connect_engine(
        self,
        *,
        timeout: float = 20.0,
        required_capabilities: Sequence[str] = (),
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> EngineClient:
        """Attach to the registered engine without taking lifecycle ownership.

        Explicit client/session IDs let one logical automation session reconnect.
        Use distinct IDs for independent agents; the native input lease remains
        exclusive. Closing the client leaves the engine running.
        """
        from .client import Client as EngineClient

        return EngineClient._from_editor(
            self,
            build_command=None,
            timeout=timeout,
            required_capabilities=required_capabilities,
            client_id=client_id,
            session_id=session_id,
        )


    def build_and_run(
        self,
        *,
        timeout: float = 60.0,
        focus: Optional[bool] = None,
        required_capabilities: Sequence[str] = (),
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> EngineClient:
        """Compile, launch, and return a client with owns_engine=True.

        Uses ``run`` on Defold 1.13.2 and ``build`` on 1.13.1. Omitted focus
        defaults to False when the editor advertises focus control, otherwise
        preserves the legacy focused launch. Explicit ``focus=False`` requires
        Defold 1.13.2. ``focus=True`` requests the native focused launch on both.
        Unsupported requests fail before closing any engine. Build evidence is
        available in ``last_command_result``; no separate compile is needed.

        Explicit client/session IDs let one logical automation session reconnect.
        Use distinct IDs for independent agents; the native input lease remains
        exclusive. Closing the client leaves the engine running.
        """
        from .client import Client as EngineClient

        if focus is not None and type(focus) is not bool:
            raise ValueError("focus must be a boolean or None")
        command = "run" if self.commands.supports("run") else "build"
        negotiated_focus = self._run_focus(command, focus)
        return EngineClient._from_editor(
            self,
            build_command=command,
            timeout=timeout,
            required_capabilities=required_capabilities,
            client_id=client_id,
            session_id=session_id,
            **({"focus": negotiated_focus} if negotiated_focus is not None else {}),
        )


    def clean_build_and_run(
        self,
        *,
        timeout: float = 60.0,
        required_capabilities: Sequence[str] = (),
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> EngineClient:
        """Clear build caches, rebuild, launch, and return an owned engine client.

        Use only to recover from stale build caches. The native clean-build
        command launches with focus on both Defold 1.13.1 and 1.13.2.

        Explicit client/session IDs let one logical automation session reconnect.
        Use distinct IDs for independent agents; the native input lease remains
        exclusive. Closing the client leaves the engine running.
        """
        from .client import Client as EngineClient

        self._require_command("clean-build")
        return EngineClient._from_editor(
            self,
            build_command="clean-build",
            timeout=timeout,
            required_capabilities=required_capabilities,
            client_id=client_id,
            session_id=session_id,
        )


    def build_and_run_html5(self, *, timeout: float = 60.0) -> None:
        """Build and launch HTML5; inspect last_command_result for completion.

        Defold 1.13.1 only acknowledges this request. Defold 1.13.2 waits for
        completion and raises BuildError with source diagnostics on failure.
        """
        self._empty_command("build-html5", timeout)

    def _build_and_run_command(self, command: str, timeout: float = 60.0, *, focus: Optional[bool] = None) -> BuildResult:
        """Execute a desktop build-and-run command and await endpoint registration."""
        check_cancelled()
        if command not in {"build", "run", "clean-build"}:
            raise ValueError(f"unsupported desktop build-and-run command: {command}")
        self._require_command(command)
        url = f"{self.base_url}/command/{command}"
        if command == "run" or focus is not None:
            negotiated_focus = self._run_focus(command, focus)
            if negotiated_focus is not None:
                url += "?" + urllib.parse.urlencode({"focus": "true" if negotiated_focus else "false"})
        self._record_lifecycle("editor_build_started")
        previous_lines = self._console_lines()
        previous_registration_count = self._endpoint_registered_count(previous_lines)
        previous_registration_ports = self._latest_registration_engine_service_ports(previous_lines)
        previous_port = self._engine_service_port_value()
        if previous_port is not None:
            self._engine_service_port = previous_port
        self._last_command_result = None
        try:
            status, response = request_json(url, method="POST", timeout=timeout)
            result = self._accept_build_result(command, url, status, response)
        except Exception as exc:
            self._record_lifecycle("editor_build_failed", error=str(exc))
            raise
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
        cancellable_sleep(0.2)
        return result

    def _console_lines(self) -> list:
        """Return current editor console lines."""
        return list(self.console.read().lines)

    def _render_preview(
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
        self._require_operation("/preview/{path}", "get")
        encoded_path = urllib.parse.quote(project_path, safe="/")
        url = f"{self.base_url}/preview/{encoded_path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, body = request_raw(url, timeout=timeout)
        if status < 200 or status >= 300:
            message = body[:300].decode("utf-8", "replace").strip() or f"unexpected status {status}"
            raise PreviewError(f"GET {url} failed: {message}")
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise PreviewError(f"GET {url} failed: editor preview did not return a PNG")
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

    def _engine_service_port_value(self) -> Optional[int]:
        """Return the service port before endpoint registration, or the cached reused port."""
        ports = self._engine_service_ports()
        return ports[0] if ports else None

    def _cached_engine_service_port_value(self) -> Optional[int]:
        """Return a cached port only when it has enough identity data for validation."""
        port = self._engine_service_port
        cached = self._cached_engine_identity
        if port is None or port <= 0 or not isinstance(cached, dict):
            return None
        if cached.get("port") != port:
            return None
        for key in ("engine_instance_id", "project_identity"):
            value = cached.get(key)
            if not isinstance(value, str) or not value:
                return None
        return port

    def _engine_service_ports(self, lines: Optional[list] = None) -> list:
        """Return candidate engine service ports from current logs, then the cached port."""
        candidates = self._current_registration_engine_service_ports(lines)

        if self._engine_service_port is not None:
            self._append_port_candidate(candidates, self._engine_service_port)
        return candidates

    def _current_registration_engine_service_ports(self, lines: Optional[list] = None) -> list:
        """Return ports logged before the latest Automation Bridge endpoint registration only."""
        if lines is None:
            lines = self._console_lines()
        return self._latest_registration_engine_service_ports(lines)

    def _cached_remotery_url_value(self) -> Optional[str]:
        """Return the cached Remotery URL without reading the editor console."""
        return self._remotery_url

    def _remotery_url_value(self, lines: Optional[list] = None) -> Optional[str]:
        """Return the Remotery websocket URL from current logs, then the cached URL."""
        urls = self._remotery_urls(lines)
        return urls[0] if urls else None

    def _remotery_urls(self, lines: Optional[list] = None) -> list:
        """Return candidate Remotery websocket URLs from current logs, then the cached URL."""
        candidates = self._current_registration_remotery_urls(lines)
        if self._remotery_url is not None:
            self._append_unique_candidate(candidates, self._remotery_url)
        return candidates

    def _current_registration_remotery_urls(self, lines: Optional[list] = None) -> list:
        """Return Remotery websocket URLs logged before the latest endpoint registration only."""
        if lines is None:
            lines = self._console_lines()
        return self._latest_registration_remotery_urls(lines)

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
        lines = self._console_lines()
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
        process_id: Optional[int] = None,
    ) -> None:
        """Remember a validated port and identity so stale port reuse is detectable."""
        self._engine_service_port = int(port)
        self._write_cached_engine_service_port(self._engine_service_port)
        self._cached_engine_identity = {
            "port": self._engine_service_port,
            "engine_instance_id": engine_instance_id,
            "project_identity": project_identity,
            "process_id": process_id,
        }
        self._write_cached_engine_identity(self._cached_engine_identity)

    def _validate_cached_engine_health(self, port: int, health: dict, fresh_build: bool = False) -> bool:
        """Reject a cached-only port when it now belongs to a different engine/project.

        Fresh editor registrations are authoritative and replace the cache. When attaching
        without a build, accept an instance change only when the process is unchanged,
        which identifies a normal editor-triggered engine reboot.
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
            cached_process = cached.get("process_id")
            current_process = identity.get("process_id")
            if cached_process is not None and cached_process == current_process:
                return True
            self._record_lifecycle("cached_port_rejected", port=port, reason="engine_instance_mismatch")
            return False
        return True

    def _cached_engine_health_matches(self, port: int, health: dict) -> bool:
        """Return whether health describes the exact engine identity stored for this port."""
        cached = self._cached_engine_identity
        identity = health.get("identity", {}) if isinstance(health, dict) else {}
        if not isinstance(cached, dict) or not isinstance(identity, dict):
            return False
        if cached.get("port") != int(port):
            return False
        for key in ("engine_instance_id", "project_identity"):
            cached_value = cached.get(key)
            if not cached_value or identity.get(key) != cached_value:
                return False
        cached_process = cached.get("process_id")
        if cached_process is not None and identity.get("process_id") != cached_process:
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


def is_running(root: Union[str, Path] = ".", *, timeout: float = 1.0) -> bool:
    """Check the recorded editor endpoint once; use open_project for retries."""
    try:
        return Client(root)._is_running(timeout=timeout)
    except (AutomationBridgeError, FileNotFoundError, OSError, ValueError):
        return False


def doctor(
    project_path: Union[str, Path] = ".",
    *,
    required_capabilities: Sequence[str] = (),
    timeout: float = 2.0,
) -> DoctorReport:
    """Inspect setup, versions, capabilities and connections without launching.

    ``timeout`` bounds each network probe. No build, install, project edit,
    input acquisition, background log collector or cache write is performed.
    Inspect ``report.checks`` for actionable failures, or serialize with
    ``report.as_dict()``. ``ready`` requires a compatible running engine.
    """
    from .diagnostics import inspect_project
    return inspect_project(project_path, required_capabilities=required_capabilities, timeout=timeout)


def update_python_wrapper(project_path: Union[str, Path] = ".") -> Path:
    """Update the project's Python wrapper from its already-fetched dependency.

    No editor or network connection is required. Fetch Libraries first, then
    run this helper from an extension checkout or the standalone install.py.
    It atomically replaces the complete managed automation-bridge-python
    directory; keep project scripts elsewhere and restart Python afterward.
    The dependency URL and other project settings remain unchanged.
    """
    root = Path(project_path).expanduser().resolve()
    dependency = _bridge_dependency(_read_project_configuration(root))
    destination = root / _AUTOMATION_BRIDGE_PYTHON_DIRECTORY
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise AutomationBridgeUpdateError(f"refusing to replace non-directory Python wrapper path: {destination}")
    _replace_python_wrapper(_automation_bridge_archive_path(root, dependency), destination)
    return destination


def open_project(
    root: Union[str, Path] = ".",
    *,
    start_if_needed: bool = True,
    timeout: float = 30.0,
    launcher: Optional[Union[str, Path]] = None,
) -> Client:
    """Open or reuse a Defold editor for one project.

    Discovery retries for up to ``timeout`` seconds, rereading the port file
    between attempts. Each HTTP request can use the remaining discovery time.
    ``timeout`` must be finite and greater than zero.

    With ``start_if_needed=True``, a missing/invalid port file or refused
    connection gets up to five seconds (bounded by ``timeout``) to recover
    before launching Defold. Startup then has its own ``timeout`` budget.
    Other connection failures are retried for the full discovery timeout and
    raise ``NotRunningError`` with diagnostics instead of launching a duplicate.
    ``start_if_needed=False`` always waits for discovery without launching.
    """
    return Client._open_project(
        root,
        start_if_needed=start_if_needed,
        timeout=timeout,
        launcher=launcher,
    )


def installations() -> list[Installation]:
    return Client._installations()


def latest_installation() -> Installation:
    return Client._latest_installation()


def installation_registry_path() -> Path:
    return Client._installation_registry_path()


__all__ = [
    "AutomationBridgeUpdateError",
    "AutomationBridgeUpdateResult",
    "BuildError",
    "BuildIssue",
    "BuildResult",
    "Client",
    "CommandError",
    "CommandInfo",
    "Commands",
    "Console",
    "ConsoleRegion",
    "ConsoleSnapshot",
    "ConsoleStream",
    "Debugger",
    "DiagnosticCheck",
    "DoctorReport",
    "Error",
    "FetchLibrariesResult",
    "HttpError",
    "Installation",
    "LaunchError",
    "LibraryResult",
    "NotRunningError",
    "PreferenceError",
    "PreferenceKey",
    "Preferences",
    "Preview",
    "PreviewError",
    "Reference",
    "SourcePosition",
    "SourceRange",
    "UnsupportedOperationError",
    "installation_registry_path",
    "doctor",
    "installations",
    "is_running",
    "latest_installation",
    "open_project",
    "update_python_wrapper",
]
