import pytest

from tricycle_reaction_db.domain.enums import FrameRole
from tricycle_reaction_db.domain.reaction_frames import is_transition_state_frame_eligible


@pytest.mark.parametrize(
    "frame_role",
    [FrameRole.SINGLE_POINT, FrameRole.TERMINAL],
)
def test_ts_frame_eligibility_does_not_apply_optimization_convergence(
    frame_role: FrameRole,
) -> None:
    assert is_transition_state_frame_eligible(frame_role)


@pytest.mark.parametrize("frame_role", [FrameRole.INITIAL, FrameRole.INTERMEDIATE])
def test_ts_frame_eligibility_still_requires_a_terminal_coordinate(
    frame_role: FrameRole,
) -> None:
    assert not is_transition_state_frame_eligible(frame_role)
