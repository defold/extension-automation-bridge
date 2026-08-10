#!/usr/bin/env python3
"""Dependency-free structural and semantic validation for this plugin bundle."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PLUGIN_NAME = "automation-bridge"
PLUGIN_NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
PLUGIN_KEYS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
AUTHOR_KEYS = {"name", "email", "url"}
STDIO_KEYS = {"type", "command", "args", "env", "cwd"}
REMOTE_KEYS = {"type", "url", "headers"}
IGNORED_PARTS = {".DS_Store", "__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Automation Bridge plugin directory.",
    )
    parser.add_argument(
        "--allow-missing-python",
        action="store_true",
        help="Permit an unsynced development scaffold without python/.",
    )
    parser.add_argument(
        "--check-sync",
        action="store_true",
        help="Require python/ to exactly match the canonical repository wrapper.",
    )
    return parser.parse_args()


def load_object(path: Path, errors: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"missing file: {path}")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON in {path}: {exc}")
        return None
    if not isinstance(payload, dict):
        errors.append(f"JSON root must be an object: {path}")
        return None
    return payload


def require_keys(
    payload: dict[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    label: str,
    errors: list[str],
) -> None:
    for key in sorted(required - set(payload)):
        errors.append(f"{label} is missing required field {key!r}")
    for key in sorted(set(payload) - allowed):
        errors.append(f"{label} has unknown field {key!r}")


def validate_portable_manifest(payload: dict[str, Any], errors: list[str]) -> None:
    require_keys(
        payload,
        required={"$schema", "name"},
        allowed=PLUGIN_KEYS,
        label="plugin.json",
        errors=errors,
    )
    if payload.get("$schema") != PLUGIN_SCHEMA:
        errors.append("plugin.json targets the wrong Agent Plugins schema")
    name = payload.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 64):
        errors.append("plugin.json name must be a 1-64 character string")
    elif PLUGIN_NAME_PATTERN.fullmatch(name) is None:
        errors.append("plugin.json name violates Agent Plugins naming rules")
    if name != PLUGIN_NAME:
        errors.append(f"plugin.json name must be {PLUGIN_NAME!r}")
    for key in ("version", "description", "homepage", "repository", "license"):
        if key in payload and not isinstance(payload[key], str):
            errors.append(f"plugin.json field {key!r} must be a string")
    author = payload.get("author")
    if author is not None:
        if not isinstance(author, dict):
            errors.append("plugin.json field 'author' must be an object")
        else:
            require_keys(
                author,
                required=set(),
                allowed=AUTHOR_KEYS,
                label="plugin.json author",
                errors=errors,
            )
            for key, value in author.items():
                if not isinstance(value, str):
                    errors.append(f"plugin.json author field {key!r} must be a string")
    keywords = payload.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or not all(isinstance(value, str) for value in keywords)
    ):
        errors.append("plugin.json field 'keywords' must be an array of strings")
    extensions = payload.get("extensions")
    if extensions is not None and (
        not isinstance(extensions, dict)
        or not all(isinstance(value, dict) for value in extensions.values())
    ):
        errors.append("plugin.json field 'extensions' must map namespaces to objects")


def validate_stdio_server(
    server: dict[str, Any], label: str, plugin_root: Path, errors: list[str]
) -> None:
    require_keys(
        server,
        required={"type", "command"},
        allowed=STDIO_KEYS,
        label=label,
        errors=errors,
    )
    command = server.get("command")
    if not isinstance(command, str) or not command:
        errors.append(f"{label} command must be a non-empty executable token")
    elif any(character.isspace() for character in command):
        errors.append(f"{label} command must be one executable token")
    elif "/" in command or "\\" in command:
        if not command.startswith("./"):
            errors.append(f"{label} command path must begin with './'")
        else:
            validate_contained_path(plugin_root, command, f"{label} command", errors)
    args = server.get("args")
    if args is not None and (
        not isinstance(args, list) or not all(isinstance(value, str) for value in args)
    ):
        errors.append(f"{label} args must be an array of strings")
    env = server.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            errors.append(f"{label} env must map strings to strings")
        elif {"PLUGIN_ROOT", "PLUGIN_DATA"} & set(env):
            errors.append(f"{label} env must not set reserved plugin variables")
    cwd = server.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            errors.append(f"{label} cwd must be a string")
        elif cwd.startswith("./"):
            validate_contained_path(plugin_root, cwd, f"{label} cwd", errors)
        elif not (
            cwd == "${PLUGIN_ROOT}"
            or cwd.startswith("${PLUGIN_ROOT}/")
            or cwd == "${PLUGIN_DATA}"
            or cwd.startswith("${PLUGIN_DATA}/")
        ):
            errors.append(f"{label} cwd is not rooted in the plugin or plugin data directory")


def validate_portable_mcp(
    payload: dict[str, Any], plugin_root: Path, errors: list[str]
) -> None:
    require_keys(
        payload,
        required={"$schema", "mcpServers"},
        allowed={"$schema", "mcpServers"},
        label="mcp.json",
        errors=errors,
    )
    if payload.get("$schema") != MCP_SCHEMA:
        errors.append("mcp.json targets the wrong Agent Plugins schema")
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        errors.append("mcp.json field 'mcpServers' must be an object")
        return
    for name, server in servers.items():
        label = f"mcp.json server {name!r}"
        if not isinstance(name, str) or not name:
            errors.append("mcp.json server names must be non-empty strings")
        if not isinstance(server, dict):
            errors.append(f"{label} must be an object")
            continue
        server_type = server.get("type")
        if server_type == "stdio":
            validate_stdio_server(server, label, plugin_root, errors)
        elif server_type in {"streamable-http", "sse"}:
            require_keys(
                server,
                required={"type", "url"},
                allowed=REMOTE_KEYS,
                label=label,
                errors=errors,
            )
        else:
            errors.append(f"{label} has unsupported type {server_type!r}")
    expected = servers.get(PLUGIN_NAME)
    if isinstance(expected, dict):
        if expected.get("command") != "python3":
            errors.append("portable automation-bridge server must use command 'python3'")
        if expected.get("args") != ["${PLUGIN_ROOT}/scripts/run_mcp.py"]:
            errors.append("portable automation-bridge args must use the bundled launcher")
        if expected.get("cwd") != "${PLUGIN_ROOT}":
            errors.append("portable automation-bridge cwd must be ${PLUGIN_ROOT}")
        if expected.get("env") != {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }:
            errors.append("portable automation-bridge env must enforce UTF-8 unbuffered stdio")
    else:
        errors.append("mcp.json is missing the automation-bridge server")

    for path, value in walk_strings(payload):
        if "${PLUGIN_ROOT}" not in value:
            continue
        allowed = (
            len(path) >= 3
            and path[0] == "mcpServers"
            and (
                (path[2] == "cwd" and len(path) == 3)
                or (path[2] in {"args", "env"} and len(path) >= 4)
            )
        )
        if not allowed:
            errors.append(f"PLUGIN_ROOT is not allowed at mcp.json path {format_path(path)}")


def validate_native_files(
    manifest: dict[str, Any], mcp: dict[str, Any], portable: dict[str, Any], errors: list[str]
) -> None:
    if manifest.get("name") != PLUGIN_NAME:
        errors.append("Codex manifest name does not match the portable manifest")
    if manifest.get("version") != portable.get("version"):
        errors.append("Codex and portable plugin versions do not match")
    if manifest.get("skills") != "./skills/":
        errors.append("Codex manifest must discover skills from './skills/'")
    if manifest.get("mcpServers") != "./.mcp.json":
        errors.append("Codex manifest must reference './.mcp.json'")
    servers = mcp.get("mcpServers")
    if set(mcp) != {"mcpServers"} or not isinstance(servers, dict):
        errors.append(".mcp.json must contain only an mcpServers object")
        return
    server = servers.get(PLUGIN_NAME)
    if not isinstance(server, dict):
        errors.append(".mcp.json is missing the automation-bridge server")
        return
    if server.get("command") != "python3":
        errors.append("Codex-native MCP server must use command 'python3'")
    if server.get("args") != ["scripts/run_mcp.py"]:
        errors.append("Codex-native MCP args must use the relative launcher")
    if server.get("cwd") != ".":
        errors.append("Codex-native MCP cwd must be '.'")
    if server.get("env") != {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }:
        errors.append("Codex-native MCP env must enforce UTF-8 unbuffered stdio")
    if "${PLUGIN_ROOT}" in json.dumps(mcp):
        errors.append("PLUGIN_ROOT expansion must not appear in native .mcp.json")


def validate_skill(plugin_root: Path, errors: list[str]) -> None:
    skills_root = plugin_root / "skills"
    expected = skills_root / "defold-automation" / "SKILL.md"
    if not expected.is_file():
        errors.append(f"missing Agent Skill: {expected}")
        return
    for child in skills_root.iterdir():
        if child.is_dir() and not (child / "SKILL.md").is_file():
            errors.append(f"skill directory lacks exact uppercase SKILL.md: {child}")
    text = expected.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        errors.append("defold-automation SKILL.md lacks YAML frontmatter")
        return
    end = text.find("\n---\n", 4)
    if end < 0:
        errors.append("defold-automation SKILL.md has unclosed YAML frontmatter")
        return
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            errors.append(f"invalid SKILL.md frontmatter line: {line!r}")
            continue
        fields[key.strip()] = value.strip()
    if set(fields) != {"name", "description"}:
        errors.append("SKILL.md frontmatter must contain only name and description")
    if fields.get("name") != "defold-automation":
        errors.append("SKILL.md name must match its directory")
    if not fields.get("description"):
        errors.append("SKILL.md description must be non-empty")
    if "[TODO:" in text:
        errors.append("SKILL.md still contains a TODO placeholder")


def validate_marketplace(plugin_root: Path, errors: list[str]) -> None:
    repository_root = plugin_root.parents[1]
    path = repository_root / ".agents" / "plugins" / "marketplace.json"
    # The marketplace is distribution metadata outside the portable plugin.
    # Validate it in a source checkout when present, but keep a copied/installed
    # plugin independently self-validating.
    if not path.exists():
        return
    payload = load_object(path, errors)
    if payload is None:
        return
    if not isinstance(payload.get("name"), str) or not payload["name"]:
        errors.append("marketplace name must be a non-empty string")
    interface = payload.get("interface")
    if not isinstance(interface, dict) or not isinstance(interface.get("displayName"), str):
        errors.append("marketplace interface.displayName must be a string")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        errors.append("marketplace plugins must be an array")
        return
    entries = [entry for entry in plugins if isinstance(entry, dict) and entry.get("name") == PLUGIN_NAME]
    if len(entries) != 1:
        errors.append("marketplace must contain exactly one automation-bridge entry")
        return
    entry = entries[0]
    if entry.get("source") != {"source": "local", "path": "./plugins/automation-bridge"}:
        errors.append("marketplace automation-bridge source path is invalid")
    policy = entry.get("policy")
    if not isinstance(policy, dict):
        errors.append("marketplace automation-bridge policy must be an object")
    else:
        if policy.get("installation") not in {
            "NOT_AVAILABLE",
            "AVAILABLE",
            "INSTALLED_BY_DEFAULT",
        }:
            errors.append("marketplace installation policy is invalid")
        if policy.get("authentication") not in {"ON_INSTALL", "ON_USE"}:
            errors.append("marketplace authentication policy is invalid")
    if not isinstance(entry.get("category"), str) or not entry["category"]:
        errors.append("marketplace automation-bridge category must be non-empty")


def validate_launcher_and_bundle(
    plugin_root: Path, *, allow_missing_python: bool, errors: list[str]
) -> None:
    launcher = plugin_root / "scripts" / "run_mcp.py"
    if not launcher.is_file():
        errors.append(f"missing MCP launcher: {launcher}")
    else:
        try:
            compile(launcher.read_text(encoding="utf-8"), str(launcher), "exec")
        except (OSError, UnicodeError, SyntaxError) as exc:
            errors.append(f"MCP launcher is not valid Python: {exc}")
    mcp_server = plugin_root / "python" / "automation_bridge" / "mcp_server.py"
    if not mcp_server.is_file() and not allow_missing_python:
        errors.append(f"bundled MCP server is missing: {mcp_server}")


def validate_contained_path(
    plugin_root: Path, raw_path: str, label: str, errors: list[str]
) -> None:
    candidate = (plugin_root / raw_path).resolve()
    if not candidate.is_relative_to(plugin_root.resolve()):
        errors.append(f"{label} escapes the plugin root")


def validate_tree_containment(plugin_root: Path, errors: list[str]) -> None:
    resolved_root = plugin_root.resolve()
    for path in plugin_root.rglob("*"):
        if not path.resolve().is_relative_to(resolved_root):
            errors.append(f"package path resolves outside the plugin root: {path}")


def included_files(root: Path) -> dict[Path, bytes]:
    result: dict[Path, bytes] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if IGNORED_PARTS & set(relative.parts) or path.suffix in IGNORED_SUFFIXES:
            continue
        result[relative] = path.read_bytes()
    return result


def validate_sync(plugin_root: Path, errors: list[str]) -> None:
    repository_root = plugin_root.parents[1]
    source = repository_root / "automation_bridge" / "automation-bridge-python"
    bundled = plugin_root / "python"
    if not source.is_dir() or not bundled.is_dir():
        errors.append("cannot check wrapper sync until source and bundled python directories exist")
        return
    source_files = included_files(source)
    bundled_files = included_files(bundled)
    for relative in sorted(set(source_files) - set(bundled_files)):
        errors.append(f"bundled wrapper is missing {relative}")
    for relative in sorted(set(bundled_files) - set(source_files)):
        errors.append(f"bundled wrapper has stale file {relative}")
    for relative in sorted(set(source_files) & set(bundled_files)):
        if source_files[relative] != bundled_files[relative]:
            errors.append(f"bundled wrapper differs at {relative}")


def walk_strings(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[tuple[Any, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, path + (index,))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(item, path + (key,))


def format_path(path: tuple[Any, ...]) -> str:
    return ".".join(str(part) for part in path)


def main() -> None:
    args = parse_args()
    plugin_root = args.plugin_root.expanduser().resolve()
    errors: list[str] = []

    portable_manifest = load_object(plugin_root / "plugin.json", errors)
    portable_mcp = load_object(plugin_root / "mcp.json", errors)
    codex_manifest = load_object(plugin_root / ".codex-plugin" / "plugin.json", errors)
    codex_mcp = load_object(plugin_root / ".mcp.json", errors)

    if portable_manifest is not None:
        validate_portable_manifest(portable_manifest, errors)
    if portable_mcp is not None:
        validate_portable_mcp(portable_mcp, plugin_root, errors)
    if codex_manifest is not None and codex_mcp is not None and portable_manifest is not None:
        validate_native_files(codex_manifest, codex_mcp, portable_manifest, errors)

    validate_skill(plugin_root, errors)
    validate_marketplace(plugin_root, errors)
    validate_launcher_and_bundle(
        plugin_root,
        allow_missing_python=args.allow_missing_python,
        errors=errors,
    )
    validate_tree_containment(plugin_root, errors)
    if args.check_sync:
        validate_sync(plugin_root, errors)

    if errors:
        print("Automation Bridge plugin validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Automation Bridge plugin validation passed: {plugin_root}")


if __name__ == "__main__":
    main()
