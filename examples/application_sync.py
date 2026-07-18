"""Race-free application synchronization example."""

from automation_bridge import editor


game = editor.open_project(".").build_and_run()

with game.events(from_cursor="now") as events:
    before = game.state("sample.game")
    result = game.command("sample.reset", {"request_id": "example-1"})
    completed = events.wait(
        "sample.reset_complete",
        where={"request_id": "example-1"},
        timeout=10,
    )
    state = game.wait_for_state(
        "sample.game.item_count",
        0,
        after_revision=before.revision,
        timeout=10,
    )

print(result["result"], completed.sequence, state.revision)
game.mark("reset_visible", {"request_id": "example-1"})
