#!/usr/bin/env python3
"""Atomically refresh the plugin's bundled Python API from this repository."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path


PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_DIRECTORY.parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT / "automation_bridge" / "automation-bridge-python"
DESTINATION = PLUGIN_DIRECTORY / "python"
IGNORED_NAMES = (".DS_Store", "__pycache__", "*.pyc", "*.pyo")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Refresh plugins/automation-bridge/python from the canonical wrapper."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="Wrapper source directory (defaults to the source checkout's canonical wrapper).",
    )
    return parser.parse_args()


def main() -> None:
    source = parse_args().source.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"wrapper source is not a directory: {source}")
    required = source / "automation_bridge" / "mcp_server.py"
    if not required.is_file():
        raise SystemExit(f"wrapper source is missing MCP server: {required}")

    staging_parent = Path(
        tempfile.mkdtemp(prefix=".automation-bridge-python-", dir=PLUGIN_DIRECTORY)
    )
    staged = staging_parent / "python"
    backup = staging_parent / "previous"
    try:
        shutil.copytree(source, staged, ignore=shutil.ignore_patterns(*IGNORED_NAMES))
        if DESTINATION.exists():
            if DESTINATION.is_symlink() or not DESTINATION.is_dir():
                raise SystemExit(f"refusing to replace non-directory: {DESTINATION}")
            DESTINATION.rename(backup)
        try:
            staged.rename(DESTINATION)
        except BaseException:
            if backup.exists() and not DESTINATION.exists():
                backup.rename(DESTINATION)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)

    print(f"Synced {source} -> {DESTINATION}")


if __name__ == "__main__":
    main()
