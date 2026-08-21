"""Copyable Automation Bridge Python patterns for coding agents.

This is a reference module, not one end-to-end test. Each function demonstrates
one workflow and is intentionally not called on import. Replace project-specific
resource paths, selectors, state paths, event names, and commands before use.

Core rules:

* Use ``from automation_bridge import editor, engine``.
* Prefer named helpers over ``game.request(...)``.
* Declare required capabilities and probe optional ones.
* Select semantically; do not infer identity from coordinates or appearance.
* Treat ``Element`` objects as snapshots and re-query after state changes.
* Synchronize with state, events, acknowledgements, frames, or scene evidence;
  do not use arbitrary sleeps.
* Keep editor preview evidence separate from runtime screenshot evidence.
* Close an engine only when the script owns its lifecycle.

Public docstrings are the exact API reference. Use ``help(engine.Client)`` or
``help(engine.Client.<method>)`` when adapting these examples.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Union

from automation_bridge import editor, engine


ProjectPath = Union[str, Path]


def update_bridge(
    project: editor.Client,
    version: Optional[str] = None,
) -> editor.AutomationBridgeUpdateResult:
    """Run as a maintenance step; restart Python before further automation."""
    if version is None:
        return project.update_automation_bridge()
    return project.update_automation_bridge(version)


def require_running_editor(project_root: ProjectPath = ".") -> editor.Client:
    """Reuse an editor without launching a GUI process from a sandbox."""
    return editor.open_project(project_root, start_if_needed=False)


def open_or_launch_editor(project_root: ProjectPath = ".") -> editor.Client:
    """Start Defold only when this Python process is allowed to launch a GUI."""
    return editor.open_project(project_root)


def preview_resources(
    project: editor.Client,
    resources: Iterable[str],
    output_directory: ProjectPath,
) -> None:
    """Validate manually edited resources before building the game."""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    for resource in resources:
        png = project.preview.render(resource, resolution_multiplier=0.5)
        destination = output / f"{Path(resource).name}.png"
        destination.write_bytes(png)


def inspect_editor_services(project: editor.Client) -> None:
    """Use typed editor helpers instead of constructing editor HTTP requests."""
    console = project.console.read()
    references = project.reference.search(
        environment="runtime",
        language="lua",
        query="go.property",
    )
    font_size = project.preferences.get(project.preferences.CODE_FONT_SIZE)
    print(len(console.lines), len(references), font_size)

    # Mutating editor operations are explicit. Invoke only when required.
    project.preferences.set(project.preferences.CODE_FONT_SIZE, 16)
    project.commands.hot_reload()


def build_owned_game(project: editor.Client) -> engine.Client:
    """Build a fresh engine and declare the features required by the script."""
    return project.build_and_run(
        required_capabilities=(
            "scene",
            "elements",
            "input.click",
            "input.drag",
            "input.key",
            "screenshot",
        ),
    )


def connect_to_existing_game(project: editor.Client) -> engine.Client:
    """Connect without building or taking ownership of engine cleanup."""
    return project.connect_engine(required_capabilities=("scene", "elements"))


def connect_to_known_port(port: int) -> engine.Client:
    """Connect without an editor when the engine service port is already known."""
    return engine.connect(port, required_capabilities=("scene", "elements"))


def inspect_capabilities(game: engine.Client) -> None:
    """Require mandatory features and branch around optional features."""
    game.require("scene", "input.click")
    if game.supports("screen.resize"):
        game.resize(960, 640)
    print(game.trace_metadata())


def select_elements(game: engine.Client) -> engine.Element:
    """Prefer semantic, exact selectors and use count() for complete counts."""
    labels = game.elements(type="labelc", visible=True, limit=100)
    print(game.format_elements(labels))
    print("all labels:", game.count(type="labelc"))

    # Replace these application-owned annotations with selectors from the game.
    button = game.element(automation_id="play", visible=True, enabled=True)
    optional_popup = game.maybe_element(role="modal")
    if optional_popup is not None:
        print(optional_popup.compact())

    # A child can provide the useful selector while its parent receives input.
    if button.type != "goc" and button.parent_id:
        return game.parent(button)
    return button


def send_safe_input(game: engine.Client) -> None:
    """Pass Element objects for identity guards and separate text from keys."""
    button = game.element(automation_id="play", visible=True, enabled=True)
    try:
        game.click(button)
    except engine.StaleElementError:
        # Re-query; never retry the same stale object.
        button = game.element(automation_id="play", visible=True, enabled=True)
        game.click(button)

    # Re-query after input before starting another element-targeted gesture.
    source = game.element(automation_id="drag_source", visible=True, enabled=True)
    target = game.element(automation_id="drop_target", visible=True, enabled=True)
    try:
        game.drag(source, target, duration=0.25, easing="ease_in_out")
    except engine.StaleElementError:
        source = game.element(automation_id="drag_source", visible=True, enabled=True)
        target = game.element(automation_id="drop_target", visible=True, enabled=True)
        game.drag(source, target, duration=0.25, easing="ease_in_out")

    game.type_text("literal {UTF-8} text")
    game.key("ENTER")
    game.key("SPACE", hold=1.0, wait="released", timeout=2.0)


def control_input_lifecycle(game: engine.Client) -> None:
    """Use receipts and contexts so interruptions release native input safely."""
    button = game.element(automation_id="play")
    with game.input.interruption_scope():
        receipt = game.click(button, wait=False)
        game.input.wait(receipt, state="released", timeout=5)

    with game.pointer((100, 100), lease=10) as pointer:
        pointer.move((200, 150), duration=0.2)
        pointer.hold(0.1)
        pressed = game.screenshot(after_frames=1)
        print(pointer.input_id, pressed.path)


def synchronize_with_application(game: engine.Client) -> None:
    """Subscribe before acting and require a newer state revision when needed."""
    button = game.element(automation_id="play")

    with game.events("now") as events:
        game.click(button)
        event = events.wait("operation.complete", timeout=5)
        print(event.name, event.data)

    revision = game.state("ui").revision
    game.click(game.element(automation_id="play"))
    state = game.wait_for_state("ui.busy", False, after_revision=revision)
    print(state.name, state.revision)

    result = game.command("reset_fixture", {"seed": 42})
    marker = game.mark("fixture_reset", {"command_id": result["command_id"]})
    print(marker)


def wait_for_semantic_input_completion(game: engine.Client) -> None:
    """Distinguish native release from application acknowledgement."""
    button = game.element(automation_id="play")
    with game.events("now") as events:
        receipt = game.click(button)
        acknowledgement = game.wait_for_input_acknowledgement(
            receipt.input_id,
            events=events,
        )
    print(acknowledgement.data)


def wait_for_scene_evidence(game: engine.Client) -> None:
    """Use scene and frame observations only when they are the required evidence."""
    trigger = game.element(automation_id="play")
    previous = trigger.scene_sequence
    game.click(trigger)
    result = game.wait_for_element(
        automation_id="result",
        after_scene_sequence=previous,
    )
    stable = game.observe_element(
        automation_id="result",
        minimum_frames=3,
    )
    game.wait_for_count(1, role="result")
    game.wait_frames(2)
    disappeared = game.wait_for_disappearance(result.id)
    print(stable.observed_frames, disappeared.disappeared_frame)


def capture_visual_evidence(game: engine.Client) -> None:
    """Keep atomic receipts and convert coordinates through named spaces."""
    before = game.screenshot(after_frames=1, resolution_multiplier=0.5)
    button = game.element(automation_id="play")
    revision = game.state("ui").revision
    game.click(button)
    game.wait_for_state("ui.busy", False, after_revision=revision)
    after = game.screenshot(after_frames=1, resolution_multiplier=0.5)
    difference = game.visual.difference(before, after)
    print(before.path, before.sha256, difference)

    window_point = game.convert_point(
        (0.5, 0.5),
        from_space="normalized_viewport",
        to_space="window",
    )
    print(window_point)


def collect_diagnostics(game: engine.Client, output_directory: ProjectPath) -> None:
    """Collect bounded logs and a trace around the operation of interest."""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    print("\n".join(game.logs.tail(100, contains="ERROR:")))

    button = game.element(automation_id="play")
    with game.trace(output / "session.trace.json", screenshots="on_error") as trace:
        game.click(button)
        trace.record("checkpoint", {"name": "after play"})


def generate_reproducible_gesture(game: engine.Client) -> None:
    """Use a seed when generated input must be reproducible."""
    gesture = game.gestures.generate_drag(
        (100, 100),
        (500, 300),
        seed=42,
        duration=(0.7, 0.9),
        control_points=4,
    )
    game.drag_path(**gesture)


def profile_operation(game: engine.Client) -> None:
    """Capture profiler evidence around a bounded operation."""
    recording = game.profiler.start_recording(warmup_frames=10)
    try:
        revision = game.state("ui").revision
        game.click(game.element(automation_id="play"))
        game.wait_for_state("ui.busy", False, after_revision=revision)
    finally:
        capture = recording.stop()
    for scope in capture.scopes(contains="Update"):
        print(scope.path, scope.self.p95_ms)


def record_optional_video(game: engine.Client, output: ProjectPath) -> None:
    """Probe optional recording support before relying on it."""
    capabilities = game.video_recording.capabilities()
    if not capabilities.available:
        return
    with game.video_recording.start(output, size=(960, 540), fps=30):
        revision = game.state("ui").revision
        game.click(game.element(automation_id="play"))
        game.wait_for_state("ui.busy", False, after_revision=revision)


def capture_optional_metal_trace(game: engine.Client, output: ProjectPath) -> None:
    """Capture Metal only when advertised and enabled at engine launch."""
    if not game.supports("metal.capture"):
        return
    capture = game.metal_capture.start(output, frames=1)
    print(capture.path, capture.frames_captured)


def close_owned_game(game: engine.Client, owns_engine: bool) -> None:
    """Do not terminate a reused engine merely because a script is finished."""
    if owns_engine:
        game.close_engine()


if __name__ == "__main__":
    print(__doc__)
