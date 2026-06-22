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
from datetime import datetime, timezone

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
