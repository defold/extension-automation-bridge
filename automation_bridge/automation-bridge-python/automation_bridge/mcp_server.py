"""Executable stdio entry point for the Automation Bridge MCP server."""

from __future__ import annotations

import sys
from typing import Any, Optional, Sequence, TextIO

from automation_bridge.mcp_protocol import McpProtocol, StdioServer


def _configure_utf8_stream(stream: Any, *, output: bool) -> None:
    """Make the standard binding explicitly UTF-8 when Python supports it."""
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    options = {"encoding": "utf-8", "errors": "strict"}
    if output:
        options["newline"] = "\n"
    reconfigure(**options)


def create_runtime() -> Any:
    """Create the dependency-free Automation Bridge MCP runtime lazily."""
    from automation_bridge.mcp_runtime import BridgeRuntime

    return BridgeRuntime()


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    runtime: Any = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Run the newline-delimited stdio server until the client closes stdin."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments:
        target = stderr if stderr is not None else sys.stderr
        target.write("automation-bridge MCP does not accept command-line arguments\n")
        target.flush()
        return 2
    if stdin is None:
        _configure_utf8_stream(sys.stdin, output=False)
    if stdout is None:
        _configure_utf8_stream(sys.stdout, output=True)
    bridge_runtime = runtime if runtime is not None else create_runtime()
    protocol = McpProtocol(bridge_runtime)
    return StdioServer(
        protocol,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
    ).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
