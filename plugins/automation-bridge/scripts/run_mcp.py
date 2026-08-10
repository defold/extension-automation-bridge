#!/usr/bin/env python3
"""Launch the bundled Automation Bridge MCP server over standard I/O."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1]
BUNDLED_PYTHON = PLUGIN_DIRECTORY / "python"
sys.path.insert(0, str(BUNDLED_PYTHON))

import automation_bridge.mcp_server  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(automation_bridge.mcp_server.main())
