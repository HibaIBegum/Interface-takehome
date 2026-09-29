"""Playwright (Chromium, sync API) implementation of the Surface protocol.

Element discovery uses Playwright's role engine, the same engine `role_name` resolution uses,
so an element observed as (role, name) resolves by (role, name). The `label` strategy mirrors
the observation's label rule exactly: a real <label>/aria-label, else the caption cell
immediately to the left in a table row.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .base import (
    Action, ActionResult, BBox, CandidateAttempt, Click, ElementInfo, ErrorCode, Extract, Fill, FrameInfo,
    FramePath, LocatorCandidate, Navigate, Observation, Press, Resolution, Select, Strategy, Target,
    TargetResolutionError, WaitFor,
)

_CAPTURE_JS = Path(__file__).resolve().parents[1] / "handoff" / "capture.js"

INTERACTIVE_ROLES = (
    "button", "link", "textbox", "searchbox", "combobox", "listbox",
    "checkbox", "radio", "spinbutton", "switch", "tab", "menuitem",
)
_TEXT_PER_FRAME = 2000
_OBSERVE_ATTEMPTS = 3

# Form controls that can carry a label (buttons are named by their own text instead).
_JS_IS_LABELABLE = """
el => el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' ||
      (el.tagName === 'INPUT' && !['submit', 'button', 'reset', 'image', 'hidden'].includes(el.type))
"""
_XPATH_LABELABLE = (
    "//*[self::select or self::textarea or (self::input and not("
    "@type='submit' or @type='button' or @type='reset' or @type='image' or @type='hidden'))]"
)

_ELEMENT_JS = """
(els) => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const strip = s => clean(s).replace(/:$/, '').trim();
  const labelable = %s;
  const all = Array.from(document.getElementsByTagName('*'));
  return els.map(el => {
    let label = '';
    if (el.labels && el.labels.length) label = strip(el.labels[0].innerText);
    else if (el.getAttribute('aria-label')) label = strip(el.getAttribute('aria-label'));
    else if (labelable(el)) {
      const cell = el.closest('td');
      const caption = cell && cell.previousElementSibling;
      if (caption) label = strip(caption.innerText);
    }
    const row = el.closest('tr');
    const near = clean(row ? row.innerText : (el.parentElement ? el.parentElement.innerText : ''));
    const r = el.getBoundingClientRect();
    let occluded = false;
    if (r.width > 0 && r.height > 0) {
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      occluded = !!hit && hit !== el && !el.contains(hit);
    }
    const options = el.tagName === 'SELECT' ? Array.from(el.options).map(o => clean(o.text)) : null;
    const fieldName = labelable(el) ? (el.getAttribute('name') || '') : '';
    return {order: all.indexOf(el), label, near: near.slice(0, 120), occluded, options, fieldName};
  });
}
""" % _JS_IS_LABELABLE.strip()

# Data cells a caption can address: the cell right after the row caption, or any cell whose
# column has a header (same cell count as the header row, so colspans can't shift the index).
_LEAF_CELLS_XPATH = "//td[not(.//td) and not(.//input or .//select or .//textarea or .//button or .//a)]"
_CELL_JS = """
(cells) => {
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
  const strip = s => clean(s).replace(/:$/, '').trim();
  const all = Array.from(document.getElementsByTagName('*'));
  return cells.map(td => {
    const row = td.parentElement, table = td.closest('table');
    const skip = {keep: false};
    if (!row || !table || td.cellIndex < 1 || row.rowIndex === 0) return skip;
    const text = clean(td.innerText), label = strip(row.cells[0].innerText);
    if (!text || !label) return skip;
    const header = table.rows[0];
    const column = header.cells.length === row.cells.length ? strip(header.cells[td.cellIndex].innerText) : '';
    if (!column && td.cellIndex !== 1) return skip;
    return {keep: true, order: all.indexOf(td), text: text.slice(0, 80), label, column,
            near: clean(row.innerText).slice(0, 120)};
  });
}
"""

_BODY_TEXT_JS ="() => (document.body && document.body.tagName === 'BODY') ? document.body.innerText : ''"

# Explicit readiness condition for a frame path: every frame exists and its document has parsed.
_FRAME_READY_JS = """
(path) => {
  let w = window;
  for (const seg of path) {
    const m = /^\\[(\\d+)\\]$/.exec(seg);
    try { w = m ? w.frames[Number(m[1])] : w.frames[seg]; } catch (e) { return false; }
    if (!w) return false;
  }
  try { return w.document.readyState !== 'loading'; } catch (e) { return false; }
}
"""

_READ_JS = "el => ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) ? el.value : el.innerText"

_SNAPSHOT_NAME = re.compile(r'^- [\w-]+(?: "((?:[^"\\]|\\.)*)")?')


def _live_children(frame: Frame) -> list[Frame]:
    # Playwright keeps detached frames in child_frames after a frameset is re-rendered (seen after
    # signing in a second time); they come first and would shadow the live frame of the same name.
    return [f for f in frame.child_frames if not f.is_detached()]


def _is_navigation_race(exc: Exception) -> bool:
    """The page or a frame navigated (or was removed) while we were reading it."""
    text = str(exc)
    return "Execution context was destroyed" in text or "detached" in text


class FrameNotFound(Exception):
    pass


@dataclass
class Resolved:
    resolution: Resolution
    locator: Locator | None = None
    point: tuple[float, float] | None = None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _xpath_literal(s: str) -> str:
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in s.split("'")) + ")"


def _caption_value_cell_xpath(caption: str) -> str:
    """The cell right after a td/th whose text is `caption` (optionally followed by a colon)."""
    exact, colon = _xpath_literal(caption), _xpath_literal(caption + ":")
    return (f"//*[self::td or self::th][normalize-space(.)={exact} or normalize-space(.)={colon}]"
            f"/following-sibling::td[1]")


def _row_column_cell_xpath(caption: str, column: str) -> str:
    """The cell in the row whose first cell is `caption`, under the header cell `column`."""
    cap, cap_colon = _xpath_literal(caption), _xpath_literal(caption + ":")
    col, col_colon = _xpath_literal(column), _xpath_literal(column + ":")
    header = (f"(ancestor::table[1]/tr | ancestor::table[1]/*/tr)[1]"
              f"/*[normalize-space(.)={col} or normalize-space(.)={col_colon}]")
    return (f"//tr[*[1][normalize-space(.)={cap} or normalize-space(.)={cap_colon}]]"
            f"/*[{header} and position() = count({header}/preceding-sibling::*) + 1]")


def _snapshot_name(snapshot: str) -> str:
    match = _SNAPSHOT_NAME.match(snapshot.splitlines()[0] if snapshot else "")
    return json.loads(f'"{match.group(1)}"') if match and match.group(1) else ""


def _error_text(exc: Exception, secret: str | None = None) -> str:
    lines = str(exc).splitlines()
    text = lines[0] if lines else type(exc).__name__
    intercept = next((ln.strip() for ln in lines if "intercepts pointer events" in ln), None)
    if intercept:
        text += f" ({intercept})"
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return text[:400]


class PlaywrightSurface:
    def __init__(self, page: Page, *, base_url: str = "", timeout_ms: int = 10_000, mask: Sequence[Target] = ()):
        self.page = page
        self.base_url = base_url
        self.timeout_ms = timeout_ms
        self.mask = list(mask)
        page.set_default_timeout(timeout_ms)

    @classmethod
    @contextmanager
    def launch(cls, base_url: str = "", *, headless: bool = True, timeout_ms: int = 10_000,
               mask: Sequence[Target] = ()) -> Iterator[PlaywrightSurface]:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                yield cls(page, base_url=base_url, timeout_ms=timeout_ms, mask=mask)
            finally:
                browser.close()

    # ------------------------------------------------------------ frames

    def _walk_frames(self, frame: Frame | None = None, path: tuple[str, ...] = ()) -> Iterator[tuple[Frame, FramePath]]:
        frame = frame or self.page.main_frame
        yield frame, list(path)
        for i, child in enumerate(_live_children(frame)):
            yield from self._walk_frames(child, path + (child.name or f"[{i}]",))

    def _frame(self, path: FramePath) -> Frame:
        frame = self.page.main_frame
        for seg in path:
            kids = _live_children(frame)
            if m := re.fullmatch(r"\[(\d+)\]", seg):
                idx = int(m.group(1))
                found = kids[idx] if idx < len(kids) else None
            else:
                found = next((f for f in kids if f.name == seg), None)
            if found is None:
                raise FrameNotFound("/".join(path))
            frame = found
        return frame

    def _wait_frame_ready(self, path: FramePath, timeout_ms: float) -> None:
        if not path:
            return
        try:
            self.page.wait_for_function(_FRAME_READY_JS, arg=path, timeout=max(timeout_ms, 1))
        except PlaywrightTimeoutError:
            pass  # resolution will report zero matches

    def _await_frame(self, path: FramePath, timeout_ms: float) -> Frame:
        """The frame once its document has parsed *and* Playwright has registered it.

        The page's JS can see a new frame slightly before Playwright's frame tree does, so after the
        DOM-side check we also wait on frame navigation events (re-checking at least every 250 ms,
        in case the event fired just before we started listening), bounded by the deadline.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        self._wait_frame_ready(path, timeout_ms)
        while True:
            try:
                return self._frame(path)
            except FrameNotFound:
                remaining = (deadline - time.monotonic()) * 1000
                if remaining <= 0:
                    raise
                try:
                    self.page.wait_for_event("framenavigated", timeout=min(remaining, 250))
                except PlaywrightTimeoutError:
                    pass

    # ------------------------------------------------------------ resolution

    def _locator(self, c: LocatorCandidate) -> Locator:
        frame = self._frame(c.frame_path)
        if c.strategy is Strategy.ROLE_NAME:
            loc = frame.get_by_role(c.role, name=c.value, exact=True)
        elif c.strategy is Strategy.LABEL:
            table_caption = frame.locator("xpath=" + _caption_value_cell_xpath(c.value) + _XPATH_LABELABLE)
            loc = frame.get_by_label(c.value, exact=True).or_(table_caption)
        elif c.strategy is Strategy.TEXT_NEAR:
            xpath = _row_column_cell_xpath(c.value, c.column) if c.column else _caption_value_cell_xpath(c.value)
            loc = frame.locator("xpath=" + xpath)
            if c.role:
                loc = loc.get_by_role(c.role)
        elif c.strategy is Strategy.CSS:
            loc = frame.locator(c.value)
        else:
            raise ValueError("coords candidates have no locator")
        return loc.filter(visible=True)

    def _wait_until_any(self, target: Target, timeout_ms: float) -> None:
        """Explicit wait: until any structural candidate in the first candidate's frame is visible."""
        structural = [c for c in target.candidates if c.strategy is not Strategy.COORDS]
        if not structural:
            return
        deadline = time.monotonic() + timeout_ms / 1000
        frame_path = structural[0].frame_path
        try:
            self._await_frame(frame_path, timeout_ms)
            locs = [self._locator(c) for c in structural if c.frame_path == frame_path]
        except FrameNotFound:
            return
        remaining = max((deadline - time.monotonic()) * 1000, 1)
        try:
            functools.reduce(Locator.or_, locs).first.wait_for(state="visible", timeout=remaining)
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as exc:
            if not _is_navigation_race(exc):
                raise

    def resolve(self, target: Target, timeout_ms: float | None = None) -> Resolved:
        """First candidate with exactly one visible match wins. Raises TargetResolutionError otherwise."""
        self._wait_until_any(target, timeout_ms if timeout_ms is not None else self.timeout_ms)
        attempts: list[CandidateAttempt] = []
        for i, c in enumerate(target.candidates):
            if c.strategy is Strategy.COORDS:
                attempts.append(CandidateAttempt(index=i, strategy=c.strategy, matches=1))
                return Resolved(Resolution(matched_index=i, strategy=c.strategy, attempts=attempts), point=c.point())
            try:
                loc = self._locator(c)
                count = loc.count()
            except FrameNotFound:
                count = 0
            except PlaywrightError as exc:
                if not _is_navigation_race(exc):
                    raise
                count = 0  # the frame navigated away or was removed mid-count: nothing to match right now
            attempts.append(CandidateAttempt(index=i, strategy=c.strategy, matches=count))
            if count == 1:
                return Resolved(Resolution(matched_index=i, strategy=c.strategy, attempts=attempts), locator=loc)
        raise TargetResolutionError(target, Resolution(matched_index=None, strategy=None, attempts=attempts))

    # ------------------------------------------------------------ observe

    def _frame_elements(self, frame: Frame, path: FramePath, frame_order: int) -> list[tuple[tuple[int, int], dict]]:
        found = []
        for role in INTERACTIVE_ROLES:
            loc = frame.get_by_role(role)
            if loc.count() == 0:
                continue
            for i, info in enumerate(loc.evaluate_all(_ELEMENT_JS)):
                item = loc.nth(i)
                box = item.bounding_box(timeout=self.timeout_ms)
                found.append(((frame_order, info["order"]), {
                    "role": role,
                    "name": _snapshot_name(item.aria_snapshot(timeout=self.timeout_ms)),
                    "label": info["label"],
                    "nearby_text": info["near"],
                    "frame_path": path,
                    "bbox": BBox(**box) if box else None,
                    "options": info["options"],
                    "occluded": info["occluded"],
                    "field_name": info["fieldName"],
                }))
        cells = frame.locator("xpath=" + _LEAF_CELLS_XPATH).filter(visible=True)
        for i, info in enumerate(cells.evaluate_all(_CELL_JS)):
            if not info["keep"]:
                continue
            box = cells.nth(i).bounding_box(timeout=self.timeout_ms)
            found.append(((frame_order, info["order"]), {
                "role": "cell", "name": "", "label": info["label"], "column": info["column"], "text": info["text"],
                "nearby_text": info["near"], "frame_path": path, "bbox": BBox(**box) if box else None,
            }))
        return found

    def _settle(self) -> None:
        """Give an in-flight load up to the surface timeout, then observe whatever is there.

        A slow page is not an error at this layer: callers decide (replay backs off and re-checks).
        """
        try:
            self.page.wait_for_load_state("load", timeout=self.timeout_ms)
        except PlaywrightTimeoutError:
            pass

    def _observe_once(self, screenshot_path: Path | None) -> Observation:
        self._settle()
        frames: list[FrameInfo] = []
        raw: list[tuple[tuple[int, int], dict]] = []
        texts: list[str] = []
        for order, (frame, path) in enumerate(self._walk_frames()):
            try:
                info = FrameInfo(path=path, url=frame.url, title=frame.title())
                found = self._frame_elements(frame, path, order)
                text = _clean(frame.evaluate(_BODY_TEXT_JS))
            except PlaywrightError as exc:
                if not path or not _is_navigation_race(exc):
                    raise  # the top document racing is retried by observe()
                continue  # a child frame detached mid-read: it is no longer part of the screen
            frames.append(info)
            raw.extend(found)
            if text:
                texts.append(f"[{'/'.join(path) or 'top'}] {text[:_TEXT_PER_FRAME]}")
        raw.sort(key=lambda item: item[0])
        elements = [ElementInfo(index=i, **fields) for i, (_, fields) in enumerate(raw)]
        if screenshot_path is not None:
            self.screenshot(screenshot_path)
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            frames=frames,
            elements=elements,
            visible_text="\n".join(texts),
            screenshot_path=screenshot_path,
            state_hash=_state_hash(frames, elements),
        )

    def observe(self, screenshot_path: Path | None = None) -> Observation:
        for attempt in range(_OBSERVE_ATTEMPTS):
            try:
                return self._observe_once(screenshot_path)
            except PlaywrightError as exc:
                if not _is_navigation_race(exc) or attempt == _OBSERVE_ATTEMPTS - 1:
                    raise
                self._settle()
        raise AssertionError("unreachable")

    # ------------------------------------------------------------ screenshot / extract

    def _mask_locators(self) -> list[Locator]:
        locs = [f.locator("input[type=password]") for f in self.page.frames]
        for target in self.mask:
            for c in target.candidates:
                if c.strategy is Strategy.COORDS:
                    continue
                try:
                    locs.append(self._locator(c))  # over-masking is fine: every candidate, no uniqueness check
                except FrameNotFound:
                    continue
        return locs

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), mask=self._mask_locators(), mask_color="#000000")
        return path

    def locate(self, target: Target, timeout_ms: float = 0) -> Resolution:
        """Resolution only: does the target resolve uniquely right now (or within timeout_ms)?"""
        try:
            return self.resolve(target, timeout_ms=timeout_ms).resolution
        except TargetResolutionError as exc:
            return exc.resolution

    def current_url(self) -> str:
        return self.page.url

    def frame_url(self, frame_path: FramePath) -> str | None:
        try:
            return self._frame(frame_path).url
        except FrameNotFound:
            return None

    def wait_for_navigation(self, timeout_ms: float) -> bool:
        """Explicit event wait: True if any frame navigated within timeout_ms."""
        try:
            self.page.wait_for_event("framenavigated", timeout=max(timeout_ms, 1))
            return True
        except PlaywrightTimeoutError:
            return False

    def debug_snapshot(self) -> dict[str, str]:
        aria, dom = [], []
        for frame, path in self._walk_frames():
            name = "/".join(path) or "top"
            try:
                aria.append(f"## frame {name} ({frame.url})\n" + frame.locator(":root").aria_snapshot(timeout=2000))
                dom.append(f"<!-- frame {name} ({frame.url}) -->\n" + frame.content())
            except PlaywrightError as exc:
                aria.append(f"## frame {name}: unavailable ({_error_text(exc)})")
        return {"accessibility": "\n\n".join(aria), "dom": "\n\n".join(dom)}

    def _path_of(self, frame: Frame) -> FramePath:
        path: list[str] = []
        while frame.parent_frame is not None:
            parent = frame.parent_frame
            siblings = _live_children(parent)
            path.append(frame.name or f"[{siblings.index(frame) if frame in siblings else 0}]")
            frame = parent
        return list(reversed(path))

    def enable_capture(self, callback: Callable[[FramePath, dict], None]) -> None:
        """Report DOM clicks/changes (via capture.js in every frame) and frame navigations to `callback`.

        Events are delivered whenever Python is inside a Playwright call; use pump_events while waiting.
        """
        script = _CAPTURE_JS.read_text()
        context = self.page.context
        context.expose_binding("__cuaCapture", lambda source, payload: callback(self._path_of(source["frame"]),
                                                                                 payload))
        context.add_init_script(script=script)
        for frame in self.page.frames:  # init scripts only reach documents loaded from now on
            try:
                frame.evaluate(script)
            except PlaywrightError:
                pass
        self.page.on("framenavigated", lambda frame: callback(
            self._path_of(frame), {"kind": "navigate", "url": frame.url, "at": time.time() * 1000}))

    def flush_capture(self) -> None:
        for frame, _ in self._walk_frames():
            try:
                frame.evaluate("() => window.__cuaFlush && window.__cuaFlush()")
            except PlaywrightError:
                pass

    def pump_events(self, ms: float) -> None:
        """Let Playwright deliver pending browser events for up to `ms` (used while a human has control)."""
        self.page.wait_for_timeout(ms)

    def extract(self, target: Target) -> str:
        resolved = self.resolve(target)
        if resolved.locator is None:
            raise ValueError("cannot extract from a coords target")
        return _clean(resolved.locator.evaluate(_READ_JS))

    # ------------------------------------------------------------ act

    def act(self, action: Action) -> ActionResult:
        start = time.monotonic()
        resolution: Resolution | None = None
        extracted: str | None = None
        secret = action.value if isinstance(action, Fill) and action.sensitive else None

        def result(ok: bool, code: ErrorCode | None = None, error: str | None = None) -> ActionResult:
            return ActionResult(
                kind=action.kind, ok=ok, resolution=resolution, extracted=extracted, error_code=code,
                error=error, duration_ms=int((time.monotonic() - start) * 1000),
            )

        try:
            if isinstance(action, WaitFor):
                resolution = self._wait_for(action)
            elif isinstance(action, Navigate):
                self.page.goto(urljoin(self.base_url, action.url), wait_until="load")
            elif isinstance(action, Press) and action.target is None:
                self.page.keyboard.press(action.key)
            else:
                resolved = self.resolve(action.target)
                resolution = resolved.resolution
                extracted = self._perform(action, resolved)
        except TargetResolutionError as exc:
            resolution = exc.resolution
            code = ErrorCode.TIMEOUT if isinstance(action, WaitFor) else exc.code
            return result(False, code, str(exc))
        except FrameNotFound as exc:
            code = ErrorCode.TIMEOUT if isinstance(action, WaitFor) else ErrorCode.TARGET_NOT_FOUND
            return result(False, code, f"frame not found: {exc}")
        except (PlaywrightTimeoutError, AssertionError) as exc:
            obstructed = "intercepts pointer events" in str(exc)
            return result(False, ErrorCode.OBSTRUCTED if obstructed else ErrorCode.TIMEOUT, _error_text(exc, secret))
        except PlaywrightError as exc:
            code = ErrorCode.NAVIGATION_FAILED if isinstance(action, Navigate) else ErrorCode.ACTION_FAILED
            return result(False, code, _error_text(exc, secret))
        return result(True)

    def _perform(self, action: Action, resolved: Resolved) -> str | None:
        loc = resolved.locator
        if isinstance(action, Click):
            if loc is None:
                self.page.mouse.click(*resolved.point)
            else:
                loc.click()
            return None
        if loc is None:
            raise PlaywrightError(f"{action.kind} needs a structural target, not coords")
        if isinstance(action, Fill):
            loc.fill(action.value)
        elif isinstance(action, Select):
            loc.select_option(label=action.option)
        elif isinstance(action, Press):
            loc.press(action.key)
        elif isinstance(action, Extract):
            return _clean(loc.evaluate(_READ_JS))
        return None

    def _wait_for(self, action: WaitFor) -> Resolution | None:
        timeout = action.timeout_ms if action.timeout_ms is not None else self.timeout_ms
        if action.target is not None and action.state == "visible":
            return self.resolve(action.target, timeout).resolution
        if action.target is not None:
            first = next(c for c in action.target.candidates if c.strategy is not Strategy.COORDS)
            loc = self._locator(first)
        else:
            loc = self._await_frame(action.frame_path, timeout).get_by_text(action.text).filter(visible=True)
        if action.state == "visible":
            loc.first.wait_for(state="visible", timeout=timeout)
        else:
            expect(loc).to_have_count(0, timeout=timeout)
        return None


def _state_hash(frames: list[FrameInfo], elements: list[ElementInfo]) -> str:
    """Same screen => same hash, regardless of data shown or values typed."""
    signature = {
        "frames": [[f.path, urlsplit(f.url).path] for f in frames],
        "elements": [[e.frame_path, e.role, e.name, e.label, e.column] for e in elements],  # never e.text
    }
    return hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:16]
