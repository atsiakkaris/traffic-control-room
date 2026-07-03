"""
Tests for data validation logic introduced in the data-quality/cross-validation branch.

Covers:
- fetch_sensor_ids_for_run: returns correct IDs per group
- retire guard: retire_missing_sensors is NOT called when feed status is fail
- retire guard: retire_missing_sensors IS called when feed passes with sensors
- retire guard: retire_missing_sensors is NOT called when feed passes with zero sensors
"""

import importlib
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

RUNNER = Path(__file__).parent.parent / "runner"
sys.path.insert(0, str(RUNNER))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Isolated DB for each test."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def _run(db, sensors_by_group=None):
    """Insert a minimal run and sensor results; return run_id."""
    run_id = str(uuid.uuid4())
    run_at = datetime.now(timezone.utc).isoformat()
    db.insert_run(run_id, run_at, {"total": 1, "passed": 1, "failed": 0, "errored": 0})
    for group, sensor_ids in (sensors_by_group or {}).items():
        for sid in sensor_ids:
            db.insert_sensor_result(run_id, run_at, group, sid, "working")
    return run_id, run_at


# ── fetch_sensor_ids_for_run ──────────────────────────────────────────────────

def test_fetch_sensor_ids_returns_correct_group(db):
    run_id, _ = _run(db, {"Traffic Detection": ["10", "11"], "Bluetooth": ["10", "20"]})
    assert db.fetch_sensor_ids_for_run(run_id, "Traffic Detection") == {"10", "11"}
    assert db.fetch_sensor_ids_for_run(run_id, "Bluetooth") == {"10", "20"}


def test_fetch_sensor_ids_empty_for_unknown_group(db):
    run_id, _ = _run(db, {"Traffic Detection": ["10"]})
    assert db.fetch_sensor_ids_for_run(run_id, "VMS") == set()


def test_fetch_sensor_ids_deduplicates(db):
    # Insert the same sensor twice (simulates a duplicate row scenario)
    run_id = str(uuid.uuid4())
    run_at = datetime.now(timezone.utc).isoformat()
    db.insert_run(run_id, run_at, {"total": 1, "passed": 1, "failed": 0, "errored": 0})
    db.insert_sensor_result(run_id, run_at, "Traffic Detection", "99", "working")
    db.insert_sensor_result(run_id, run_at, "Traffic Detection", "99", "working")
    assert db.fetch_sensor_ids_for_run(run_id, "Traffic Detection") == {"99"}


# ── _process_coords retire guard ─────────────────────────────────────────────
#
# These call the real run_tests._process_coords, replacing the handler entry
# with mocks so we can observe whether upsert/retire actually fire.

def _install_mock_handler(monkeypatch, coords):
    """Replace the measurement_site handler; return (upsert_mock, retire_mock)."""
    import run_tests
    upsert = Mock()
    retire = Mock()
    monkeypatch.setitem(
        run_tests._COORDS_HANDLERS, "measurement_site",
        (lambda text: coords,
         lambda c, g: upsert(g, c),
         lambda c, g: retire(g, set(c.keys())),
         "sensors"),
    )
    return run_tests, upsert, retire


def test_retire_not_called_when_feed_fails(monkeypatch):
    """When inventory status is 'fail', neither upsert nor retire may run."""
    coords = {"10": {"lat": 34.9, "lon": 33.0, "name": "A"}}
    run_tests, upsert, retire = _install_mock_handler(monkeypatch, coords)

    run_tests._process_coords("measurement_site", "<xml/>", "Traffic Detection", "fail")

    upsert.assert_not_called()
    retire.assert_not_called()


def test_retire_called_when_feed_passes_with_sensors(monkeypatch):
    """When inventory passes and returns sensors, upsert and retire both run."""
    coords = {"10": {"lat": 34.9, "lon": 33.0, "name": "A"}}
    run_tests, upsert, retire = _install_mock_handler(monkeypatch, coords)

    run_tests._process_coords("measurement_site", "<xml/>", "Traffic Detection", "pass")

    upsert.assert_called_once_with("Traffic Detection", coords)
    retire.assert_called_once_with("Traffic Detection", {"10"})


def test_retire_not_called_when_feed_passes_with_zero_sensors(monkeypatch):
    """When inventory passes but extraction returns nothing, retire must be skipped."""
    run_tests, upsert, retire = _install_mock_handler(monkeypatch, {})

    run_tests._process_coords("measurement_site", "<xml/>", "Traffic Detection", "pass")

    upsert.assert_not_called()
    retire.assert_not_called()


def test_unknown_coords_type_is_noop(monkeypatch):
    """An unregistered coords_extract value must not raise."""
    import run_tests
    run_tests._process_coords("nonexistent_type", "<xml/>", "Traffic Detection", "pass")
