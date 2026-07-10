#!/usr/bin/env python3
"""Demonstrate snapshot guards, coordinate conversion, and atomic captures."""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import AutomationBridgeClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()

    bridge = AutomationBridgeClient.from_project(ROOT, build=not args.no_build)
    try:
        scene = bridge.scene()
        spawner = bridge.node(type="goc", name_exact="/spawner", visible=True)
        bridge.click(spawner, expected_scene_sequence=spawner.scene_sequence)

        observation = bridge.wait_for_appearance(
            type="labelc",
            text_exact="L1",
            after_scene_sequence=max(scene["scene_sequence"], spawner.scene_sequence),
        )
        stable = bridge.observe_node(
            logical_id=observation.node.logical_id,
            minimum_frames=2,
        )

        center = bridge.convert_point(
            (0.5, 0.5),
            from_space="normalized_viewport",
            to_space="window",
        )
        shot = bridge.screenshot(after_frames=1, wait=True)

        print(f"node={stable.identity} frames={stable.first_frame}..{stable.last_frame}")
        print(f"viewport center in window pixels: {center}")
        print(
            f"capture={shot.capture_id} frame={shot.frame} sequence={shot.scene_sequence} "
            f"size={shot.width}x{shot.height} sha256={shot.sha256} path={shot.path}"
        )
    finally:
        if not args.keep_open:
            bridge.close_engine()


if __name__ == "__main__":
    main()
