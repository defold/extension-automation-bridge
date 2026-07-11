"""Example: reproducible path generation plus a diagnostic trace bundle."""

from pathlib import Path

from automation_bridge import editor


game = editor.open_project(".").build_and_run()
artifact_directory = Path("artifacts")

with game.trace(
    artifact_directory / "gesture.trace.json",
    screenshots="on_error",
    prerequisites={
        "application_state": "known test fixture",
        "random_seeds": [42],
        "timing_mode": "native release receipts",
        "external_services": "offline",
    },
) as trace:
    gesture = game.gestures.generate_drag(
        (100, 100),
        (600, 400),
        seed=42,
        bounds=(0, 0, 800, 600),
        max_velocity=1200,
        max_acceleration=8000,
    )
    # Requires the native multi-point gesture capability.
    receipt = game.drag_path(**gesture)
    trace.record_input_acknowledgement({"input_id": receipt.input_id, "accepted": True})
