from __future__ import annotations

import pytest

from automation.logs import RunLog, read_events
from automation.policy import Redactor
from automation.surface import WebSurface


def test_registered_values_redacted_everywhere_in_nested_data():
    r = Redactor()
    r.add("10001", "member_id")
    r.add("Pw-secret-99", "secret")
    data = {
        "a": "member 10001 opened",
        "b": ["x10001y", {"c": "Pw-secret-99!", "d": ("10001",)}],
        "n": 10001,
        "path": "screenshots/0001-s1.png",
    }
    assert r.obj(data) == {
        "a": "member [REDACTED:member_id] opened",
        "b": ["x[REDACTED:member_id]y", {"c": "[REDACTED:secret]!", "d": ["[REDACTED:member_id]"]}],
        "n": 10001,
        "path": "screenshots/0001-s1.png",
    }


def test_longest_registered_value_wins_and_short_values_are_ignored():
    r = Redactor()
    r.add("123", "short")
    r.add("12345", "long")
    r.add("ab", "tiny")
    assert r.text("12345 and 123 and ab") == "[REDACTED:long] and [REDACTED:short] and ab"


@pytest.mark.parametrize("text, expected", [
    ("SSN 123-45-6789 on file", "SSN [SSN] on file"),
    ("card 4111 1111 1111 1111", "card [CARD]"),
    ("card 4111-1111-1111-1111.", "card [CARD]."),
    ("account 4821930571", "account [ACCOUNT]"),
    ("routing 021000021 acct 12345678901234567", "routing [ACCOUNT] acct [ACCOUNT]"),
    ("New Account Number:\t4821930571\n", "New Account Number:\t[ACCOUNT]\n"),
])
def test_financial_identifier_patterns(text, expected):
    assert Redactor().text(text) == expected


@pytest.mark.parametrize("text", [
    "opened 2026-09-15",
    "call 555-1234",
    "balance 12,450.33",
    "balance 12345678.90",
    "member 1234567",
    "ref 12-345-6789",
    "version 1.2.3",
    "SSN-like 123-45-67890",
])
def test_near_misses_are_left_alone(text):
    assert Redactor().text(text) == text


def test_redaction_is_idempotent():
    r = Redactor()
    r.add("Jane", "name")
    text = "Jane 123-45-6789 4821930571"
    assert r.text(r.text(text)) == r.text(text)


def test_runlog_redacts_payloads_and_tracebacks(tmp_path):
    r = Redactor()
    r.add("Pw-secret-99", "secret")
    log = RunLog(tmp_path, r)
    log.emit("error", {"error_type": "ValueError", "message": "login with Pw-secret-99 failed",
                       "traceback": 'File "x.py"\n  password = "Pw-secret-99"  # ssn 123-45-6789'}, actor="system")
    log.write_text("frames/0001-main.html", "<td>123-45-6789</td><td>Pw-secret-99</td>")
    log.close()
    raw = (log.dir / "events.jsonl").read_text() + (log.dir / "frames/0001-main.html").read_text()
    assert "Pw-secret-99" not in raw and "123-45-6789" not in raw
    assert read_events(log.dir)[0].data["message"] == "login with [REDACTED:secret] failed"


def test_generated_locators_never_embed_input_values():
    element = {"tag": "a", "type": None, "role": "link", "name": "10001", "name_source": "text", "label": None,
               "row_header": None, "col_header": None, "text": "10001", "css": "body > a:nth-of-type(3)",
               "href": "http://h/member?mid=10001", "options": None}
    target = WebSurface.target_for(None, element, ["main"], avoid=["10001"])
    assert [s.by for s in target.strategies] == ["css"]
    assert "10001" not in target.model_dump_json(exclude={"fingerprint"})
