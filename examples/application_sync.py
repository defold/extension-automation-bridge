"""Race-free application synchronization example."""

from automation_bridge import AutomationBridgeClient


bridge = AutomationBridgeClient.from_project(".", build=True)

with bridge.events(from_cursor="now") as events:
    before = bridge.state("sample.game")
    result = bridge.command("sample.reset", {"request_id": "example-1"})
    completed = events.wait(
        "sample.reset_complete",
        where={"request_id": "example-1"},
        timeout=10,
    )
    state = bridge.wait_for_state(
        "sample.game.item_count",
        0,
        after_revision=before.revision,
        timeout=10,
    )

print(result["result"], completed.sequence, state.revision)
bridge.mark("reset_visible", {"request_id": "example-1"})
