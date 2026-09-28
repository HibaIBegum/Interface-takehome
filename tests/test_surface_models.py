import pytest
from pydantic import TypeAdapter, ValidationError

from cua.surface.base import Action, ElementInfo, Fill, LocatorCandidate, Strategy, Target, WaitFor, target_for


def test_role_name_candidate_requires_role():
    with pytest.raises(ValidationError):
        LocatorCandidate(strategy=Strategy.ROLE_NAME, value="Search")


def test_coords_candidate_must_be_x_comma_y():
    assert LocatorCandidate(strategy=Strategy.COORDS, value="10,20.5").point() == (10.0, 20.5)
    with pytest.raises(ValidationError):
        LocatorCandidate(strategy=Strategy.COORDS, value="middle")


def test_target_needs_a_candidate():
    with pytest.raises(ValidationError):
        Target(candidates=[])


def test_wait_for_needs_exactly_one_condition():
    with pytest.raises(ValidationError):
        WaitFor()
    with pytest.raises(ValidationError):
        WaitFor(text="x", target=Target(candidates=[LocatorCandidate(strategy=Strategy.CSS, value="a")]))


def test_actions_round_trip_through_json():
    adapter = TypeAdapter(Action)
    action = adapter.validate_python({"kind": "wait_for", "text": "Member Detail", "frame_path": ["main"]})
    assert adapter.validate_json(adapter.dump_json(action)) == action


def test_fill_value_is_not_in_repr():
    fill = Fill(target=Target(candidates=[LocatorCandidate(strategy=Strategy.CSS, value="input")]),
                value="hunter2", sensitive=True)
    assert "hunter2" not in repr(fill)


def test_target_for_prefers_role_name_then_label_and_never_coords():
    element = ElementInfo(index=0, role="textbox", name="", label="Member ID", nearby_text="",
                          frame_path=["main"], bbox=None)
    assert [c.strategy for c in target_for(element).candidates] == [Strategy.LABEL]
    element = element.model_copy(update={"name": "Member ID"})
    assert [c.strategy for c in target_for(element).candidates] == [Strategy.ROLE_NAME, Strategy.LABEL]
