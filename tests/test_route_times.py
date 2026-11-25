import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))
import route_times as rt  # noqa: E402

# A - B - C in a line, a direct A - C path, and a look-alike node for B (id 9) a few metres from node 2.
NODES = [{"id": 1, "name": "A", "lat": 35.00, "lon": 33.0}, {"id": 2, "name": "B", "lat": 35.01, "lon": 33.0},
         {"id": 3, "name": "C", "lat": 35.02, "lon": 33.0}, {"id": 9, "name": "b", "lat": 35.01003, "lon": 33.0},
         {"id": 4, "name": "Far", "lat": 36.0, "lon": 34.0}]
PATHS = [{"id": 10, "name": "A->B", "start": 1, "end": 2, "length_km": 1.1},
         {"id": 11, "name": "B->C", "start": 9, "end": 3, "length_km": 1.1},      # starts at the look-alike node
         {"id": 12, "name": "A->C", "start": 1, "end": 3, "length_km": 2.5},
         {"id": 13, "name": "C->A", "start": 3, "end": 1},
         {"id": 14, "name": "not drawn", "start": 1, "end": 2}]
FEED = {str(p["id"]): {"name": p["name"], "coords": [[35.0, 33.0], [35.01, 33.0]]} for p in PATHS if p["id"] != 14}
SNAP = {"nodes": NODES, "paths": PATHS}


def net():
    return rt.build_network(SNAP, FEED)


def times(**kw):
    return {k.lstrip("p"): {"seconds": v, "kmh": 50.0} for k, v in kw.items()}


def test_look_alike_nodes_are_one_site_and_undrawn_paths_are_left_out():
    n = net()
    assert set(n["paths"]) == {"10", "11", "12", "13"}
    assert n["paths"]["10"]["b"] == n["paths"]["11"]["a"] == 2             # node 9 folded into node 2 (same name, 33 m apart)
    assert 4 not in n["sites"] and set(n["sites"]) == {1, 2, 3}
    assert n["paths"]["13"]["length_m"] > 1000                             # no length_km: measured from the drawing


def test_live_times_skip_malfunctioning_and_missing_readings():
    live = {"common_measurement_timestamp": "2026-11-25T10:00:00",
            "paths": [{"id": 10, "speed_kmh": 40, "travel_time_s": 100}, {"id": 11, "speed_kmh": -1, "travel_time_s": 50},
                      {"id": 12, "speed_kmh": 30, "travel_time_s": None}, {"id": 13, "speed_kmh": 0, "travel_time_s": 0}]}
    t, silent, at = rt.live_times(live)
    assert set(t) == {"10"} and silent == {"11", "12", "13"} and at == "2026-11-25T10:00:00"


def test_chain_adds_times_and_says_where_paths_do_not_join_or_have_no_reading():
    n = net()
    ok = rt.chain_total(n, ["10", "11"], times(p10=100, p11=200))
    assert ok["seconds"] == 300 and ok["complete"] and ok["problems"] == [] and ok["length_m"] == 2200 and ok["kmh"] == 26.4
    bad = rt.chain_total(n, ["10", "13"], times(p10=100, p13=60))
    assert not bad["complete"] and "do not join" in bad["problems"][0] and bad["seconds"] == 160
    gap = rt.chain_total(n, ["10", "11"], times(p10=100))
    assert gap["seconds"] == 100 and not gap["complete"] and "no live reading" in gap["problems"][0] and gap["kmh"] is None
    assert "not in the network" in rt.chain_total(n, ["99"], {})["problems"][0]


def test_fastest_picks_the_quickest_chain_and_offers_the_other_one():
    n = net()
    out = rt.fastest(n, times(p10=100, p11=200, p12=250), 1, 3)
    assert [r["seconds"] for r in out["routes"]] == [250, 300]
    assert out["routes"][0]["legs"][0]["id"] == "12" and [l["id"] for l in out["routes"][1]["legs"]] == ["10", "11"]
    out = rt.fastest(n, times(p10=100, p11=100, p12=250), 1, 3)
    assert [l["id"] for l in out["routes"][0]["legs"]] == ["10", "11"]


def test_fastest_never_uses_a_path_without_a_reading_and_says_so():
    n = net()
    out = rt.fastest(n, times(p10=100, p12=250), 1, 3)
    assert [l["id"] for l in out["routes"][0]["legs"]] == ["12"] and len(out["routes"]) == 1
    out = rt.fastest(n, times(p10=100), 1, 3)
    assert out["routes"] == [] and "path 12 has no live reading" in out["note"]
    assert "No chain" in rt.fastest(n, times(p10=100), 3, 2)["note"] or rt.fastest(n, times(p10=100), 3, 2)["routes"] == []
    assert rt.fastest(n, {}, 1, 1)["routes"] == [] and "same place" in rt.fastest(n, {}, 1, 1)["note"]


def test_next_paths_lists_what_starts_at_a_site():
    n = net()
    assert [l["id"] for l in rt.next_paths(n, 1, times(p10=100))] == ["10", "12"]
    assert rt.next_paths(n, 1, times(p10=100))[1]["seconds"] is None
