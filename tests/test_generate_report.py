"""
Smoke test for generate_report().

Populates a minimal in-memory-style SQLite fixture and asserts the generated
HTML is structurally valid (no broken f-strings, no orphaned JS, key labels
from ui_labels.yaml are present).
"""

import os
import sys
import uuid
import tempfile
import importlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

# Ensure runner/ is importable
RUNNER = Path(__file__).parent.parent / "runner"
sys.path.insert(0, str(RUNNER))


@pytest.fixture()
def fixture_db(tmp_path, monkeypatch):
    """Create a temporary DB, seed one run, and point db.DB_PATH at it."""
    db_file = tmp_path / "test_history.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    # Force db module to re-read DB_PATH from env
    import db
    importlib.reload(db)

    db.init_db()

    run_id = str(uuid.uuid4())
    run_at = datetime.now(timezone.utc).isoformat()

    db.insert_run(run_id, run_at, {"total": 3, "passed": 2, "failed": 1, "errored": 0})

    for group, test_name, status in [
        ("Traffic Detection", "Traffic Detection Live", "pass"),
        ("VMS",               "VMS Live Data",          "fail"),
        ("Bluetooth",         "Bluetooth Paths Live (FCD)", "pass"),
    ]:
        db.insert_result(
            run_id=run_id, group_name=group, test_name=test_name,
            endpoint="https://example.com/test", method="GET",
            status=status, status_code=200, expected_code=200,
            response_ms=123.4, failure_reason=None,
            check_summary=f"[{'✓' if status == 'pass' else '✗'}] check: ok",
        )

    yield tmp_path, run_id


@pytest.fixture()
def report_html(fixture_db, tmp_path, monkeypatch):
    """Call generate_report() with REPORT_PATH redirected to tmp_path."""
    import report
    importlib.reload(report)

    out_path = tmp_path / "reports" / "latest.html"
    monkeypatch.setattr(report, "REPORT_PATH", out_path)

    path = report.generate_report()
    return Path(path).read_text(encoding="utf-8")


# ── Structural checks ─────────────────────────────────────────────────────────

def test_report_is_html(report_html):
    assert report_html.strip().startswith("<!DOCTYPE") or "<html" in report_html


def test_script_tags_balanced(report_html):
    assert report_html.count("<script") == report_html.count("</script>")


def test_no_unterminated_template_literal(report_html):
    # A raw backtick in the HTML output (outside a <script> block) most likely
    # indicates a broken f-string that leaked a Python expression.
    # This is a heuristic — adjust the threshold if the report legitimately uses backticks.
    outside_scripts = ""
    for chunk in report_html.split("<script"):
        outside_scripts += chunk.split("</script>")[-1]
    assert "`" not in outside_scripts, "Stray backtick found outside <script> blocks"


# ── Content checks ────────────────────────────────────────────────────────────

def test_group_labels_present(report_html):
    """Every group display label from ui_labels.yaml should appear in the output."""
    import yaml
    labels_path = Path(__file__).parent.parent / "config" / "ui_labels.yaml"
    ui = yaml.safe_load(labels_path.read_text(encoding="utf-8"))
    for _key, meta in ui.get("groups", {}).items():
        label = meta.get("display") or meta.get("history_label", "")
        if label:
            assert label in report_html, f"Expected group label '{label}' in report HTML"


def test_run_history_table_present(report_html):
    assert "Run history" in report_html or "run history" in report_html.lower()


def test_chart_datasets_js_valid(report_html):
    """Chart.js datasets block must appear and contain at least one 'label:' entry."""
    assert "datasets:" in report_html or "datasets =" in report_html or "label:" in report_html


# ── Current state / fault age ─────────────────────────────────────────────────

def _hist(*pairs):
    """pairs: (days_ago, status) — oldest first, as fetch_sensor_stability returns."""
    now = datetime.now(timezone.utc)
    return [{"run_at": (now - timedelta(days=d)).isoformat(), "status": st} for d, st in pairs]


def test_current_state_working_when_last_run_good():
    import report
    label, _c, _tip, down = report._current_state(_hist((3, "failing"), (0, "working")))
    assert label == "Working"
    assert down == 0


def test_current_state_reports_outage_length_from_start_of_streak():
    """The outage began at the first bad run after the last good one — not at
    the most recent check. That start date is what a contractor is held to."""
    import report
    label, _c, tip, down = report._current_state(
        _hist((30, "working"), (10, "malfunctioning"), (5, "malfunctioning"), (0, "malfunctioning"))
    )
    assert down == 10
    assert label == "Down 10d"
    assert "Failing since" in tip


def test_current_state_ignores_older_outages_once_repaired():
    """A long outage that was fixed must not inflate the current fault age."""
    import report
    _label, _c, _tip, down = report._current_state(
        _hist((60, "failing"), (50, "failing"), (2, "working"), (1, "failing"), (0, "failing"))
    )
    assert down == 1


def test_current_state_never_worked_is_distinct_from_down():
    """'Never worked' is a stronger, separately-labelled claim than 'Down Nd'."""
    import report
    label, _c, tip, down = report._current_state(
        _hist((28, "no_measurement"), (14, "no_measurement"), (0, "no_measurement"))
    )
    assert label == "Never worked (28d)"
    assert down == 28
    assert "No good reading since first seen" in tip


def test_current_state_no_history():
    import report
    label, _c, _tip, down = report._current_state([])
    assert label == "No data"
    assert down is None


# ── Map history playback ordering ─────────────────────────────────────────────

def _sensor(sensor_id, runs):
    return {
        "group_name": "VMS",
        "sensor_id": sensor_id,
        "history": [{"run_at": rat, "status": st} for rat, st in runs],
    }


def test_history_playback_sorted_chronologically_across_month_boundary():
    """Regression: the slider used to sort runs by their formatted dd/mm/yy
    display string, so "01/07" sorted before "30/06" and the playback jumped
    backwards across a month boundary. It must sort by the ISO timestamp."""
    import json, report
    importlib.reload(report)

    all_sensors = [_sensor("1", [
        ("2026-06-29T12:00:00+00:00", "working"),
        ("2026-07-01T12:00:00+00:00", "not_working"),
        ("2026-06-30T12:00:00+00:00", "working"),
    ])]

    runs = json.loads(report._build_history_playback(all_sensors))
    assert [r["run_at"][:8] for r in runs] == ["29/06/26", "30/06/26", "01/07/26"]


def test_history_playback_keeps_the_newest_30_runs():
    """The slider shows the last 30 runs — a text sort could keep the wrong 30."""
    import json, report
    importlib.reload(report)

    # 35 runs spanning a month boundary, supplied in shuffled order.
    days = [f"2026-06-{d:02d}T12:00:00+00:00" for d in range(20, 31)] + \
           [f"2026-07-{d:02d}T12:00:00+00:00" for d in range(1, 25)]
    all_sensors = [_sensor("1", [(rat, "working") for rat in reversed(days)])]

    runs = json.loads(report._build_history_playback(all_sensors))
    assert len(runs) == 30
    # Newest run kept, oldest dropped; strictly increasing overall.
    assert runs[-1]["run_at"][:8] == "24/07/26"
    assert runs[0]["run_at"][:8] == "25/06/26"


# ── Exact ("quoted") search tokens ────────────────────────────────────────────

def test_search_tokens_split_site_code_and_name():
    import report
    assert report._search_tokens("1004 (Severi (TCC))") == ["1004", "severi", "tcc"]


def test_search_tokens_include_both_bluetooth_path_endpoints():
    import report
    assert report._search_tokens("1008->1016") == ["1008", "1016"]


def test_exact_search_for_10_does_not_match_1001_or_1040():
    """The reported bug: substring search for '10' dragged in 1001, 1040, 1008..."""
    import report
    for display in ("1001 (Elaionon (ACC))", "1040 (99)", "1008->1016"):
        assert "10" not in report._search_tokens(display)


def test_search_tokens_ignore_raw_sensor_id_when_not_displayed():
    """BT path with sensor_id 100 renders as 'Strovolou-30881' — "100" must not hit it."""
    import report
    assert report._search_tokens("Strovolou-30881") == ["30881", "strovolou"]


def test_search_tokens_keep_sensor_id_when_it_is_displayed():
    """TD sensor 100 renders as '1040 (100)', so both 100 and 1040 are addressable."""
    import report
    assert report._search_tokens("1040 (100)") == ["100", "1040"]


def test_search_tokens_deduplicate_repeated_values():
    import report
    assert report._search_tokens("1040 (1040)") == ["1040"]
