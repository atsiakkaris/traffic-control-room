"""Site visits planner: what is worth a visit, stops, driving order, days and the two exports."""
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


def _days():
    a = api(1, 35.0, 33.0, name='A & "B"', site_code="1001")
    b = api(2, 35.01, 33.0)
    items = sv.collect_items([group([{"type": "match", "api": a, "ref": ref("Row <1>", 35.003, 33.0), "distance_m": 333},
                                     {"type": "api_only", "api": b, "ref": None}], [a, b],
                                    notes={"1": {"note": "gate ]]> locked"}})])
    return sv.plan(items), items


def test_kml_is_well_formed():
    days, _ = _days()
    root = ET.fromstring(sv.to_kml(days))                 # well-formed despite the &, <, " and ]]> above
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    names = [p.find("k:name", ns).text for p in root.iter("{http://www.opengis.net/kml/2.2}Placemark")]
    assert names[0].startswith("D1-01") and any("spreadsheet position" in n for n in names)


def test_excel_field_sheet_has_dropdowns_links_hidden_key_and_reads_back(tmp_path):
    from openpyxl import load_workbook
    days, items = _days()
    f = tmp_path / "sheet.xlsx"
    sv.write_field_sheet(days, f)
    wb = load_workbook(f)
    assert wb.sheetnames[0] == "Read me" and wb.sheetnames[1].startswith("Day 1")
    ws = wb[wb.sheetnames[1]]
    head = {c.value: c.column_letter for c in ws[3]}
    assert ws.column_dimensions[head["Key"]].hidden and not ws.column_dimensions[head["Found?"]].hidden
    assert ws.freeze_panes == "C4" and ws.auto_filter.ref
    lists = {dv.formula1 for dv in ws.data_validations.dataValidation}
    assert '"Yes,No,Not sure"' in lists and any("API position" in x for x in lists)
    row = next(r for r in range(4, ws.max_row + 1) if ws["%s%d" % (head["ID"], r)].value == "1")
    assert ws["%s%d" % (head["Go to API position"], row)].hyperlink.target.startswith("https://www.google.com/maps")
    # fill it in like a person would, save, read it back
    ws["%s%d" % (head["Found?"], row)] = "Yes"
    ws["%s%d" % (head["Right position"], row)] = "API position"
    ws["%s%d" % (head["Remarks"], row)] = "photo IMG_1"
    wb.save(f)
    rows, warnings = sv.read_findings(f)
    assert warnings == [] and len(rows) == len(items)
    r1 = next(r for r in rows if r["id"] == "1")
    assert (r1["found"], r1["right"], r1["remarks"]) == ("yes", "api", "photo IMG_1") and r1["kinds"][0] == "far_apart"
    assert r1["api"] == [35.0, 33.0] and r1["sheet"] == [35.003, 33.0]


def test_read_findings_understands_loose_answers_and_gps_typos(tmp_path):
    f = tmp_path / "f.csv"
    f.write_text("Key,Group,ID,Name,Kinds,Found?,Right position,GPS lat,GPS lon,Label on site,Remarks\n"
                 "td|api|1,TD,1,A,far_apart,y,Spreadsheet position,,,,\n"
                 "td|api|2,TD,2,B,far_apart,NO,,,,,\n"
                 "td|api|3,TD,3,C,far_apart,Yes,Neither,\"35.123456, 33.654321\",,L-9,\n"
                 "td|api|4,TD,4,D,far_apart,maybe,,33.5,35.5,,\n"
                 "td|api|5,TD,5,E,far_apart,,,,,,\n", encoding="utf-8")
    rows, warnings = sv.read_findings(f)
    by = {r["id"]: r for r in rows}
    assert by["1"]["found"] == "yes" and by["1"]["right"] == "sheet" and by["2"]["found"] == "no"
    assert (by["3"]["lat"], by["3"]["lon"], by["3"]["label"]) == (35.123456, 33.654321, "L-9")     # pasted "lat, lon"
    assert by["4"]["found"] == "" and by["4"]["lat"] is None                                       # swapped = not in Cyprus
    assert sum("not understood" in w for w in warnings) == 1 and sum("not in Cyprus" in w for w in warnings) == 1
    entries, summary = sv.build_entries(rows, {"td|api|1": {}})
    assert set(entries) == {"td|api|1", "td|api|2", "td|api|3"}          # 4 has no usable answer, 5 is empty
    assert summary["skipped_empty"] == 2 and summary["answered"] == 3
    assert entries["td|api|1"]["done"] and entries["td|api|2"]["done"] and summary["updated"] == 1 and summary["new"] == 2


def test_wrong_files_are_refused(tmp_path):
    import pytest
    (tmp_path / "x.txt").write_text("hi")
    with pytest.raises(ValueError):
        sv.read_findings(tmp_path / "x.txt")
    (tmp_path / "nokey.csv").write_text("a,b\n1,2\n")
    with pytest.raises(ValueError):
        sv.read_findings(tmp_path / "nokey.csv")


def test_what_each_finding_means():
    act = lambda **kw: sv.finding_action(dict({"found": "yes"}, **kw))[0]
    api_pos, sheet_pos = [35.0, 33.0], [35.003, 33.0]
    assert act(kinds=["far_apart"], right="api", api=api_pos, sheet=sheet_pos) == "fix_sheet"
    assert act(kinds=["far_apart"], right="sheet") == "fix_api"
    assert act(kinds=["far_apart"], right="neither", lat=35.1, lon=33.1) == "fix_both"
    assert "no GPS fix" in sv.finding_action({"found": "yes", "right": "neither", "kinds": ["far_apart"]})[1]
    assert act(kinds=["far_apart"]) == "incomplete"
    assert act(kinds=["api_only"]) == "add_sheet" and act(kinds=["ref_only"]) == "not_in_api" and act(kinds=["retired"]) == "not_in_api"
    assert act(kinds=["faulty"]) == "check_contractor" and act(kinds=["note"]) == "ok"
    assert act(found="no", kinds=["far_apart"]) == "not_found" and act(found="unsure") == "unsure" and act(found="") == "open"
    assert sv.correct_position({"right": "api", "api": api_pos, "lat": None}) == api_pos
    assert sv.correct_position({"right": "api", "api": api_pos, "lat": 35.5, "lon": 33.5}) == [35.5, 33.5]    # a GPS fix wins


def test_corrections_list_groups_by_action():
    store = {"td|api|1": {"found": "yes", "right": "api", "kinds": ["far_apart"], "api": [35.0, 33.0], "sheet": [35.003, 33.0],
                          "group": "TD", "id": "1", "name": "A", "at": "2026-11-12T10:00:00", "done": True},
             "td|api|2": {"found": "no", "kinds": ["retired"], "group": "TD", "id": "2", "name": "B", "at": "2026-11-12T11:00:00", "done": True},
             "td|api|3": {"done": True, "source": "manual", "group": "TD", "id": "3", "name": "C"}}
    rows = sv.corrections_rows(store)
    assert [r[0] for r in rows[1:]] == ["Correct in the spreadsheet", "Not found on site"]      # the bare mark is not a finding
    assert rows[1][4:6] == [35.0, 33.0]
