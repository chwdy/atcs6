"""Action factories for negative tests run at event triggers.

Actions probe the system for guard rails — e.g. "while a train is on this
switch, attempting to flip it from outside must be rejected." They run AFTER
the event's assertions on the same snapshot, and raise AssertionError on
unexpected outcomes.
"""

from typing import Callable, Optional

from .harness import Snapshot

Check = Callable[[Snapshot], None]


def attempt_set_switch_position(
    switch_id: str,
    new_position: int,
    should_succeed: bool,
    expect_msg_contains: Optional[str] = None,
) -> Check:
    """Call `SwitchManager.request_position_change(...)` and assert the result
    matches `should_succeed`. Optionally require the rejection message to
    contain `expect_msg_contains` (useful to verify the *reason* the system
    rejected, not just that it rejected).
    """

    def action(s: Snapshot) -> None:
        manager = s.sim_controller.switch_manager
        assert manager is not None, "switch_manager not wired on sim_controller"
        success, message = manager.request_position_change(switch_id, new_position)
        if should_succeed:
            assert success, (
                f"expected position change on switch {switch_id} to "
                f"{new_position} to SUCCEED, but it was rejected: {message!r}"
            )
        else:
            assert not success, (
                f"expected position change on switch {switch_id} to "
                f"{new_position} to be REJECTED, but it succeeded "
                f"({message!r})"
            )
            if expect_msg_contains is not None:
                assert expect_msg_contains.lower() in (message or "").lower(), (
                    f"rejection message {message!r} does not contain "
                    f"{expect_msg_contains!r}"
                )

    return action
