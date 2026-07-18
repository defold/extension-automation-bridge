#!/usr/bin/env python3
"""Demonstrate snapshot guards, coordinate conversion, and atomic captures."""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import editor  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    project = editor.open_project(ROOT)
    game = project.connect_engine() if args.no_build else project.build_and_run()
    try:
        scene = game.scene()
        spawner = game.element(type="goc", name_exact="/spawner", visible=True)
        game.click(spawner, expected_scene_sequence=spawner.scene_sequence)

        appeared = game.wait_for_element(
            type="labelc",
            text_exact="L1",
            after_scene_sequence=max(scene["scene_sequence"], spawner.scene_sequence),
        )
        stable = game.observe_element(
            logical_id=appeared.logical_id,
            minimum_frames=2,
        )

        center = game.convert_point(
            (0.5, 0.5),
            from_space="normalized_viewport",
            to_space="window",
        )
        shot = game.screenshot(after_frames=1, wait=True)

        print(f"element={stable.identity} frames={stable.first_frame}..{stable.last_frame}")
        print(f"viewport center in window pixels: {center}")
        print(
            f"capture={shot.capture_id} frame={shot.frame} sequence={shot.scene_sequence} "
            f"size={shot.width}x{shot.height} sha256={shot.sha256} path={shot.path}"
        )
    finally:
        if not args.keep_open:
            game.close_engine()


if __name__ == "__main__":
    main()
