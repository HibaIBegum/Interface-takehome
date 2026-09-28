import base64

import pytest

from conftest import MOCK_PASSWORD, MOCK_USER, post_json
from cua.surface.base import (
    Click, ErrorCode, Extract, Fill, LocatorCandidate, Navigate, Select, Strategy, Target, WaitFor, target_for,
)
from cua.surface.playwright_surface import PlaywrightSurface


def role(role_: str, name: str, frame: list[str] | None = None) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=Strategy.ROLE_NAME, role=role_, value=name,
                                               frame_path=["main"] if frame is None else frame)])


def label(text: str, frame: list[str] | None = None) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=Strategy.LABEL, value=text,
                                               frame_path=["main"] if frame is None else frame)])


def near(text: str) -> Target:
    return Target(candidates=[LocatorCandidate(strategy=Strategy.TEXT_NEAR, value=text, frame_path=["main"])])


SEARCH_BUTTON = role("button", "Search")
MEMBER_ID_FIELD = label("Member ID")


def act_ok(surface, action):
    result = surface.act(action)
    assert result.ok, result
    return result


def login(surface):
    act_ok(surface, Navigate(url="/login"))
    act_ok(surface, Fill(target=label("User ID", frame=[]), value=MOCK_USER))
    act_ok(surface, Fill(target=label("Password", frame=[]), value=MOCK_PASSWORD, sensitive=True))
    act_ok(surface, Click(target=role("button", "Sign On", frame=[])))
    act_ok(surface, WaitFor(target=SEARCH_BUTTON))


def open_member(surface, member_id="100234"):
    act_ok(surface, Fill(target=MEMBER_ID_FIELD, value=member_id))
    act_ok(surface, Click(target=SEARCH_BUTTON))
    act_ok(surface, WaitFor(text="Member Detail", frame_path=["main"]))


# ---------------------------------------------------------------- acceptance

def test_resolves_search_button_and_member_id_inside_frame(surface):
    login(surface)
    obs = surface.observe()

    assert [f.path for f in obs.frames] == [[], ["hdr"], ["main"]]
    button = next(e for e in obs.elements if e.role == "button" and e.name == "Search")
    field = next(e for e in obs.elements if e.role == "textbox" and e.label == "Member ID")
    assert button.frame_path == field.frame_path == ["main"]

    assert surface.resolve(SEARCH_BUTTON).resolution.strategy is Strategy.ROLE_NAME
    resolved = surface.resolve(MEMBER_ID_FIELD)
    assert resolved.resolution.strategy is Strategy.LABEL
    assert resolved.locator.bounding_box() == field.bbox.model_dump()


def test_full_flow_through_surface(surface):
    login(surface)
    open_member(surface)
    assert act_ok(surface, Extract(target=near("Member Name"))).extracted == "Jane Q. Testmember"
    assert surface.extract(near("SSN")) == "***-**-0001"

    act_ok(surface, Click(target=role("button", "Open Sub-Account")))
    act_ok(surface, Select(target=label("Sub-Account Type"), option="Money Market"))
    act_ok(surface, Fill(target=label("Nickname"), value="Rainy Day"))
    act_ok(surface, Fill(target=label("Initial Deposit ($)"), value="75.00"))
    act_ok(surface, Click(target=role("button", "Continue")))
    act_ok(surface, Click(target=role("button", "Submit")))
    act_ok(surface, WaitFor(text="Sub-Account Opened", frame_path=["main"]))
    assert surface.extract(near("Reference Number")).startswith("SA-")


# ---------------------------------------------------------------- resolution semantics

def test_falls_through_to_next_candidate_and_records_attempts(surface):
    login(surface)
    target = Target(candidates=[
        LocatorCandidate(strategy=Strategy.ROLE_NAME, role="button", value="Find", frame_path=["main"]),
        LocatorCandidate(strategy=Strategy.LABEL, value="Member ID", frame_path=["main"]),
    ])
    resolution = surface.resolve(target, timeout_ms=500).resolution
    assert resolution.matched_index == 1
    assert [(a.strategy, a.matches) for a in resolution.attempts] == [(Strategy.ROLE_NAME, 0), (Strategy.LABEL, 1)]


def test_ambiguous_target_is_rejected(surface):
    login(surface)
    ambiguous = Target(candidates=[LocatorCandidate(strategy=Strategy.CSS, value="input", frame_path=["main"])])
    result = surface.act(Click(target=ambiguous))
    assert not result.ok and result.error_code is ErrorCode.TARGET_AMBIGUOUS
    assert result.resolution.attempts[0].matches == 2


def test_missing_target_fails_within_timeout(surface):
    login(surface)
    surface.timeout_ms = 500
    result = surface.act(Click(target=role("button", "Delete Member")))
    assert result.error_code is ErrorCode.TARGET_NOT_FOUND
    assert result.duration_ms < 2000


def test_wrong_frame_does_not_match(surface):
    login(surface)
    assert surface.act(Click(target=role("button", "Search", frame=["hdr"]))).error_code is ErrorCode.TARGET_NOT_FOUND


@pytest.mark.parametrize("screen", ["search", "member", "subaccount_form"])
def test_every_observed_element_resolves_back_to_itself(surface, screen):
    login(surface)
    if screen != "search":
        open_member(surface)
    if screen == "subaccount_form":
        act_ok(surface, Click(target=role("button", "Open Sub-Account")))
        act_ok(surface, WaitFor(target=label("Nickname")))

    obs = surface.observe()
    assert len(obs.elements) >= 4
    for element in obs.elements:
        resolved = surface.resolve(target_for(element), timeout_ms=500)
        assert resolved.resolution.matched_index == 0, element
        assert resolved.locator.bounding_box() == element.bbox.model_dump(), element


# ---------------------------------------------------------------- observation content

def test_observation_carries_no_typed_values(surface):
    login(surface)
    act_ok(surface, Fill(target=MEMBER_ID_FIELD, value="987654"))
    assert "987654" not in surface.observe().model_dump_json()


def test_state_hash_tracks_screen_not_data(surface):
    login(surface)
    search_hash = surface.observe().state_hash
    open_member(surface, "100234")
    jane = surface.observe()
    act_ok(surface, Click(target=role("link", "Member Search", frame=["hdr"])))
    open_member(surface, "100236")
    maria = surface.observe()
    assert "Maria Placeholder" in maria.visible_text
    assert jane.state_hash == maria.state_hash != search_hash


def test_interstitial_marks_elements_occluded_and_blocks_click(surface, mock_server):
    login(surface)
    post_json(f"{mock_server}/__faults", {"fault": "interstitial"})
    act_ok(surface, Click(target=role("link", "Member Search", frame=["hdr"])))
    act_ok(surface, WaitFor(text="System Notice", frame_path=["main"]))

    obs = surface.observe()
    assert next(e for e in obs.elements if e.name == "Search").occluded
    assert not next(e for e in obs.elements if e.name == "OK").occluded

    surface.page.set_default_timeout(800)
    blocked = surface.act(Click(target=SEARCH_BUTTON))
    assert blocked.error_code is ErrorCode.OBSTRUCTED and blocked.resolution.matched_index == 0

    act_ok(surface, Click(target=role("button", "OK")))
    act_ok(surface, WaitFor(text="System Notice", frame_path=["main"], state="hidden"))
    act_ok(surface, Click(target=SEARCH_BUTTON))


# ---------------------------------------------------------------- screenshots

def _pixel(browser, png_path, point):
    page = browser.new_page()
    try:
        src = "data:image/png;base64," + base64.b64encode(png_path.read_bytes()).decode()
        return page.evaluate(
            """async ([src, x, y]) => {
                 const img = new Image(); img.src = src; await img.decode();
                 const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
                 const ctx = c.getContext('2d'); ctx.drawImage(img, 0, 0);
                 return Array.from(ctx.getImageData(x, y, 1, 1).data.slice(0, 3));
               }""",
            [src, round(point[0]), round(point[1])],
        )
    finally:
        page.close()


def test_screenshot_masks_passwords_and_configured_fields_in_frames(surface, browser, tmp_path):
    black = [0, 0, 0]
    act_ok(surface, Navigate(url="/login"))
    act_ok(surface, Fill(target=label("Password", frame=[]), value=MOCK_PASSWORD, sensitive=True))
    login_obs = surface.observe(screenshot_path=tmp_path / "login.png")
    by_label = {e.label: e for e in login_obs.elements}
    assert _pixel(browser, login_obs.screenshot_path, by_label["Password"].bbox.center()) == black
    assert _pixel(browser, login_obs.screenshot_path, by_label["User ID"].bbox.center()) != black

    masked = PlaywrightSurface(surface.page, base_url=surface.base_url, timeout_ms=3000, mask=[near("SSN")])
    login(masked)
    open_member(masked)
    shot = masked.screenshot(tmp_path / "member.png")
    ssn_box = masked.resolve(near("SSN")).locator.bounding_box()
    name_box = masked.resolve(near("Member Name")).locator.bounding_box()
    assert _pixel(browser, shot, (ssn_box["x"] + 5, ssn_box["y"] + ssn_box["height"] / 2)) == black
    assert _pixel(browser, shot, (name_box["x"] + 5, name_box["y"] + name_box["height"] / 2)) != black
