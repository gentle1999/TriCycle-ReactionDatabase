"""Qualification rules for frames that establish transition-state coordinates."""

from tricycle_reaction_db.domain.enums import FrameRole


def is_transition_state_frame_eligible(frame_role: FrameRole) -> bool:
    """Return whether a frame may provide a TS coordinate.

    TS endpoint inference is the scientific qualification. Terminal TS frames
    therefore do not inherit the endpoint optimization-convergence threshold.
    """

    return frame_role in {FrameRole.SINGLE_POINT, FrameRole.TERMINAL}


__all__ = ["is_transition_state_frame_eligible"]
