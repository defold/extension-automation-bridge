#!/usr/bin/env python3
"""Smoke-test game object bounds that are derived from visible child components."""

import argparse
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation_bridge" / "automation-bridge-python"))

from automation_bridge import AutomationBridgeClient  # noqa: E402


def center_xy(node):
    if not node.bounds:
        raise AssertionError(f"node has no bounds: {node.compact()}")
    center = node.bounds.center
    return float(center["x"]), float(center["y"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-build", action="store_true", help="connect to the current editor engine instead of building first")
    parser.add_argument("--keep-open", action="store_true", help="leave the engine running after the smoke test")
    args = parser.parse_args()

    bridge = AutomationBridgeClient.from_project(ROOT, build=not args.no_build)
    try:
        try:
            bridge.resize(960, 640)
        except Exception as exc:  # noqa: BLE001 - window resizing is a desktop-only convenience.
            print(f"warning: could not resize engine window: {exc}", file=sys.stderr)

        fixture = bridge.node(
            type="goc",
            name_exact="/bounds_fixture",
            visible=True,
            include=["bounds", "children"],
        )
        sprite_children = [child for child in fixture.children if child.type == "spritec"]
        if len(sprite_children) != 1:
            raise AssertionError(f"expected one sprite child, found {len(sprite_children)} in {fixture.compact()}")

        sprite = sprite_children[0]
        fixture_x, fixture_y = center_xy(fixture)
        sprite_x, sprite_y = center_xy(sprite)
        distance = math.hypot(fixture_x - sprite_x, fixture_y - sprite_y)
        if distance > 2.0:
            raise AssertionError(
                "parent game object bounds did not follow child sprite bounds: "
                f"parent=({fixture_x:.1f},{fixture_y:.1f}) "
                f"sprite=({sprite_x:.1f},{sprite_y:.1f}) "
                f"distance={distance:.2f}"
            )

        bridge.click(fixture, wait=0.1)
        print("ok: bounds_fixture game object center follows its offset sprite child")
        print(f"parent center=({fixture_x:.1f},{fixture_y:.1f}) sprite center=({sprite_x:.1f},{sprite_y:.1f})")
    finally:
        if not args.keep_open:
            bridge.close_engine()


if __name__ == "__main__":
    main()
