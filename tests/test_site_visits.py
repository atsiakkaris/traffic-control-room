"""Site visits planner: what is worth a visit, stops, driving order, days and the two exports."""
import csv
import io
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "runner"))

import site_visits as sv  # noqa: E402


def api(sid, lat, lon, active=True, name=None, site_code=None):
    return {"id": sid, "name": name or "S%s" % sid, "lat": lat, "lon": lon, "active": active, "site_code": site_code}


def ref(name, lat, lon, commissioning="active"):
    return {"name": name, "lat": lat, "lon": lon, "commissioning": commissioning}


def group(matches, apis, slug="traffic-detection", verdicts=None, notes=None):
    return {"group": slug.title(), "slug": slug, "matches": matches, "api": apis, "verdicts": verdicts or {}, "notes": notes or {}}


def kinds_by_id(items):
    return {(it["id"] or it["ref_name"]): it["kinds"] for it in items}


def test_collects_each_kind_of_doubt_and_skips_what_is_fine():
    a_far, r_far = api(1, 35.0, 33.0), ref("Far row", 35.002, 33.0)            # ~220 m apart
    a_ok, r_ok = api(2, 35.1, 33.1), ref("Fine row", 35.1001, 33.1)            # ~11 m: fine
    a_only, r_only = api(3, 35.2, 33.2), ref("Lonely row", 35.204, 33.2)       # ~440 m: probably one device
    a_alone = api(4, 34.7, 32.4)                                               # nothing near it
    r_alone = ref("Row far away", 34.9, 33.6)
    r_power = ref("Awaiting power", 34.95, 33.65, commissioning="not_electrified")
    a_retired = api(5, 34.68, 33.04, active=False)
    matches = [
        {"type": "match", "api": a_far, "ref": r_far, "distance_m": 222.4},
        {"type": "match", "api": a_ok, "ref": r_ok, "distance_m": 11.1},
        {"type": "api_only", "api": a_only, "ref": None}, {"type": "ref_only", "api": None, "ref": r_only},
        {"type": "api_only", "api": a_alone, "ref": None}, {"type": "ref_only", "api": None, "ref": r_alone},
        {"type": "ref_only", "api": None, "ref": r_power}, {"type": "api_only", "api": a_retired, "ref": None},
    ]
    items = sv.collect_items([group(matches, [a_far, a_ok, a_only, a_alone, a_retired])])
    k = kinds_by_id(items)
    assert k == {"1": ["far_apart"], "3": ["pair"], "4": ["api_only"], "Row far away": ["ref_only"], "5": ["retired"]}
    far = next(it for it in items if it["id"] == "1")
    assert [p["label"] for p in far["points"]] == ["API", "Spreadsheet"] and far["dist"] == 222


def test_faulty_bluetooth_and_notes_join_the_same_sensor():
    a = api(9, 35.0, 33.0)
    b = api(10, 35.3, 33.3)
    g = group([{"type": "match", "api": a, "ref": ref("Row", 35.0021, 33.0), "distance_m": 233}], [a, b], slug="bluetooth",
              verdicts={"9": "faulty_high", "10": "path_problem"}, notes={"9": {"note": "cabinet open"}, "10": {"note": "check"}})
    items = sv.collect_items([g])
    k = kinds_by_id(items)
    assert k == {"9": ["far_apart", "faulty", "note"], "10": ["note"]}
    assert next(it for it in items if it["id"] == "9")["note"] == "cabinet open"


def test_close_things_are_one_stop_and_stops_get_an_area():
    items = sv.collect_items([group([{"type": "api_only", "api": api(1, 35.1700, 33.3600), "ref": None},
                                     {"type": "api_only", "api": api(2, 35.1705, 33.3601), "ref": None},   # ~55 m
                                     {"type": "api_only", "api": api(3, 34.7700, 32.4300), "ref": None}],
                                    [])])
    stops = sv.group_stops(items)
    assert sorted(len(s["items"]) for s in stops) == [1, 2]
    assert {s["area"] for s in stops} == {"Nicosia", "Paphos"}


def test_route_order_is_never_longer_than_nearest_neighbour_and_fixes_a_zigzag():
    start = (35.0, 33.0)
    # points on a line east of the start, given out of order
    stops = [{"lat": 35.0, "lon": 33.0 + d / 100} for d in (5, 1, 4, 2, 3)]
    route = sv.order_route(stops, start)
    assert [round((s["lon"] - 33.0) * 100) for s in route] == [1, 2, 3, 4, 5]
    # a case where nearest neighbour alone doubles back: 2-opt must not be worse
    import random
    rnd = random.Random(4)
    pts = [{"lat": 35 + rnd.random() / 5, "lon": 33 + rnd.random() / 5} for _ in range(25)]
    greedy, here, left = [], start, list(pts)
    while left:
        nxt = min(left, key=lambda s: sv.haversine_m(here[0], here[1], s["lat"], s["lon"]))
        left.remove(nxt); greedy.append(nxt); here = (nxt["lat"], nxt["lon"])
    assert sv.route_length(start, sv.order_route(pts, start)) <= sv.route_length(start, greedy) + 1e-6
    assert sorted(map(id, sv.order_route(pts, start))) == sorted(map(id, pts))


def test_plan_cuts_days_filters_and_numbers_stops():
    matches = [{"type": "api_only", "api": api(i, 35.17 + i / 100, 33.36), "ref": None} for i in range(7)]
    matches.append({"type": "api_only", "api": api(99, 34.77, 32.43), "ref": None})
    items = sv.collect_items([group(matches, [])])
    days = sv.plan(items, per_day=3)
    assert [(d["area"], len(d["stops"])) for d in days] == [("Nicosia", 3), ("Nicosia", 3), ("Nicosia", 1), ("Paphos", 1)]
    assert days[0]["stops"][0]["code"] == "D1-01" and days[3]["stops"][0]["code"] == "D4-01"
    assert sv.plan(items, kinds=["retired"]) == []
    assert sv.plan(items, groups=["vms"]) == []


def test_google_links_stay_within_the_waypoint_limit():
    pts = [(35 + i / 100, 33.0) for i in range(12)]
    urls = sv.route_urls(pts)
    assert len(urls) == 2
    assert urls[0].count("%7C") == 7                     # 10 points: origin + 8 waypoints + destination
    assert "origin=35.090000,33.000000" in urls[1]        # the second link starts where the first ends
    assert sv.route_urls([(35.0, 33.0)]) == [sv.nav_url(35.0, 33.0)]
    assert sv.route_urls([]) == []


def test_field_sheet_and_kml():
    a = api(1, 35.0, 33.0, name='A & "B"')
    items = sv.collect_items([group([{"type": "match", "api": a, "ref": ref("Row <1>", 35.003, 33.0), "distance_m": 333}], [a],
                                    notes={"1": {"note": "gate ]]> locked"}})])
    days = sv.plan(items)
    rows = sv.field_sheet_rows(days)
    assert rows[0][-len(sv.FIELD_COLUMNS):] == sv.FIELD_COLUMNS
    assert rows[1][1] == "D1-01" and "333 m apart" in rows[1][9] and "gate" in rows[1][9]
    csv.writer(io.StringIO()).writerows(rows)
    root = ET.fromstring(sv.to_kml(days))                 # well-formed despite the &, <, " and ]]> above
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    names = [p.find("k:name", ns).text for p in root.iter("{http://www.opengis.net/kml/2.2}Placemark")]
    assert names[0].startswith("D1-01") and any("spreadsheet position" in n for n in names)
