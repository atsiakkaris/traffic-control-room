"""Tests for labels.py — the shared human-readable sensor name."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

from labels import sensor_display_name, with_id


# ── Traffic Detection: site code carries the meaning ──────────────────────────

def test_td_unnamed_sensor_shows_its_site_code():
    """22 of 104 TD loops have no name; a bare id tells the reader nothing."""
    assert sensor_display_name("Traffic Detection", "100", None, "1040") == "1040 (100)"


def test_td_named_sensor_shows_site_code_and_name():
    assert sensor_display_name("Traffic Detection", "21", "Gr. Dhigeni Ave. (TCC)", "1010") \
        == "1010 (Gr. Dhigeni Ave. (TCC))"


def test_td_without_site_code_falls_back_to_name_then_id():
    assert sensor_display_name("Traffic Detection", "5", "Somewhere", None) == "Somewhere"
    assert sensor_display_name("Traffic Detection", "5", None, None) == "5"


def test_feed_newlines_are_collapsed():
    """Names arrive as 'Gr. Dhigeni Ave.\n (TCC)' and broke the HTML attribute."""
    assert sensor_display_name("Traffic Detection", "21", "Gr. Dhigeni Ave.\n (TCC)", "1010") \
        == "1010 (Gr. Dhigeni Ave. (TCC))"
    # The feed's own name ends in ')', so the bracket doubles up. That is the feed's
    # data, not a formatting bug — what matters is that no newline survives.
    assert sensor_display_name("Traffic Detection", "35", "Latsia\n)\n", "6000") == "6000 (Latsia ))"
    assert "\n" not in sensor_display_name("Traffic Detection", "35", "Latsia\n)\n", "6000")


# ── VMS and Bluetooth paths ───────────────────────────────────────────────────

def test_vms_shows_name_then_id():
    assert sensor_display_name("VMS", "8", "A1 Highway (Alambra)", None) == "A1 Highway (Alambra) (8)"


def test_vms_without_a_name_is_just_the_id():
    assert sensor_display_name("VMS", "8", None, None) == "8"


def test_bluetooth_path_uses_its_name_or_the_bare_id():
    assert sensor_display_name("Bluetooth Paths", "1", "1004->1008") == "1004->1008"
    assert sensor_display_name("Bluetooth Paths", "580", None) == "580"


# ── with_id ───────────────────────────────────────────────────────────────────

def test_with_id_appends_the_id_when_absent():
    assert '(75)' in with_id("1023 (Ararhippou A3-West)", "75")


def test_with_id_does_not_duplicate_an_id_already_shown():
    assert with_id("1040 (100)", "100") == "1040 (100)"


def test_with_id_matches_on_digit_boundaries_not_substrings():
    """Sensor 23 must not be judged 'already present' by site code 1023."""
    assert '(23)' in with_id("1023 (Ararhippou A3-West)", "23")
