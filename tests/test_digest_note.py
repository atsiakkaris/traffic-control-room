import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))
from digest import _render_note_html


def test_bullets_directly_under_a_text_line_still_become_a_list():
    html = _render_note_html("Good news!\n- one\n- two")
    assert html.count("<ul") == 1 and html.count("<li") == 2
    assert "<p" in html and "- one" not in html


def test_blank_line_separated_bullets_unchanged():
    html = _render_note_html("Intro\n\n- one\n- two")
    assert html.count("<ul") == 1 and html.count("<li") == 2


def test_plain_text_and_html_passthrough():
    assert "<ul" not in _render_note_html("just text\nmore text")
    assert _render_note_html("<p>x</p>") == "<p>x</p>"
