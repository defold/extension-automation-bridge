"""Non-launching diagnostics for Automation Bridge project setup."""

import ast
from dataclasses import asdict, dataclass
import math
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Union


@dataclass(frozen=True)
class DiagnosticCheck:
    """One named check with status ok, warning, or error and a suggested action."""

    name: str
    status: str
    message: str
    action: Optional[str] = None


@dataclass(frozen=True)
class DoctorReport:
    """Setup evidence collected without launching, building, or updating a project.

    ``ready`` means a compatible engine was reached. ``ok`` means no check
    failed; warnings such as an absent installation registry may coexist with
    a healthy running editor. Versions are reported independently because
    native and Python package version numbers are not required to be equal.
    """

    project_path: Path
    checks: Tuple[DiagnosticCheck, ...]
    python_package_version: str
    installed_python_version: Optional[str] = None
    health: Optional[Mapping[str, Any]] = None

    @property
    def ok(self) -> bool:
        return all(check.status != "error" for check in self.checks)

    @property
    def ready(self) -> bool:
        return self.health is not None and self.ok

    def as_dict(self) -> dict:
        """Return a JSON-serializable report for CLIs and agent adapters."""
        result = asdict(self)
        result["project_path"] = str(self.project_path)
        result.update(ok=self.ok, ready=self.ready)
        return result


def _wrapper_version(path: Path) -> str:
    # Read metadata without importing or executing a project's copied package.
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PYTHON_PACKAGE_VERSION"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ValueError("wrapper does not declare PYTHON_PACKAGE_VERSION")


def inspect_project(
    project_path: Union[str, Path],
    *,
    required_capabilities: Sequence[str] = (),
    timeout: float = 2.0,
) -> DoctorReport:
    from . import editor
    from .client import Client, PYTHON_PACKAGE_VERSION

    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("diagnostic timeout must be finite and greater than zero")
    root = Path(project_path).expanduser().resolve()
    checks = []
    installed_version = None
    health = None

    def report() -> DoctorReport:
        return DoctorReport(root, tuple(checks), PYTHON_PACKAGE_VERSION, installed_version, health)

    try:
        configuration = editor._read_project_configuration(root)
    except (OSError, ValueError) as exc:
        checks.append(DiagnosticCheck("project", "error", str(exc), "Select a directory containing a valid game.project."))
        return report()
    checks.append(DiagnosticCheck("project", "ok", str(root / "game.project")))
    local_extension = (root / "automation_bridge" / "ext.manifest").is_file()
    try:
        dependency = editor._bridge_dependency(configuration)
    except editor.AutomationBridgeUpdateError as exc:
        if local_extension and "missing" in str(exc):
            checks.append(DiagnosticCheck("dependency", "ok", "Using the local native extension."))
        else:
            checks.append(DiagnosticCheck("dependency", "error", str(exc), "Add one Automation Bridge dependency and Fetch Libraries."))
    else:
        try:
            archive = editor._automation_bridge_archive_path(root, dependency)
            checks.append(DiagnosticCheck("dependency", "ok", str(archive)))
        except editor.AutomationBridgeUpdateError as exc:
            checks.append(DiagnosticCheck("dependency", "warning", str(exc), "Run Defold's Fetch Libraries command."))

    installed = root / "automation-bridge-python" / "automation_bridge" / "client.py"
    if local_extension and not installed.exists():
        installed = root / "automation_bridge" / "automation-bridge-python" / "automation_bridge" / "client.py"
    try:
        installed_version = _wrapper_version(installed)
        same = installed_version == PYTHON_PACKAGE_VERSION
        checks.append(DiagnosticCheck(
            "python_wrapper", "ok" if same else "warning",
            f"Loaded {PYTHON_PACKAGE_VERSION}; project copy {installed_version}.",
            None if same else "Use the complete project wrapper and restart Python after installation or update.",
        ))
    except (OSError, ValueError, SyntaxError) as exc:
        checks.append(DiagnosticCheck("python_wrapper", "warning", str(exc), "Run editor.install_python(project_path) after Fetch Libraries."))

    try:
        available = editor.installations()
        checks.append(DiagnosticCheck(
            "editor_installation", "ok" if available else "warning",
            f"Found {len(available)} registered Defold installations.",
            None if available else "Install Defold or open the project manually.",
        ))
    except (OSError, ValueError, editor.Error) as exc:
        checks.append(DiagnosticCheck("editor_installation", "warning", str(exc), "Open the project manually if automatic launcher discovery is unavailable."))

    try:
        project = editor.Client(root)
        project._check_connection(timeout=timeout)
        checks.append(DiagnosticCheck("editor_connection", "ok", f"Editor responds on port {project.port}."))
    except (OSError, ValueError, editor.Error) as exc:
        checks.append(DiagnosticCheck(
            "editor_connection", "error", str(exc),
            "Open this project in Defold outside a restricted sandbox, then probe again; check local connection permissions if it is already running.",
        ))
        return report()

    errors = []
    cached_port = project._cached_engine_service_port_value()
    if cached_port is not None:
        try:
            candidate = Client(cached_port, timeout=timeout, required_capabilities=required_capabilities)
            observed = candidate.health()
            if project._cached_engine_health_matches(cached_port, observed):
                health = observed
            else:
                errors.append("Cached engine identity no longer matches.")
        except editor.Error as exc:
            errors.append(str(exc))
    if health is None:
        try:
            status, console = editor.request_json(f"{project.base_url}/console", timeout=timeout)
            if status < 200 or status >= 300:
                raise editor.HttpError("GET", f"{project.base_url}/console", str(console), status=status)
            lines = console.get("lines", [])
            for port in project._current_registration_engine_service_ports(lines)[:8]:
                try:
                    observed = Client(port, timeout=timeout, required_capabilities=required_capabilities).health()
                    if project._validate_cached_engine_health(port, observed, fresh_build=True):
                        health = observed
                        break
                    errors.append(f"Engine identity mismatch on port {port}.")
                except editor.Error as exc:
                    errors.append(str(exc))
        except (editor.Error, OSError, ValueError) as exc:
            errors.append(str(exc))
    checks.append(DiagnosticCheck(
        "engine_connection", "ok" if health is not None else "error",
        "Compatible engine and required capabilities verified." if health is not None else "; ".join(errors) or "No engine registered in this project's editor console.",
        None if health is not None else "Build and run a debug game with the matching extension; inspect the reported API/capability or connection error.",
    ))
    return report()
