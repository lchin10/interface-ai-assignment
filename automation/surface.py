"""The perception/action seam.

`Surface` is what the recorded flow needs from *any* UI: an element index to observe,
target resolution, a handful of actions, condition checks, screenshots, and a hook for
human actions. `WebSurface` implements it with Playwright. A desktop implementation would
fill the same element index from UI Automation / AT-SPI; the artifact and replay engine
would not change.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from playwright.sync_api import Browser, ElementHandle, Frame, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .schema import (
    ActionKind,
    AdjacentLabelStrategy,
    Condition,
    CssStrategy,
    Fingerprint,
    RoleStrategy,
    Strategy,
    TableCellStrategy,
    Target,
    TextStrategy,
    describe_strategy,
)

ROLE_NAME_SOURCES = {"aria", "label", "value", "text", "title", "placeholder"}
ROLES_WITH_NAMES = {"link", "button", "textbox", "combobox", "checkbox", "radio", "spinbutton"}

# Shared by the element index, element description and human-action capture, so all three
# describe an element identically.
_HELPERS_JS = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const visible = el => {
  const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
};
const cssPath = el => {
  const parts = [];
  while (el && el.nodeType === 1 && el.tagName !== 'HTML') {
    const tag = el.tagName.toLowerCase();
    if (tag === 'body') { parts.unshift('body'); break; }
    let i = 1, sib = el;
    while ((sib = sib.previousElementSibling)) if (sib.tagName === el.tagName) i++;
    parts.unshift(`${tag}:nth-of-type(${i})`);
    el = el.parentElement;
  }
  return parts.join(' > ');
};
const roleOf = el => {
  const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || 'text').toLowerCase();
  if (el.getAttribute('role')) return el.getAttribute('role');
  if (t === 'a' && el.hasAttribute('href')) return 'link';
  if (t === 'button' || (t === 'input' && ['submit', 'button', 'reset', 'image'].includes(ty))) return 'button';
  if (t === 'input' && ty === 'checkbox') return 'checkbox';
  if (t === 'input' && ty === 'radio') return 'radio';
  if (t === 'input' && ty === 'number') return 'spinbutton';
  if (t === 'input' && ty === 'password') return 'password';
  if (t === 'input' || t === 'textarea') return 'textbox';
  if (t === 'select') return 'combobox';
  if (t === 'td') return 'cell';
  if (t === 'th') return 'columnheader';
  return 'text';
};
const nameOf = (el, role) => {
  const aria = el.getAttribute('aria-label'); if (aria) return [norm(aria), 'aria'];
  if (el.id) { const l = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return [norm(l.innerText), 'label']; }
  const wrap = el.closest('label'); if (wrap && wrap !== el) return [norm(wrap.innerText), 'label'];
  if (role === 'button' && el.tagName === 'INPUT') return [norm(el.value), 'value'];
  if (['link', 'button', 'cell', 'columnheader', 'text'].includes(role)) return [norm(el.innerText), 'text'];
  if (el.getAttribute('title')) return [norm(el.getAttribute('title')), 'title'];
  if (el.getAttribute('placeholder')) return [norm(el.getAttribute('placeholder')), 'placeholder'];
  return [null, null];
};
const cellText = c => c ? norm(c.innerText) : '';
const labelOf = el => {
  const cell = el.matches('td,th') ? el : el.closest('td,th');
  if (!cell) return null;
  let prev = cell.previousElementSibling;
  while (prev && !cellText(prev)) prev = prev.previousElementSibling;
  return prev ? cellText(prev) : null;
};
const headersOf = el => {
  if (!el.matches('td')) return [null, null];
  const row = el.parentElement, table = el.closest('table');
  // row key: the first other cell that reads like a label (no digits), else the first other cell
  const others = Array.from(row.cells).filter(c => c !== el).map(cellText).filter(Boolean);
  const rowHeader = others.find(t => !/\d/.test(t)) || others[0] || null;
  let colHeader = null;
  for (const r of table.rows) {
    if (r.querySelector(':scope > th')) { const h = r.cells[el.cellIndex]; colHeader = h ? cellText(h) : null; break; }
  }
  return [rowHeader || null, colHeader || null];
};
const describe = el => {
  const role = roleOf(el);
  const [name, nameSource] = nameOf(el, role);
  const [rowHeader, colHeader] = headersOf(el);
  const interactive = el.matches('a[href],button,input,select,textarea');
  return {
    tag: el.tagName.toLowerCase(),
    type: el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text').toLowerCase() : null,
    role, name, name_source: nameSource,
    label: labelOf(el), row_header: rowHeader, col_header: colHeader,
    text: interactive && el.tagName !== 'A' && el.tagName !== 'BUTTON' ? null : norm(el.innerText).slice(0, 120),
    css: cssPath(el),
    href: el.tagName === 'A' && el.hasAttribute('href') ? el.href : null,
    options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => norm(o.text)) : null,
  };
};
"""

_INDEX_JS = "(offset) => {" + _HELPERS_JS + r"""
  const out = [];
  const sel = 'a[href],button,input,select,textarea,td,th,b,strong,font,h1,h2,h3,h4,span,p,label,li';
  for (const el of document.querySelectorAll(sel)) {
    if (el.tagName === 'INPUT' && el.type === 'hidden') continue;
    if (!visible(el)) continue;
    const interactive = el.matches('a[href],button,input,select,textarea');
    if (!interactive) {
      if (el.querySelector('a[href],button,input,select,textarea,table')) continue;
      const text = norm(el.innerText);
      if (!text) continue;
      if (!el.matches('td,th')) {
        if (el.children.length) continue;
        const cell = el.closest('td,th');
        if (cell && cellText(cell) === text) continue;
      }
    }
    const ref = 'e' + (offset + out.length + 1);
    el.setAttribute('data-automation-ref', ref);
    out.push(Object.assign({ref}, describe(el)));
    if (out.length >= 400) break;
  }
  return out;
}"""

_DESCRIBE_JS = "(el) => {" + _HELPERS_JS + " return describe(el); }"

_CAPTURE_JS = "(() => {" + _HELPERS_JS + r"""
  const send = p => { try { window.__automationHumanEvent(p); } catch (e) {} };
  document.addEventListener('click', ev => {
    const el = ev.target.closest && ev.target.closest('a[href],button,input[type=submit],input[type=button],input[type=checkbox],input[type=radio]');
    if (el) send({kind: 'click', element: describe(el)});
  }, true);
  document.addEventListener('change', ev => {
    const el = ev.target;
    if (!el.matches || !el.matches('input,select,textarea')) return;
    const kind = el.tagName === 'SELECT' ? 'select' : 'fill';
    send({kind, element: describe(el), value: el.type === 'password' ? null : el.value});
  }, true);
})();"""


class TargetError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Element:
    ref: str
    frame: list[str]
    tag: str
    type: str | None
    role: str
    name: str | None
    name_source: str | None
    label: str | None
    row_header: str | None
    col_header: str | None
    text: str | None
    css: str
    href: str | None
    options: list[str] | None

    def display(self) -> str:
        name = self.name or (self.label or "").rstrip(":") or ""
        parts = [f"[{self.ref}] {self.role}"]
        if self.type and self.role in ("textbox", "password"):
            parts[0] += f"({self.type})"
        if self.role in ("cell", "columnheader", "text"):
            parts.append(json.dumps(self.text))
        else:
            parts.append(json.dumps(name))
        if self.label and self.role in ("cell",) and not self.row_header:
            parts.append(f"label={json.dumps(self.label)}")
        if self.row_header:
            parts.append(f"row={json.dumps(self.row_header)}")
        if self.col_header:
            parts.append(f"column={json.dumps(self.col_header)}")
        if self.options:
            parts.append(f"options={json.dumps(self.options)}")
        parts.append(f"frame={'/'.join(self.frame) or 'top'}")
        return " ".join(parts)


@dataclass
class Observation:
    url: str
    frames: dict[str, str]
    elements: list[Element]
    hash: str

    def element(self, ref: str) -> Element | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def to_prompt(self) -> str:
        frames = ", ".join(f"{name} -> {url}" for name, url in self.frames.items())
        lines = "\n".join(e.display() for e in self.elements)
        return f"URL: {self.url}\nFrames: {frames}\nElements:\n{lines}"


@dataclass
class Resolved:
    locator: Locator
    frame_url: str
    strategy_index: int
    strategy: Strategy
    match_count: int
    element: dict[str, Any]
    fingerprint_ok: bool
    drift: list[str] = field(default_factory=list)
    handle: ElementHandle | None = None

    def describe(self) -> str:
        # a data cell's text is the data itself, so identify cells by their locator only
        name = "" if self.element.get("tag") in ("td", "th") else (
            self.element.get("name") or self.element.get("label") or "")
        role = self.element.get("role")
        return f'{role} "{name}" via {describe_strategy(self.strategy)}' if name else \
            f"{role} via {describe_strategy(self.strategy)}"


HumanCallback = Callable[[str, dict[str, Any]], None]


class Surface(Protocol):
    @property
    def url(self) -> str: ...
    def observe(self) -> Observation: ...
    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolved: ...
    def target_for(self, element: Element | dict[str, Any], frame: list[str], avoid: list[str] = ...) -> Target: ...
    def verified_target_for(self, element: Element, avoid: list[str] = ...) -> Target: ...
    def masked_refs(self, mask: list[Target]) -> set[str]: ...
    def wait_detached(self, resolved: Resolved, timeout_ms: int) -> bool: ...
    def settle(self, timeout_ms: int = ..., quiet_ms: int = ...) -> bool: ...
    def perform(self, action: ActionKind, *, resolved: Resolved | None = None, url: str | None = None,
                value: str | None = None, wait_ms: int | None = None) -> str | None: ...
    def check(self, condition: Condition, snapshot: dict[str, Any] | None = None) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...
    def visible_text(self) -> dict[str, str]: ...
    def screenshot(self, path: Path, mask: list[Target]) -> None: ...
    def frame_html(self) -> dict[str, str]: ...
    def pump(self, ms: int) -> None: ...
    def on_human_event(self, callback: HumanCallback | None) -> None: ...
    def close(self) -> None: ...


def _xpath_literal(s: str) -> str:
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in s.split('"')) + ")"


def _frame_path(frame: Frame) -> list[str]:
    names = []
    while frame.parent_frame is not None:
        names.insert(0, frame.name)
        frame = frame.parent_frame
    return names


class WebSurface:
    def __init__(self, page: Page, closer: Callable[[], None]):
        self.page = page
        self._closer = closer
        self._closed = False
        self._human: HumanCallback | None = None
        page.context.expose_binding("__automationHumanEvent", self._binding)
        page.context.add_init_script(_CAPTURE_JS)
        page.on("framenavigated", self._navigated)

    @classmethod
    def launch(cls, *, headless: bool = True, browser: Browser | None = None) -> WebSurface:
        pw = None
        if browser is None:
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1280, "height": 860})
        owned_browser = browser if pw else None

        def closer() -> None:
            context.close()
            if owned_browser:
                owned_browser.close()
                pw.stop()

        return cls(context.new_page(), closer)

    # -- frames -----------------------------------------------------------------------
    @property
    def url(self) -> str:
        return self.page.url

    def _frame(self, path: list[str]) -> Frame | None:
        frame = self.page.main_frame
        for name in path:
            # a frameset rebuilt by navigation can leave a detached namesake behind
            frame = next((c for c in frame.child_frames if c.name == name and not c.is_detached()), None)
            if frame is None:
                return None
        return frame

    def _walk(self) -> Iterator[tuple[list[str], Frame]]:
        stack = [([], self.page.main_frame)]
        while stack:
            path, frame = stack.pop(0)
            yield path, frame
            stack.extend((path + [c.name], c) for c in frame.child_frames if not c.is_detached())

    # -- observe ----------------------------------------------------------------------
    def observe(self) -> Observation:
        elements: list[Element] = []
        frames: dict[str, str] = {}
        for path, frame in self._walk():
            frames["/".join(path) or "top"] = frame.url
            try:
                raw = frame.evaluate(_INDEX_JS, len(elements))
            except PlaywrightError:
                continue  # frame mid-navigation; the next observation will see it
            elements.extend(Element(frame=path, **r) for r in raw)
        digest = hashlib.sha256(json.dumps(
            [frames] + [[e.frame, e.role, e.name, e.text, e.css] for e in elements]
        ).encode()).hexdigest()[:16]
        return Observation(self.page.url, frames, elements, digest)

    # -- targeting --------------------------------------------------------------------
    def _locator(self, frame: Frame, s: Strategy) -> Locator:
        match s:
            case RoleStrategy():
                return frame.get_by_role(s.role, name=s.name, exact=True)
            case TextStrategy():
                return frame.get_by_text(s.text, exact=True)
            case AdjacentLabelStrategy():
                cell = f"//td[normalize-space(.)={_xpath_literal(s.text)}]/following-sibling::td[1]"
                tail = {
                    "input": "/descendant::*[self::input[not(@type='hidden' or @type='submit' or @type='button')] or self::textarea][1]",
                    "select": "/descendant::select[1]",
                    "cell": "",
                }[s.control]
                return frame.locator("xpath=" + cell + tail)
            case TableCellStrategy():
                col, row = _xpath_literal(s.column), _xpath_literal(s.row)
                xp = (f"//table[.//th[normalize-space(.)={col}]]//tr[td[normalize-space(.)={row}]]"
                      f"/td[count(ancestor::table[1]//th[normalize-space(.)={col}]/preceding-sibling::th)+1]")
                return frame.locator("xpath=" + xp)
            case CssStrategy():
                return frame.locator(s.value)
        raise TypeError(s)

    def resolve(self, target: Target, timeout_ms: int = 5000) -> Resolved:
        """First strategy (in order) with exactly one match whose tag/type fit the fingerprint."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            counts: list[str] = []
            frame = self._frame(target.frame)
            if frame is not None:
                for i, s in enumerate(target.strategies):
                    loc = self._locator(frame, s)
                    try:
                        n = loc.count()
                        element = loc.evaluate(_DESCRIBE_JS) if n == 1 else None
                    except PlaywrightError:
                        n, element = 0, None
                    counts.append(f"{s.by}={n}")
                    if element is None:
                        continue
                    ok, drift = self._fingerprint(target.fingerprint, element)
                    if not ok:
                        counts[-1] += "(fingerprint mismatch)"
                        continue
                    if i > 0:
                        drift.insert(0, f"fallback strategy #{i} ({describe_strategy(s)}) used; "
                                        f"earlier strategies failed: {', '.join(counts[:-1])}")
                    try:
                        handle = loc.element_handle(timeout=1_000)
                    except PlaywrightError:
                        handle = None
                    return Resolved(loc, frame.url, i, s, n, element, True, drift, handle)
            if time.monotonic() >= deadline:
                where = "/".join(target.frame) or "top"
                if frame is None:
                    raise TargetError("TARGET_NOT_FOUND", f"frame {where} not present")
                code = "TARGET_AMBIGUOUS" if any(re.search(r"=([2-9]|\d\d)", c) for c in counts) else "TARGET_NOT_FOUND"
                raise TargetError(code, f"no unique match in frame {where}: {', '.join(counts)}")
            self.page.wait_for_timeout(100)

    @staticmethod
    def _fingerprint(fp: Fingerprint | None, el: dict[str, Any]) -> tuple[bool, list[str]]:
        if fp is None:
            return True, []
        if fp.tag != el["tag"] or (fp.type is not None and fp.type != el["type"]):
            return False, []
        drift = []
        name = el.get("name") or (el.get("label") or "").rstrip(":") or None
        if fp.name is not None and name != fp.name:
            drift.append(f'element name changed: "{fp.name}" -> "{name}"')
        return True, drift

    def target_for(self, element: Element | dict[str, Any], frame: list[str], avoid: list[str] = ()) -> Target:
        """Candidate strategies for an element, most stable first. Values in `avoid` (inputs) never
        become part of a locator."""
        e = element if isinstance(element, dict) else element.__dict__
        cands: list[Strategy] = []
        if e["role"] in ROLES_WITH_NAMES and e.get("name") and e.get("name_source") in ROLE_NAME_SOURCES:
            cands.append(RoleStrategy(by="role", role=e["role"], name=e["name"]))
        if e.get("row_header") and e.get("col_header"):
            cands.append(TableCellStrategy(by="table_cell", row=e["row_header"], column=e["col_header"]))
        if e.get("label"):
            control = {"select": "select", "td": "cell"}.get(e["tag"], "input")
            if e["tag"] in ("input", "textarea", "select", "td"):
                cands.append(AdjacentLabelStrategy(by="adjacent_label", text=e["label"], control=control))
        if e["role"] == "link" and e.get("text"):
            cands.append(TextStrategy(by="text", text=e["text"]))
        cands.append(CssStrategy(by="css", value=e["css"]))
        cands = [c for c in cands if not any(v and v in c.model_dump_json() for v in avoid)]
        # a data cell's text is the data itself, not an identity: never fingerprint on it
        name = None if e["tag"] in ("td", "th") else (e.get("name") or (e.get("label") or "").rstrip(":") or None)
        fingerprint = Fingerprint(tag=e["tag"], type=e.get("type"), role=e["role"], name=name)
        return Target(frame=frame, strategies=cands, fingerprint=fingerprint)

    def verified_target_for(self, element: Element, avoid: list[str] = ()) -> Target:
        """target_for, keeping only strategies that currently resolve to exactly this element."""
        target = self.target_for(element, element.frame, avoid)
        frame = self._frame(element.frame)
        keep = []
        for s in target.strategies:
            try:
                loc = self._locator(frame, s)
                if loc.count() == 1 and loc.get_attribute("data-automation-ref") == element.ref:
                    keep.append(s)
            except PlaywrightError:
                continue
        if not keep:
            raise TargetError("TARGET_NOT_FOUND", f"no stable locator for {element.ref}")
        return target.model_copy(update={"strategies": keep})

    # -- act --------------------------------------------------------------------------
    def perform(self, action: ActionKind, *, resolved: Resolved | None = None, url: str | None = None,
                value: str | None = None, wait_ms: int | None = None) -> str | None:
        match action:
            case "navigate":
                self.page.goto(url, wait_until="domcontentloaded")
            case "click":
                resolved.locator.click(timeout=10_000)
            case "fill":
                resolved.locator.fill(value, timeout=10_000)
            case "select":
                resolved.locator.select_option(value, timeout=10_000)
            case "extract":
                return " ".join(resolved.locator.inner_text(timeout=10_000).split())
            case "wait":
                self.page.wait_for_timeout(wait_ms or 1000)
        return None

    # -- read -------------------------------------------------------------------------
    def visible_text(self) -> dict[str, str]:
        out = {}
        for path, frame in self._walk():
            try:
                out["/".join(path) or "top"] = frame.evaluate("() => document.body ? document.body.innerText : ''")
            except PlaywrightError:
                out["/".join(path) or "top"] = ""
        return out

    def snapshot(self) -> dict[str, Any]:
        """One reading of the screen. Evaluating every condition against the same snapshot stops two
        conditions from disagreeing about what was on screen because the page moved between checks."""
        return {"texts": list(self.visible_text().values()), "urls": [f.url for _, f in self._walk()]}

    def check(self, condition: Condition, snapshot: dict[str, Any] | None = None) -> bool:
        c = condition
        if c.text_visible is not None:
            # compare on collapsed whitespace: table layouts are full of tabs and newlines
            needle = " ".join(c.text_visible.split())
            texts = snapshot["texts"] if snapshot else self.visible_text().values()
            return any(needle in " ".join(t.split()) for t in texts)
        if c.url_matches is not None:
            urls = snapshot["urls"] if snapshot else [f.url for _, f in self._walk()]
            return any(re.search(c.url_matches, u) for u in urls)
        if c.element_present is not None:
            frame = self._frame(c.element_present.frame)
            if frame is None:
                return False
            for s in c.element_present.strategies:
                try:
                    if self._locator(frame, s).count() >= 1:
                        return True
                except PlaywrightError:
                    pass
            return False
        if c.all is not None:
            return all(self.check(x, snapshot) for x in c.all)
        return any(self.check(x, snapshot) for x in c.any)

    def screenshot(self, path: Path, mask: list[Target]) -> None:
        locators = []
        for target in mask:
            frame = self._frame(target.frame)
            if frame is None:
                continue
            for s in target.strategies:
                loc = self._locator(frame, s)
                try:
                    if loc.count():
                        locators.append(loc)
                        break
                except PlaywrightError:
                    pass
        self.page.screenshot(path=str(path), mask=locators, mask_color="#000000")

    def signature(self) -> str:
        """Cheap fingerprint of what is on screen, used to tell "still loading" from "settled"."""
        parts = []
        for path, frame in self._walk():
            try:
                size = frame.evaluate("() => document.body ? document.body.innerText.length : -1")
            except PlaywrightError:
                size = -2
            parts.append(["/".join(path), frame.url, size])
        return json.dumps(parts)

    def settle(self, timeout_ms: int = 3_000, quiet_ms: int = 250) -> bool:
        """Wait until two consecutive looks agree, so nothing reasons about a half-loaded screen."""
        deadline = time.monotonic() + timeout_ms / 1000
        last = None
        while True:
            current = self.signature()
            if current == last:
                return True
            if time.monotonic() >= deadline:
                return False
            last = current
            self.page.wait_for_timeout(quiet_ms)

    def masked_refs(self, mask: list[Target]) -> set[str]:
        """Element-index refs covered by mask targets (so their text can be withheld too)."""
        refs: set[str] = set()
        for target in mask:
            frame = self._frame(target.frame)
            if frame is None:
                continue
            for s in target.strategies:
                try:
                    found = self._locator(frame, s).evaluate_all(
                        "els => els.map(e => e.getAttribute('data-automation-ref'))")
                except PlaywrightError:
                    continue
                refs.update(r for r in found if r)
        return refs

    def wait_detached(self, resolved: Resolved, timeout_ms: int) -> bool:
        """True once the resolved element is gone (e.g. its frame navigated away)."""
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            try:
                if resolved.handle is None or resolved.handle.evaluate("e => !e.isConnected"):
                    return True
            except PlaywrightError:
                return True
            self.page.wait_for_timeout(50)
        return False

    def frame_html(self) -> dict[str, str]:
        out = {}
        for path, frame in self._walk():
            try:
                html = frame.content()
            except PlaywrightError:
                continue
            out["/".join(path) or "top"] = re.sub(r"(\svalue=)(['\"]).*?\2", r"\1\2[value removed]\2", html)
        return out

    def pump(self, ms: int) -> None:
        self.page.wait_for_timeout(ms)

    # -- human capture ----------------------------------------------------------------
    def on_human_event(self, callback: HumanCallback | None) -> None:
        self._human = callback

    def _binding(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        if self._human:
            self._human("/".join(_frame_path(source["frame"])) or "top", payload)

    def _navigated(self, frame: Frame) -> None:
        if self._human:
            self._human("/".join(_frame_path(frame)) or "top", {"kind": "navigate", "url": frame.url})

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._closer()
