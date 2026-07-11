"""Fail fast for required features while allowing a reduced CI/headless subset."""

from automation_bridge import editor


game = editor.open_project(".").build_and_run(
    required_capabilities=["runtime.health", "runtime.lifecycle"],
)

available = {
    capability: game.supports(capability)
    for capability in ("scene", "screenshot", "input.drag")
}
print(available)
print(game.trace_metadata())
print(game.lifecycle())
