"""Fail fast for required features while allowing a reduced CI/headless subset."""

from automation_bridge import AutomationBridgeClient


bridge = AutomationBridgeClient.from_project(
    ".",
    build=True,
    required_capabilities=["runtime.health", "runtime.lifecycle"],
    optional_capabilities=["scene", "screenshot", "input.drag"],
)

available = bridge.optional("scene", "screenshot", "input.drag")
print(available)
print(bridge.trace_metadata())
print(bridge.lifecycle())
