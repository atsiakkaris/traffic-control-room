"""Tests for bt_direction_check.py - geometry-only detection of non-reversed BT path pairs."""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_direction_check as chk

LAT0, LON0 = 35.0, 33.0
M_LAT = 111195.0                                   # metres per degree of latitude
M_LON = 111195.0 * math.cos(math.radians(35.0))    # metres per degree of longitude at lat 35


def line(x0_m, y0_m, x1_m, y1_m, n):
    """n points on a straight line between two metre offsets from the origin."""
    return [[LAT0 + (y0_m + (y1_m - y0_m) * k / (n - 1)) / M_LAT,
             LON0 + (x0_m + (x1_m - x0_m) * k / (n - 1)) / M_LON] for k in range(n)]


def east(length_m=4000, n=30, y=0, x0=0):
    return line(x0, y, x0 + length_m, y, n)


def rev(coords):
    return list(reversed(coords))


def run(*named):
    """named: (id, name, coords) -> list of pair dicts."""
    paths = {pid: {"name": name, "coords": c} for pid, name, c in named}
    return chk.analyse(paths)["pairs"]


def only(pairs):
    assert len(pairs) == 1, pairs
    return pairs[0]


def test_opposite_pair_is_healthy():
    p = only(run(("1", "", east()), ("2", "", rev(east()))))
    assert p["direction"] == "opposite" and p["bucket"] == chk.B_HEALTHY
    assert p["r"] <= -0.99


def test_same_direction_pair_is_flagged():
    p = only(run(("1", "", east()), ("2", "", east())))
    assert p["direction"] == "same" and p["bucket"] == chk.B_SAME_UNEXPL
    assert p["r"] >= 0.99


def test_opposite_pair_with_50m_lateral_offset():
    p = only(run(("1", "", east()), ("2", "", rev(east(y=50)))))
    assert p["direction"] == "opposite" and p["bucket"] == chk.B_HEALTHY


def test_same_direction_pair_with_50m_lateral_offset():
    p = only(run(("1", "", east()), ("2", "", east(y=50))))
    assert p["direction"] == "same"


def test_different_point_counts():
    p = only(run(("1", "", east(n=60)), ("2", "", rev(east(n=7)))))
    assert p["direction"] == "opposite"
    p = only(run(("1", "", east(n=60)), ("2", "", east(n=7))))
    assert p["direction"] == "same"


def test_partial_coverage_pair_is_not_paired():
    """Q covers only 50% of P: normal shared road, not a reverse-pair candidate."""
    assert run(("1", "", east(4000)), ("2", "", rev(east(2000)))) == []
    assert run(("1", "", east(4000)), ("2", "", east(2000))) == []


def test_unrelated_crossing_paths_do_not_pair():
    cross = line(2000, -2000, 2000, 2000, 30)
    assert run(("1", "", east(4000)), ("2", "", cross)) == []


def test_a_to_c_overlapping_a_to_b_is_not_a_bug():
    """A->C shares the first part of A->B's road: topology, never bucket 1."""
    ab = east(4000)
    ac = east(7000)
    pairs = run(("1", "A->B", ab), ("2", "A->C", ac))
    assert all(p["bucket"] != chk.B_NON_REVERSED for p in pairs)
    # ...and a long overlap with the same extent is only a review item (bucket 2)
    ac_long = east(4500)
    pairs = run(("1", "A->B", ab), ("2", "A->C", ac_long))
    assert all(p["bucket"] != chk.B_NON_REVERSED for p in pairs)
    assert all(p["bucket"] == chk.B_SAME_UNEXPL for p in pairs)


def test_unnamed_same_direction_pair_gets_unnamed_hint():
    # A missing name arrives as the path id (see geo.extract_bt_path_coords).
    p = only(run(("594", "594", east()), ("595", "595", east())))
    assert p["bucket"] == chk.B_SAME_UNEXPL and p["hint"] == "unnamed"
    p = only(run(("5", "", east()), ("6", None, east())))
    assert p["hint"] == "unnamed"


def test_mirrored_names_with_same_direction_geometry_is_bucket_1():
    p = only(run(("1", "A->B", east()), ("2", "B->A", east())))
    assert p["bucket"] == chk.B_NON_REVERSED and p["direction"] == "same"


def test_mirrored_names_with_opposite_geometry_is_healthy():
    p = only(run(("1", "A->B", east()), ("2", "B->A", rev(east()))))
    assert p["bucket"] == chk.B_HEALTHY


def test_names_are_never_used_to_pair():
    """Mirrored names on unrelated geometry produce no pair at all."""
    far = east(4000, y=20000)
    assert run(("1", "A->B", east()), ("2", "B->A", far)) == []


def test_wrong_member_identified_from_sensor_positions():
    ab = east()                       # path 1 is right: starts at A, ends at B
    nodes = {"A": chk.project([ab[0]])[0], "B": chk.project([ab[-1]])[0]}
    paths = {"1": {"name": "A->B", "coords": ab}, "2": {"name": "B->A", "coords": ab}}
    p = only(chk.analyse(paths, nodes)["pairs"])
    assert p["bucket"] == chk.B_NON_REVERSED
    assert "likely wrong: 2" in p["hint"]


def test_endpoint_candidate_for_geometry_with_weak_coverage():
    """Endpoints within 400 m make a pair even if the middle wanders off."""
    wander = [[LAT0, LON0]] + [[LAT0 + 0.02, LON0 + k * 0.004] for k in range(1, 8)] + \
             [[LAT0, LON0 + 0.03]]
    straight = [[LAT0, LON0], [LAT0, LON0 + 0.03]]
    p = only(run(("1", "", wander), ("2", "", straight)))
    assert p["endpoint_sum_m"] < chk.ENDPOINT_PAIR_M


def test_resample_spacing():
    pts = chk.project(east(1000, n=3))
    out = chk.resample(pts)
    assert len(out) == 41
    assert chk._dist(out[0], out[1]) == pytest.approx(chk.RESAMPLE_M, abs=0.5)



def test_orientation_helper():
    assert chk.orientation(east(), east(y=30)) == "same"
    assert chk.orientation(east(), rev(east(y=30))) == "opposite"


def test_map_duplicate_detection_ignores_mirrored_pairs():
    """Phase 2: a forward path and its mirrored partner are not duplicates."""
    import bt_paths_map as bpm
    mirrored = {"1": {"name": "", "coords": east()}, "2": {"name": "", "coords": rev(east())}}
    assert bpm._find_geometric_duplicate_groups(mirrored, ["1", "2"]) == []
    same = {"1": {"name": "", "coords": east()}, "2": {"name": "", "coords": east()}}
    assert bpm._find_geometric_duplicate_groups(same, ["1", "2"]) == [["1", "2"]]
