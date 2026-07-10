"""Fail fast for required features while allowing a reduced CI/headless subset."""

from automation_bridge import AutomationBridgeClient


bridge = AutomationBridgeClient.from_project(
    ".",
    build=True,
    required_capabilities=["runtime.health", "runtime.lifecycle"],
)

available = {
    capability: bridge.supports(capability)
    for capability in ("scene", "screenshot", "input.drag")
}
print(available)
print(bridge.trace_metadata())
print(bridge.lifecycle())
