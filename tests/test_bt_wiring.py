"""Wiring check: are paths wired to the sensors they are drawn between? Plus the OMNIA snapshot reader."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "runner"))

import bt_wiring as bw  # noqa: E402
import omnia_fetch as of  # noqa: E402


def S(name, lat, code, lon=33.0):
    return {"name": name, "lat": lat, "lon": lon, "site_code": code}


# four sensors ~1.1 km apart on a line (0.01 deg latitude)
SENSORS = {"1": S("Alpha", 35.00, "1001"), "2": S("Beta", 35.01, "1002"), "3": S("Gamma", 35.02, "1003"),
           "4": S("Delta", 35.03, "1004")}


def node(i, name, lat, lon=33.0):
    return {"id": i, "name": name, "lat": lat, "lon": lon}


NODES = [node(1, "Alpha", 35.00), node(2, "Beta", 35.01), node(3, "Gamma", 35.02), node(4, "Delta", 35.03)]


def opath(pid, name, a, b, lo=10, hi=50):
    return {"id": pid, "name": name, "start": a, "end": b, "speed_min": lo, "speed_max": hi}


def drawn(a, b):
    return {"name": "x", "coords": [[SENSORS[a]["lat"], 33.0], [SENSORS[b]["lat"], 33.0]]}


def run(paths, feed, nodes=NODES, sensors=SENSORS):
    snap = {"fetched_at": "t", "nodes": nodes, "paths": paths}
    return bw.analyse(snap, feed, sensors)


def codes(result, pid):
    return [f["code"] for f in result["paths"][pid]["findings"]]


# ── nodes <-> sensors ────────────────────────────────────────────────────────

def test_name_must_match_exactly_including_case():
    m = bw.match_nodes([node(1, "alpha", 35.00)], SENSORS)[1]
    assert m["status"] == "case_or_space" and m["sensor"] is None and m["similar"] == ["Alpha"]
    assert bw.match_nodes([node(1, "Alpha ", 35.00)], SENSORS)[1]["status"] == "case_or_space"
    assert bw.match_nodes([node(1, "Alpha", 35.00)], SENSORS)[1]["status"] == "ok"


def test_same_name_sensors_are_told_apart_by_location():
    sensors = {"a": S("Twin", 35.00, "1"), "b": S("Twin", 35.10, "2")}
    m = bw.match_nodes([node(1, "Twin", 35.10)], sensors)[1]
    assert m["sensor"] == "b" and m["status"] == "ok" and "share this name" in m["notes"][0]


def test_moved_renamed_and_unknown_nodes():
    m = bw.match_nodes([node(1, "Alpha", 35.001), node(2, "Renamed", 35.01), node(3, "Nowhere", 36.0)], SENSORS)
    assert m[1]["status"] == "name_only" and m[1]["distance_m"] > bw.SAME_PLACE_M
    assert m[2]["status"] == "location_only" and m[2]["sensor_name"] == "Beta"
    assert m[3]["status"] == "no_sensor"


def test_sensors_without_a_node_are_listed():
    m = bw.match_nodes(NODES[:3], SENSORS)
    assert bw.sensors_without_node(m, SENSORS) == [("4", "Delta")]


# ── paths ────────────────────────────────────────────────────────────────────

def test_a_correctly_wired_path_has_no_findings():
    r = run([opath(10, "1001->1002", 1, 2)], {"10": drawn("1", "2")})
    assert codes(r, "10") == [] and r["paths"]["10"]["severity"] == "ok"


def test_missing_nodes_are_reported_once():
    r = run([opath(10, "x", None, None, None, None), opath(11, "y", 1, None, None, None)],
            {"10": drawn("1", "2"), "11": drawn("1", "2")})
    assert codes(r, "10") == ["no_node"] and "Start and end" in r["paths"]["10"]["findings"][0]["text"]
    assert codes(r, "11") == ["no_node"] and "End node" in r["paths"]["11"]["findings"][0]["text"]


def test_wrong_node_names_the_sensor_it_is_really_drawn_to():
    # drawn A->C but the end node chosen is B
    r = run([opath(10, "1001->1003", 1, 2)], {"10": drawn("1", "3")})
    f = r["paths"]["10"]["findings"]
    assert [x["code"] for x in f] == ["wrong_node", "name_mismatch"]       # the name says 1003, the node is 1002
    assert "Gamma" in f[0]["text"] and "Beta" in f[0]["text"]


def test_swapped_start_and_end_nodes():
    r = run([opath(10, "1001->1002", 2, 1)], {"10": drawn("1", "2")})
    assert "swapped" in codes(r, "10") and "wrong_node" not in codes(r, "10")


def test_same_node_twice_and_unknown_node_id():
    r = run([opath(10, "a", 1, 1), opath(11, "b", 1, 99)], {"10": drawn("1", "2"), "11": drawn("1", "2")})
    assert "same_node" in codes(r, "10")
    assert "no_node" in codes(r, "11") and "99" in r["paths"]["11"]["findings"][0]["text"]


def test_node_without_a_sensor_and_far_end():
    nodes = NODES + [node(9, "5001", 35.5)]
    r = run([opath(10, "x", 1, 9)], {"10": drawn("1", "2")}, nodes=nodes)
    assert "node_without_sensor" in codes(r, "10")
    far = run([opath(10, "x", 1, 2)], {"10": {"name": "x", "coords": [[35.00, 33.0], [35.0165, 33.0]]}})
    assert "end_far" in codes(far, "10")          # ~730 m from node 2, no other sensor near the drawn end
    off = run([opath(10, "x", 1, 2)], {"10": {"name": "x", "coords": [[35.00, 33.0], [35.0115, 33.0]]}})
    assert "end_off" in codes(off, "10")          # ~170 m: only "check"


def test_name_codes_and_speed_range_and_one_sided_paths():
    r = run([opath(10, "1003->1004", 1, 2, 30, 30), opath(11, "x", 1, 2)], {"10": drawn("1", "2")})
    assert set(codes(r, "10")) == {"name_mismatch", "speed_range"}
    assert codes(r, "11") == ["not_in_feed"]
    extra = run([], {"77": drawn("1", "2")})
    assert codes(extra, "77") == ["not_in_omnia"]


def test_a_path_ending_at_a_sensor_with_a_close_neighbour_is_flagged_for_a_hand_check():
    sensors = dict(SENSORS, **{"5": S("Alpha twin", 35.0003, "1005")})          # ~33 m from Alpha
    r = run([opath(10, "1001->1002", 1, 2)], {"10": drawn("1", "2")}, sensors=sensors)
    f = r["paths"]["10"]["findings"]
    assert [x["code"] for x in f] == ["close_pair"] and "Alpha twin" in f[0]["text"] and f[0]["severity"] == "low"
    assert bw.close_neighbours(SENSORS) == {}
    assert set(bw.close_neighbours(sensors)) == {"1", "5"}


def test_descriptive_path_names_are_not_compared_with_site_codes():
    r = run([opath(10, "Alpha - Beta", 1, 2)], {"10": drawn("1", "2")})
    assert codes(r, "10") == []


def test_csv_lists_worst_first_and_sensor_wiring_summary():
    r = run([opath(10, "1001->1003", 1, 2), opath(11, "1001->1002", 1, 2)],
            {"10": drawn("1", "3"), "11": drawn("1", "2")})
    rows = bw.csv_rows(r)
    assert rows[0][0] == "Path ID" and [x[0] for x in rows[1:]] == ["10"]
    assert bw.wiring_for_sensor(r, "1") == {"nodes": [{"id": 1, "name": "Alpha", "status": "ok", "distance_m": 0.0}], "paths": 2}


# ── OMNIA snapshot ───────────────────────────────────────────────────────────

class Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


def test_mercator_conversion_matches_a_known_node():
    lat, lon = of.mercator_to_wgs84(3715272.2926525241, 4182931.1230130317)
    assert abs(lat - 35.15) < 0.02 and abs(lon - 33.37) < 0.02


def test_fetch_reads_all_pages_converts_coordinates_and_retries_a_hang():
    calls = []
    nodes = [{"Idlogicalnode": 1, "Description": "Athalassas", "Latitude": 4182931.12, "Longitude": 3715272.29}]
    paths = [{"pathId": i, "pathName": str(i), "idLogicalNodeStart": 1, "idLogicalNodeEnd": None, "speedMin": 10,
              "speedMax": 50, "pathLength": 1.0} for i in range(1, 4)]

    def get(url, params, timeout):
        calls.append((url.rsplit("/", 1)[1], params["start"]))
        if url.endswith("path-list") and calls.count(("path-list", 0)) == 1:
            raise TimeoutError()                                   # first try hangs
        if url.endswith("node-list"):
            return Resp({"data": nodes, "totalCount": 1})
        return Resp({"listPaths": paths, "total": 3})
    snap = of.fetch_snapshot("http://omnia.test/P/", get=get, sleep=lambda s: None)
    assert [p["id"] for p in snap["paths"]] == [1, 2, 3] and snap["paths"][0]["end"] is None
    assert snap["nodes"][0]["name"] == "Athalassas" and abs(snap["nodes"][0]["lat"] - 35.15) < 0.02
    assert calls.count(("path-list", 0)) == 2


def test_fetch_gives_up_with_a_readable_error_without_the_address():
    def get(url, params, timeout):
        raise ConnectionError()
    try:
        of.fetch_snapshot("http://secret.internal/P", get=get, sleep=lambda s: None)
    except of.OmniaError as exc:
        assert "node-list" in str(exc) and "secret.internal" not in str(exc)
    else:
        raise AssertionError("expected OmniaError")
    try:
        of.fetch_snapshot("", get=get)
    except of.OmniaError as exc:
        assert "CONTROL_ROOM_OMNIA_URL" in str(exc)


def test_snapshot_round_trip_and_bad_files(tmp_path):
    snap = {"fetched_at": "t", "nodes": [], "paths": []}
    of.save_snapshot(snap, tmp_path / "s.json")
    assert of.load_snapshot(tmp_path / "s.json") == snap
    assert of.load_snapshot(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("[1]")
    assert of.load_snapshot(tmp_path / "bad.json") is None


# ── stand-alone nodes map ────────────────────────────────────────────────────

def test_nodes_map_page_carries_nodes_sensors_statuses_and_is_script_safe():
    import json
    import omnia_nodes_map as nm
    nodes = [node(1, "Alpha", 35.00), node(2, "Moved", 35.011), node(3, "</script>", 36.0)]
    sensors = {"1": S("Alpha", 35.00, "1001"), "2": S("Moved", 35.01, "1002")}
    snap = {"fetched_at": "t", "nodes": nodes, "paths": [opath(10, "1001->1002", 1, 2)]}
    page = nm.build_html(snap, sensors)
    data = json.loads(page.split("var D = ", 1)[1].split(";\n  var COL", 1)[0].replace("<" + chr(92) + "/", "</"))
    st = {n["id"]: n["status"] for n in data["nodes"]}
    assert st == {1: "ok", 2: "name_only", 3: "no_sensor"} and len(data["sensors"]) == 2
    assert data["counts"]["name_only"] == 1 and [s["nodes"] for s in data["sensors"]] == [["1 Alpha"], ["2 Moved"]]
    assert page.count("</script>") == page.count("<script")      # the node named "</script>" is escaped, so no extra close tag
