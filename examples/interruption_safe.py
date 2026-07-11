#!/usr/bin/env python3
"""Demonstrate interruption-safe input, polling diagnostics, and recording hooks."""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import editor, engine  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    project = editor.open_project(ROOT)
    game = project.connect_engine() if args.no_build else project.build_and_run()
    recording = game.profiler.start_recording(
        thread="Main",
        on_finalize=lambda capture: print(f"finalized {len(capture.frames)} profiler frames"),
        on_abort=lambda cause, capture: print(
            f"aborted after {len(capture.frames)} profiler frames: {cause}",
            file=sys.stderr,
        ),
    )
    try:
        fixture = game.wait_for_element(name_exact="/bounds_fixture", visible=True)
        with game.input.interruption_scope():
            receipt = game.click(fixture, wait=False)
            game.screenshot(wait=True, timeout=5)
            game.input.wait(receipt, timeout=5, flush_on_interrupt=True)
        recording.stop()
    except engine.WaitTimeoutError as exc:
        print(
            f"wait failed after {exc.attempts} attempts at scene "
            f"{exc.scene_sequence}: {exc.last_value!r}",
            file=sys.stderr,
        )
        recording.abort(exc)
        raise
    except BaseException as exc:
        # abort() salvages the partial capture; its context/hook cleanup cannot
        # replace this original KeyboardInterrupt, cancellation, or error.
        try:
            recording.abort(exc)
        except BaseException:
            pass
        raise
    finally:
        if not args.keep_open:
            if sys.exc_info()[0] is None:
                game.close_engine()
            else:
                try:
                    game.close_engine()
                except BaseException:
                    pass


if __name__ == "__main__":
    main()
