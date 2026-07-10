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


# ── match_sensors: reference rows are identified by row, not by name ──────────
#
# Two distinct sites can legitimately share a name (two points on Georgiou Griva
# Digeni Avenue). Keying claimed rows on the name made the first match swallow
# the second row: it could never match, and it was dropped from the results too.

def _ref(name, lat, lon, project="P"):
    return {"name": name, "lat": lat, "lon": lon, "extra": {"project": project}}


def _api(sid, lat, lon):
    return {"id": sid, "lat": lat, "lon": lon, "name": f"api-{sid}"}


def test_duplicate_reference_names_each_match_their_own_sensor():
    import qa
    refs = [_ref("Digeni Avenue", 35.1641184, 33.3506207),
            _ref("Digeni Avenue", 35.1609086, 33.3424934)]
    apis = [_api("21", 35.16413, 33.35085), _api("109", 35.16090, 33.34250)]

    matches = qa.match_sensors(refs, apis, max_dist=300)
    matched = {m["api"]["id"] for m in matches if m["type"] == "match"}
    assert matched == {"21", "109"}, "each duplicate-named row must claim its own sensor"


def test_every_reference_row_is_accounted_for_exactly_once():
    """A row must surface as either a 'match' or a 'ref_only' — never vanish."""
    import qa
    refs = [_ref("Shared Name", 35.1641184, 33.3506207),
            _ref("Shared Name", 35.9000000, 33.9000000)]   # far from any sensor
    apis = [_api("21", 35.16413, 33.35085)]

    matches = qa.match_sensors(refs, apis, max_dist=300)
    accounted = sum(1 for m in matches if m["type"] in ("match", "ref_only"))
    assert accounted == len(refs)


def test_closest_sensor_still_wins_a_contested_row():
    """The greedy shortest-first rule is unchanged: nearest sensor claims the row."""
    import qa
    refs = [_ref("Only Row", 35.0, 33.0)]
    apis = [_api("far", 35.0018, 33.0), _api("near", 35.00005, 33.0)]

    matches = qa.match_sensors(refs, apis, max_dist=300)
    match = [m for m in matches if m["type"] == "match"]
    assert len(match) == 1 and match[0]["api"]["id"] == "near"
    assert {m["api"]["id"] for m in matches if m["type"] == "api_only"} == {"far"}


# ── qa.generate_html must not shadow the `html` module ────────────────────────
#
# generate_html() once assigned its output to a local named `html`, which made
# the module invisible to the nested _h() escaper closing over that scope. Every
# QA report raised NameError at render time. Guard the escaper directly.

def test_generate_html_does_not_shadow_the_html_module():
    import inspect, qa
    src = inspect.getsource(qa.generate_html)
    assert "\n    html = " not in src, "local named `html` shadows the module for _h()"


def test_qa_html_escaper_works_inside_generate_html_scope():
    """_h() must escape, not raise, for spreadsheet-derived text."""
    import qa
    # _h is nested; exercise the module-level escaping it relies on.
    assert qa.html.escape('<script>&"') == '&lt;script&gt;&amp;&quot;'


# ── Co-location radius is one constant, used by both code paths ───────────────
#
# match_sensors() labelled a sensor 'colocated' within a local 10m, while
# annotate_accountability() inherited the project within COLOCATION_M (15m).
# A sensor 12m from its twin therefore inherited ownership but was reported as
# "api_only" in the QA view. Both now read the same constant.

def _twin_at(metres):
    """Two API sensors `metres` apart; only the first matches a reference row."""
    import qa
    lat_offset = metres / 111_320.0          # ~metres per degree of latitude
    refs = [_ref("Site", 35.0, 33.0)]
    apis = [_api("matched", 35.0, 33.0), _api("twin", 35.0 + lat_offset, 33.0)]
    return qa.match_sensors(refs, apis, max_dist=300)


def test_twin_inside_the_colocation_radius_is_reported_colocated():
    import qa
    types = {m["api"]["id"]: m["type"] for m in _twin_at(qa.COLOCATION_M - 3)}
    assert types["twin"] == "colocated"


def test_twin_beyond_the_colocation_radius_is_reported_api_only():
    import qa
    types = {m["api"]["id"]: m["type"] for m in _twin_at(qa.COLOCATION_M + 5)}
    assert types["twin"] == "api_only"


def test_twin_at_12m_is_colocated_not_api_only():
    """The exact divergence: 12m was inside annotate's 15m but outside the old 10m."""
    types = {m["api"]["id"]: m["type"] for m in _twin_at(12)}
    assert types["twin"] == "colocated", "QA report must agree with project inheritance"


def test_match_sensors_uses_the_shared_constant_not_a_local_one():
    import inspect, qa
    src = inspect.getsource(qa.match_sensors)
    assert "COLOC_M" not in src, "local radius reintroduced; use COLOCATION_M"
    assert "COLOCATION_M" in src
