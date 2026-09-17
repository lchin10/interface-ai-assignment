"""WebSurface against the hostile mock markup: element index, every strategy kind, uniqueness,
fingerprints, masking, conditions and human-action capture."""
from __future__ import annotations

import pytest
from conftest import PASSWORD, USER

from automation.schema import Condition, Target
from automation.surface import TargetError


@pytest.fixture
def signed_in(mockbank, surface_factory):
    def open_(*faults: str):
        url = mockbank(*faults)
        s = surface_factory()
        s.page.goto(url + "/login")
        s.page.fill("input[name=u]", USER)
        s.page.fill("input[name=p]", PASSWORD)
        s.page.click("input[type=submit]")
        for _ in range(100):
            if any(e.frame == ["main"] and e.tag == "input" for e in s.observe().elements):
                break
            s.pump(50)
        return s, url

    return open_


def goto_detail(s, member="10001"):
    main = s.page.frame(name="main")
    main.fill("input[name=mid]", member)
    main.click("input[type=submit]")
    main.wait_for_selector("text=Member Detail")


def target(data) -> Target:
    return Target.model_validate(data)


def test_element_index_spans_frames_and_derives_legacy_labels(signed_in):
    s, _ = signed_in()
    obs = s.observe()
    by_frame = {tuple(e.frame) for e in obs.elements}
    assert {("nav",), ("main",)} <= by_frame
    member = next(e for e in obs.elements if e.tag == "input" and e.type == "text")
    assert (member.role, member.label, member.name, member.frame) == ("textbox", "Member ID:", None, ["main"])
    search = next(e for e in obs.elements if e.role == "button")
    assert (search.name, search.name_source) == ("Search", "value")
    link = next(e for e in obs.elements if e.role == "link" and e.text == "Sign Off")
    assert link.href.endswith("/logout")
    assert len({e.ref for e in obs.elements}) == len(obs.elements)
    assert s.page.frame(name="main").get_attribute("input[name=mid]", "data-automation-ref") == member.ref
    assert '[main] ' not in obs.to_prompt() and 'textbox(text) "Member ID" frame=main' in obs.to_prompt()


def test_table_cells_get_row_and_column_keys(signed_in):
    s, _ = signed_in()
    goto_detail(s)
    obs = s.observe()
    balance = next(e for e in obs.elements if e.text == "12,450.33")
    assert (balance.row_header, balance.col_header) == ("Savings", "Balance")
    tax = next(e for e in obs.elements if e.text == "123-45-6789")
    assert tax.label == "Tax ID:"


def test_observation_hash_tracks_screen_changes(signed_in):
    s, _ = signed_in()
    first, again = s.observe(), s.observe()
    assert first.hash == again.hash
    goto_detail(s)
    assert s.observe().hash != first.hash


def test_verified_targets_keep_only_unique_live_strategies(signed_in):
    s, _ = signed_in()
    obs = s.observe()
    member = next(e for e in obs.elements if e.tag == "input" and e.type == "text")
    t = s.verified_target_for(member)
    assert [x.by for x in t.strategies] == ["adjacent_label", "css"]
    assert t.fingerprint.model_dump() == {"tag": "input", "type": "text", "role": "textbox", "name": "Member ID"}
    button = s.verified_target_for(next(e for e in obs.elements if e.role == "button"))
    assert [x.by for x in button.strategies] == ["role", "css"]
    link = s.verified_target_for(next(e for e in obs.elements if e.text == "Member Search" and e.role == "link"))
    assert [x.by for x in link.strategies] == ["role", "text", "css"]


@pytest.mark.parametrize("strategy, expect", [
    ({"by": "role", "role": "button", "name": "Open Sub-Account"}, "input"),
    ({"by": "adjacent_label", "text": "Tax ID:", "control": "cell"}, "123-45-6789"),
    ({"by": "table_cell", "row": "Checking", "column": "Balance"}, "1,203.10"),
    ({"by": "text", "text": "Member Detail"}, "Member Detail"),
    ({"by": "css", "value": "table[border='1'] tr:nth-of-type(2) td:nth-of-type(1)"}, "S01"),
])
def test_each_strategy_kind_resolves(signed_in, strategy, expect):
    s, _ = signed_in()
    goto_detail(s)
    resolved = s.resolve(target({"frame": ["main"], "strategies": [strategy]}), 2000)
    assert resolved.match_count == 1 and resolved.strategy_index == 0 and resolved.drift == []
    assert expect in (resolved.element["text"] or resolved.element["tag"])


def test_ambiguous_match_is_refused(signed_in):
    s, _ = signed_in()
    goto_detail(s)
    with pytest.raises(TargetError) as exc:
        s.resolve(target({"frame": ["main"], "strategies": [{"by": "css", "value": "td[align=right]"}]}), 300)
    assert exc.value.code == "TARGET_AMBIGUOUS"
    assert "css=2" in str(exc.value)


def test_fingerprint_mismatch_rejects_wrong_kind_of_element(signed_in):
    s, _ = signed_in()
    t = target({"frame": ["main"], "strategies": [{"by": "adjacent_label", "text": "Member ID:"}],
                "fingerprint": {"tag": "select"}})
    with pytest.raises(TargetError, match="fingerprint mismatch"):
        s.resolve(t, 300)


def test_missing_frame_is_reported(signed_in):
    s, _ = signed_in()
    with pytest.raises(TargetError, match="frame reports not present"):
        s.resolve(target({"frame": ["reports"], "strategies": [{"by": "css", "value": "input"}]}), 200)


def test_resolve_waits_for_late_elements(signed_in):
    s, _ = signed_in("slow")
    main = s.page.frame(name="main")
    main.fill("input[name=mid]", "10001")
    main.click("input[type=submit]", no_wait_after=True)
    resolved = s.resolve(target({"frame": ["main"], "strategies": [
        {"by": "table_cell", "row": "Savings", "column": "Balance"}]}), 5000)
    assert resolved.element["text"] == "12,450.33"


def test_conditions(signed_in):
    s, url = signed_in()
    assert s.check(Condition(text_visible="Sign Off"))  # nav frame
    assert s.check(Condition(url_matches=r"/search$"))
    assert not s.check(Condition(text_visible="Member Detail"))
    present = {"element_present": {"frame": ["main"], "strategies": [{"by": "role", "role": "button", "name": "Search"}]}}
    assert s.check(Condition.model_validate(present))
    assert s.check(Condition.model_validate({"all": [{"text_visible": "Member Search"}, present]}))
    assert s.check(Condition.model_validate({"any": [{"text_visible": "nope"}, {"url_matches": "/nav"}]}))
    assert not s.check(Condition.model_validate({"all": [{"text_visible": "nope"}, present]}))


def test_masking_covers_screenshots_and_element_text(signed_in, tmp_path, app_profile):
    s, _ = signed_in()
    goto_detail(s)
    plain, masked = tmp_path / "plain.png", tmp_path / "masked.png"
    s.screenshot(plain, [])
    s.screenshot(masked, app_profile.mask)
    assert plain.read_bytes() != masked.read_bytes()
    obs = s.observe()
    hidden = s.masked_refs(app_profile.mask)
    assert {e.text for e in obs.elements if e.ref in hidden} == {"Jane Q. Sample", "123-45-6789"}


def test_frame_html_strips_value_attributes(signed_in):
    s, _ = signed_in()
    goto_detail(s)
    html = s.frame_html()
    assert set(html) == {"top", "nav", "main"}
    assert "Member Detail" in html["main"] and "10001" not in html["main"]
    assert "[value removed]" in html["main"]


def test_visible_text_per_frame(signed_in):
    s, _ = signed_in()
    texts = s.visible_text()
    assert texts["top"] == "" and "Sign Off" in texts["nav"] and "Member ID:" in texts["main"]


def test_human_events_are_captured_with_frame_and_descriptor(signed_in):
    s, _ = signed_in()
    seen = []
    s.on_human_event(lambda frame, payload: seen.append((frame, payload)))
    main = s.page.frame(name="main")
    main.fill("input[name=mid]", "10002")
    main.click("input[type=submit]")
    main.wait_for_selector("text=Member Detail")
    s.pump(200)
    kinds = [(f, p["kind"]) for f, p in seen]
    assert ("main", "fill") in kinds and ("main", "click") in kinds and ("main", "navigate") in kinds
    assert kinds.index(("main", "fill")) < kinds.index(("main", "click")) < kinds.index(("main", "navigate"))
    fill = next(p for f, p in seen if p["kind"] == "fill")
    assert fill["value"] == "10002" and fill["element"]["label"] == "Member ID:"
    s.on_human_event(None)


def test_password_values_are_never_captured(mockbank, surface_factory):
    url = mockbank()
    s = surface_factory()
    seen = []
    s.on_human_event(lambda frame, payload: seen.append(payload))
    s.page.goto(url + "/login")
    s.page.fill("input[name=p]", PASSWORD)
    s.page.click("input[type=submit]")
    s.pump(300)
    fills = [p for p in seen if p["kind"] == "fill"]
    assert fills and all(p["value"] is None for p in fills)
