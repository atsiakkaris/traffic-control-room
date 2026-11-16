"""
site_visits.py - plan on-site checks for sensors whose position or existence is in doubt.

Pure functions (no DB, no network). The Control Room feeds in what the sensor-location pages already compute
(API sensors, spreadsheet rows, their matches, the Bluetooth verdicts and the sensor notes) and gets back:

  1. the things to check (collect_items): positions that disagree, a sensor on one side only, retired sensors,
     faulty Bluetooth sensors, sensors you wrote a note on;
  2. stops: things closer than MERGE_M are one visit (group_stops);
  3. days: stops grouped by area (nearest of AREAS), put in driving order (nearest neighbour, then 2-opt on the
     straight-line distance) and cut into days of at most `per_day` stops (plan);
  4. exports: a field sheet (Excel, one sheet per day, yellow columns to fill in on site) and a KML file for Google My Maps;
  5. findings: the filled-in sheet is read back (read_findings), turned into entries that say which items were visited and
     what was found (build_entries), and each finding is translated into what you have to do (finding_action).

Straight-line distance is good enough to put stops in order; nothing is sent to a routing service.
"""
import csv
import math
import re
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

WARN_M = 100         # same as qa.COORD_WARN_M: spreadsheet and API further apart than this = check on site
MERGE_M = 150        # things closer than this are one stop
PAIR_M = 1000        # a sensor only in the API and a row only in the spreadsheet this close: probably one device
PER_DAY = 15

# Where a day starts. A stop belongs to the nearest one.
AREAS = {
    "Nicosia":   (35.1725, 33.3650),
    "Limassol":  (34.6841, 33.0379),
    "Larnaca":   (34.9182, 33.6201),
    "Paphos":    (34.7720, 32.4297),
    "Famagusta": (35.0300, 33.9600),
}

KINDS = {
    "far_apart": "Positions differ",
    "pair":      "Probably the same sensor",
    "api_only":  "Not in the spreadsheet",
    "ref_only":  "No API sensor",
    "retired":   "Retired from the API",
    "faulty":    "Bluetooth sensor faulty",
    "note":      "Your note",
}
KIND_ORDER = list(KINDS)

COLOCATION_M = 15    # same as qa.COLOCATION_M: a second sensor this close to a matched one shares its row


def explanation(warn_m=WARN_M, pair_m=PAIR_M, merge_m=MERGE_M, per_day=PER_DAY):
    """The reasoning in plain words, so colleagues can see why a sensor is recommended. Shown on the Site visits tab, in the
    field sheet and on the phone page. The thresholds come from the same constants the code uses, so it cannot drift."""
    reasons = {
        "far_apart": ("It is matched to a spreadsheet row, but the API position and the spreadsheet position are %d m or more apart." % warn_m,
                      "One of the two positions is wrong. Maps, fault location and the sensor-to-contract matching all depend on the coordinates."),
        "pair": ("An API sensor has no spreadsheet row, an active spreadsheet row has no API sensor, and the two are within %d m of each other "
                 "(closest pairs first)." % pair_m,
                 "Most likely one device whose position is wrong on one side: a single visit settles both."),
        "api_only": ("The API lists it (it is still in the feed), but the spreadsheet has no row for it nearby: none within the matching radius "
                     "and no unmatched row within %d m." % pair_m,
                     "Either a sensor was installed without being recorded (owner and contract unknown), or the API lists a sensor that is not there."),
        "ref_only": ("The spreadsheet lists it as active, but no API sensor is near it: none within the matching radius and no unmatched "
                     "sensor within %d m." % pair_m,
                     "Either it is not installed, not connected to the system, or it reports under a wrong position. The contract may count "
                     "equipment the system cannot see."),
        "retired": ("Its ID is missing from the latest monitor run: it dropped out of the API feed.",
                    "The equipment may have been removed, damaged or lost power, or it was only deleted from the system."),
        "faulty": ("Bluetooth triangulation: no path through it works, and its failing paths lead to sensors that are alive elsewhere "
                   "(verdicts 'Faulty (high confidence)' and 'Faulty (likely)').",
                   "A path only reports when both its sensors work, so this sensor is the most likely cause. Only a visit can confirm it."),
        "note": ("You wrote a note on this sensor.", "You flagged it yourself."),
    }
    steps = [
        "Compare: every sensor group (Traffic Detection, Bluetooth, VMS) is matched with the QA Locations spreadsheet by position, the same "
        "matching as the sensor tabs.",
        "Flag: a sensor is recommended when at least one of the reasons below applies. Sensors with nothing in doubt are not listed.",
        "Filter: you choose which reasons and groups to include. Anything already visited (answered Yes or No) is left out unless you show it.",
        "Stops: things within %d m of each other are one stop, so one visit covers them." % merge_m,
        "Areas: each stop goes to the nearest of Nicosia, Limassol, Larnaca, Paphos and Famagusta.",
        "Order: from where the day starts (your office, or the area centre) always the nearest unvisited stop next, then any two crossing "
        "legs are untangled. Straight-line distances, so the real drive can be longer.",
        "Days: at most 'Stops per day' stops (%d by default); what is left is ordered again from the start for the next day." % per_day,
    ]
    not_flagged = [
        "Sensors with nothing in doubt: matched to a spreadsheet row less than %d m away and no other reason." % warn_m,
        "Spreadsheet rows marked awaiting power or decommissioned: they are not expected to work.",
        "Spreadsheet rows without coordinates: they cannot be placed on a map or a route.",
        "A second sensor within %d m of a matched one (usually the other direction): it shares its twin's row." % COLOCATION_M,
        "Bluetooth sensors that work but have failing paths (look at the paths), or with not enough evidence (nothing to check on site).",
        "How long a sensor has been down is not a reason by itself. The visit list is about position and existence doubts; the popup "
        "status line says when a sensor is down.",
    ]
    return {"reasons": [{"kind": k, "label": KINDS[k], "rule": reasons[k][0], "matters": reasons[k][1]} for k in KIND_ORDER],
            "steps": steps, "not_flagged": not_flagged}


FOUND_CHOICES = ["Yes", "No", "Not sure"]
RIGHT_CHOICES = ["API position", "Spreadsheet position", "Neither", "Not applicable"]


def haversine_m(lat1, lon1, lat2, lon2):
    r, rad = 6371000, math.pi / 180
    p1, p2 = lat1 * rad, lat2 * rad
    a = math.sin((lat2 - lat1) * rad / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin((lon2 - lon1) * rad / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nav_url(lat, lon):
    """Google Maps directions to one point (opens the app on a phone)."""
    return "https://www.google.com/maps/dir/?api=1&destination=%.6f,%.6f" % (lat, lon)


def route_urls(points, leg=10):
    """Google Maps links that drive through `points` in order, at most `leg` points per link (its waypoint limit)."""
    out, i = [], 0
    while i < len(points) - 1 or (i == 0 and len(points) == 1):
        chunk = points[i:i + leg]
        if len(chunk) == 1:
            out.append(nav_url(*chunk[0]))
            break
        fmt = lambda p: "%.6f,%.6f" % p
        url = ("https://www.google.com/maps/dir/?api=1&origin=" + fmt(chunk[0]) + "&destination=" + fmt(chunk[-1]))
        if len(chunk) > 2:
            url += "&waypoints=" + quote("|".join(fmt(p) for p in chunk[1:-1]), safe=",")
        out.append(url + "&travelmode=driving")
        i += leg - 1                       # the next link starts where this one ends
    return out


# ── 1. what to check ─────────────────────────────────────────────────────────

def _point(label, lat, lon):
    return {"label": label, "lat": lat, "lon": lon}


def _has_coords(s):
    return s is not None and s.get("lat") is not None and s.get("lon") is not None


def collect_items(groups, warn_m=WARN_M, pair_m=PAIR_M):
    """Everything worth a visit, one item per sensor or spreadsheet row.

    groups: [{"group", "slug", "matches" (qa.match_sensors), "api" (all API sensors), "verdicts" ({id: verdict},
    Bluetooth only), "verdict_info" ({id: (label, reason)}), "notes" ({id: {"note"}}), "radius" (match radius m),
    "statuses" ({id: (sentence, tone)} from sensor_status)}]. An item: {key, group, slug, kinds [..], id, name, site_code,
    project, ref_name, dist, points [{label, lat, lon}] (first = where to go), why [evidence, one per reason: what the data
    says], checks [what to check on site], status ({text, tone} or None), note}.
    """
    items = {}

    def item(key, g, kind, api=None, ref=None, dist=None):
        it = items.get(key)
        if it is None:
            it = items[key] = {"key": "|".join(str(k) for k in key), "group": g["group"], "slug": g["slug"], "kinds": [], "checks": [],
                               "why": [], "status": None,
                               "id": str(api["id"]) if api else None, "name": (api or ref or {}).get("name"),
                               "site_code": api.get("site_code") if api else None,
                               "project": (api or {}).get("project") or (ref or {}).get("project"),
                               "ref_name": ref.get("name") if ref else None, "dist": dist, "points": [], "note": None}
            st = (g.get("statuses") or {}).get(str(api["id"])) if api else None
            if st and api.get("active", True):          # a retired sensor's reason already says it is retired
                it["status"] = {"text": st[0], "tone": st[1]}
            if _has_coords(api):
                it["points"].append(_point("API", api["lat"], api["lon"]))
            if _has_coords(ref):
                it["points"].append(_point("Spreadsheet", ref["lat"], ref["lon"]))
        if kind not in it["kinds"]:
            it["kinds"].append(kind)
        return it

    fmt = lambda p: "%.5f, %.5f" % (p["lat"], p["lon"])
    for g in groups:
        api_only, ref_only = [], []
        rad = ("%d m" % g["radius"]) if g.get("radius") else "the matching radius"
        for m in g.get("matches", []):
            api, ref = m.get("api"), m.get("ref")
            if m["type"] == "match" and (m.get("distance_m") or 0) >= warn_m and _has_coords(api) and _has_coords(ref):
                it = item((g["slug"], "api", str(api["id"])), g, "far_apart", api, ref, round(m["distance_m"]))
                it["checks"].append("Spreadsheet and API are %d m apart: which position is right?" % round(m["distance_m"]))
                it["why"].append("The API puts it at %s and the spreadsheet row \"%s\" at %s: %d m apart (we flag %d m or more), "
                                 "so one of the two positions is wrong." % (fmt(api), ref.get("name"), fmt(ref), round(m["distance_m"]), warn_m))
            elif m["type"] == "api_only" and _has_coords(api) and api.get("active", True):
                api_only.append(api)
            elif m["type"] == "ref_only" and _has_coords(ref) and ref.get("commissioning", "active") == "active":
                ref_only.append(ref)

        # A sensor only in the API and a row only in the spreadsheet close to each other are probably one device
        # whose position is wrong on one side: one visit settles both.
        pairs = sorted(((haversine_m(a["lat"], a["lon"], r["lat"], r["lon"]), ai, ri)
                        for ai, a in enumerate(api_only) for ri, r in enumerate(ref_only)), key=lambda t: t[0])
        used_a, used_r = set(), set()
        for d, ai, ri in pairs:
            if d > pair_m:
                break
            if ai in used_a or ri in used_r:
                continue
            used_a.add(ai); used_r.add(ri)
            a, r = api_only[ai], ref_only[ri]
            it = item((g["slug"], "api", str(a["id"])), g, "pair", a, r, round(d))
            it["checks"].append("API sensor without a spreadsheet row, and a spreadsheet row \"%s\" without an API sensor "
                                "%d m apart: probably the same device. Which position is right?" % (r.get("name"), round(d)))
            it["why"].append("The API sensor (at %s) has no spreadsheet row, and the spreadsheet row \"%s\" (at %s) has no API sensor. They are %d m "
                             "apart (we pair them up to %d m), so it is probably one device whose position is wrong on one side."
                             % (fmt(a), r.get("name"), fmt(r), round(d), pair_m))
        for ai, a in enumerate(api_only):
            if ai not in used_a:
                it = item((g["slug"], "api", str(a["id"])), g, "api_only", a)
                it["checks"].append("In the API but not in the spreadsheet: is a sensor installed here? Note its label and project.")
                it["why"].append("It is in the API feed, but the spreadsheet has no row for it: none within %s and no unmatched row within %d m." % (rad, pair_m))
        for ri, r in enumerate(ref_only):
            if ri not in used_r:
                key = (g["slug"], "ref", "%.5f,%.5f" % (r["lat"], r["lon"]), r.get("name"))
                it = item(key, g, "ref_only", None, r)
                it["checks"].append("In the spreadsheet (active) but no API sensor: is it installed, powered and connected?")
                it["why"].append("The spreadsheet lists it as active, but no API sensor is near it: none within %s and no unmatched sensor within %d m." % (rad, pair_m))

        by_id = {str(s["id"]): s for s in g.get("api", [])}
        for sid, s in by_id.items():
            if not s.get("active", True) and _has_coords(s):
                it = item((g["slug"], "api", sid), g, "retired", s)
                it["checks"].append("Missing from the latest API data: is it still installed? Removed, damaged or without power?")
                seen = str(s.get("last_seen") or "")[:10]
                it["why"].append("Its ID is missing from the latest monitor run%s: it dropped out of the API feed." % (" (last seen %s)" % seen if seen else ""))
        for sid, v in (g.get("verdicts") or {}).items():
            s = by_id.get(str(sid))
            if v in ("faulty_high", "faulty_likely") and _has_coords(s) and s.get("active", True):
                it = item((g["slug"], "api", str(sid)), g, "faulty", s)
                it["checks"].append("No Bluetooth path through it works: check power, antenna and communication.")
                label, reason = (g.get("verdict_info") or {}).get(str(sid), (v, ""))
                it["why"].append("Triangulation verdict: %s.%s" % (label, (" " + reason[0].upper() + reason[1:] + ".") if reason else ""))
        for sid, n in (g.get("notes") or {}).items():
            s = by_id.get(str(sid))
            if _has_coords(s):
                it = item((g["slug"], "api", str(sid)), g, "note", s)
                it["note"] = n.get("note") if isinstance(n, dict) else str(n)
                it["why"].append("You wrote a note on this sensor.")
    out = [it for it in items.values() if it["points"]]
    for it in out:
        it["kinds"].sort(key=KIND_ORDER.index)
    return out


# ── 2. stops ─────────────────────────────────────────────────────────────────

def group_stops(items, merge_m=MERGE_M):
    """Items whose first point lies within merge_m of a stop's first item join that stop (one visit)."""
    stops = []
    for it in sorted(items, key=lambda i: (i["points"][0]["lat"], i["points"][0]["lon"])):
        p = it["points"][0]
        for st in stops:
            if haversine_m(st["lat"], st["lon"], p["lat"], p["lon"]) <= merge_m:
                st["items"].append(it)
                break
        else:
            stops.append({"lat": p["lat"], "lon": p["lon"], "items": [it]})
    for st in stops:
        st["area"] = nearest_area(st["lat"], st["lon"])
    return stops


def nearest_area(lat, lon, areas=AREAS):
    return min(areas, key=lambda a: haversine_m(lat, lon, *areas[a]))


# ── 3. order and days ────────────────────────────────────────────────────────

def route_length(start, stops):
    pts = [start] + [(s["lat"], s["lon"]) for s in stops]
    return sum(haversine_m(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))


def order_route(stops, start):
    """Driving order from `start` (lat, lon): nearest neighbour, then 2-opt until no swap shortens it. Open route."""
    left, route, here = list(stops), [], start
    while left:
        nxt = min(left, key=lambda s: haversine_m(here[0], here[1], s["lat"], s["lon"]))
        left.remove(nxt)
        route.append(nxt)
        here = (nxt["lat"], nxt["lon"])
    pts = [start] + [(s["lat"], s["lon"]) for s in route]
    d = lambda i, j: haversine_m(*pts[i], *pts[j])
    better = True
    while better:
        better = False
        for i in range(0, len(pts) - 2):
            for j in range(i + 2, len(pts)):
                # reverse pts[i+1..j]: edges (i,i+1) and (j,j+1) become (i,j) and (i+1,j+1); the end is open
                old = d(i, i + 1) + (d(j, j + 1) if j + 1 < len(pts) else 0)
                new = d(i, j) + (d(i + 1, j + 1) if j + 1 < len(pts) else 0)
                if new < old - 1e-6:
                    pts[i + 1:j + 1] = pts[i + 1:j + 1][::-1]
                    route[i:j] = route[i:j][::-1]
                    better = True
    return route


def plan(items, per_day=PER_DAY, kinds=None, groups=None, merge_m=MERGE_M, areas=AREAS, start=None):
    """[{area, day, stops, km, links, start}] - one entry per day, areas in AREAS order, stops numbered in driving order.

    start = (lat, lon, name): every day begins there (the office), so each day's route is ordered from it and the leg
    from it is counted. Without it a day begins at the centre of its area. Stops are still grouped by area; what is left
    of an area after a day is ordered again from the start for the next day."""
    per_day = max(1, int(per_day))
    chosen = [it for it in items
              if (kinds is None or set(it["kinds"]) & set(kinds)) and (groups is None or it["slug"] in groups)]
    stops = group_stops(chosen, merge_m)
    days, n = [], 0
    for area in areas:
        left = [s for s in stops if s["area"] == area]
        origin = (start[0], start[1]) if start else areas[area]
        while left:
            route = order_route(left, origin)
            part, left = route[:per_day], route[per_day:]
            n += 1
            prev = origin if start else None          # straight-line km from the previous stop (or from the start)
            for i, s in enumerate(part, 1):
                s["code"] = "D%d-%02d" % (n, i)
                s["leg_km"] = round(haversine_m(prev[0], prev[1], s["lat"], s["lon"]) / 1000, 1) if prev else None
                prev = (s["lat"], s["lon"])
            pts = [(s["lat"], s["lon"]) for s in part]
            days.append({"day": n, "area": area, "stops": part, "start": start[2] if start else None,
                         "origin": list(origin) if start else None,
                         "km": round(route_length(origin if start else pts[0], part) / 1000, 1),
                         "links": route_urls(([origin] if start else []) + pts)})
    return days


def counts(items):
    """{kind: number of items}, {group slug: number of items} - for the filter boxes."""
    by_kind = {k: sum(1 for it in items if k in it["kinds"]) for k in KINDS}
    by_group = {}
    for it in items:
        by_group[it["slug"]] = by_group.get(it["slug"], 0) + 1
    return by_kind, by_group


# ── 4. exports ───────────────────────────────────────────────────────────────

def _sensor_label(it):
    bits = [it["group"]]
    if it["id"]:
        bits.append("ID %s" % it["id"])
    if it["site_code"]:
        bits.append("site %s" % it["site_code"])
    return " ".join(bits) + ": " + str(it["name"] or it["ref_name"] or "")


# Columns of the field sheet: (header, width). The import finds them by header, so their order does not matter,
# but "Key" must stay: it says which sensor or spreadsheet row a line is about.
H_KEY, H_KINDS = "Key", "Kinds"
H_FOUND, H_RIGHT, H_LAT, H_LON, H_LABEL, H_REMARKS = "Found?", "Right position", "GPS lat", "GPS lon", "Label on site", "Remarks"
SHEET_COLUMNS = [
    ("Stop", 8), ("Group", 15), ("ID", 7), ("Site code", 9), ("Name", 28), ("Why it is on the list", 50), ("What to check", 46),
    ("Go to API position", 13), ("Go to spreadsheet position", 13),
    (H_FOUND, 11), (H_RIGHT, 20), (H_LAT, 11), (H_LON, 11), (H_LABEL, 18), (H_REMARKS, 32),
    ("Project", 16), ("Spreadsheet name", 22), ("Distance m", 9),
    (H_KEY, 12), (H_KINDS, 12), ("API lat", 10), ("API lon", 10), ("Sheet lat", 10), ("Sheet lon", 10),
]
_FILL_IN = (H_FOUND, H_RIGHT, H_LAT, H_LON, H_LABEL, H_REMARKS)
_HIDDEN = (H_KEY, H_KINDS, "API lat", "API lon", "Sheet lat", "Sheet lon")

READ_ME = [
    ("How to use this field sheet", True),
    ("One sheet per day, stops in driving order. Each row is one thing to check; several rows with the same stop are at the same place.", False),
    ("", False),
    ("On site, fill in the yellow columns:", True),
    ("Found?  Yes if the sensor is there, No if it is gone, Not sure if you could not tell. (Pick from the list.)", False),
    ("Right position  Which position was correct: the API's, the spreadsheet's, or Neither. Not applicable if the question did not arise.", False),
    ("GPS lat / GPS lon  Only if neither position is right: stand at the sensor and copy the coordinates from your phone map "
     "(pasting 'lat, lon' into GPS lat is fine).", False),
    ("Label on site  The label, site code or cabinet number you can read on the equipment.", False),
    ("Remarks  Anything else, for example the file names of your photos.", False),
    ("", False),
    ("Afterwards", True),
    ("In the Control Room, open the Site visits tab and press Import findings, then choose this file. You get a preview first; "
     "nothing changes until you apply it. Visited items then leave the plan and the findings turn into a list of corrections.", False),
    ("Do not delete the hidden columns (Key and the position columns): they tell the import which sensor a row is about. "
     "Rows can be sorted, filtered or left empty.", False),
    ("Tap 'Go to ...' to open the position in Google Maps for directions.", False),
]


def _sheet_name(day):
    name = "Day %d %s" % (day["day"], day["area"])
    return "".join(c for c in name if c not in '[]:*?/\\')[:31]


def write_field_sheet(days, path):
    """The field sheet as an Excel file: one sheet per day, stops in driving order, yellow columns to fill in on site
    (drop-down lists for Found? and Right position), links to navigate, print-ready, plus a Read me sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    head_fill = PatternFill("solid", fgColor="1F4E79")
    fill_in = PatternFill("solid", fgColor="FFF4C2")
    stop_fill = PatternFill("solid", fgColor="EEF2F7")
    thin = Side(style="thin", color="C8CED6")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(wrap_text=True, vertical="top")
    link_font = Font(color="0563C1", underline="single")

    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    ws.column_dimensions["A"].width = 110
    for i, (text, bold) in enumerate(READ_ME, 1):
        c = ws.cell(row=i, column=1, value=text)
        c.font = Font(bold=bold, size=13 if i == 1 else 11)
        c.alignment = Alignment(wrap_text=True, vertical="top")

    # why sensors are recommended, in words (same text as the Site visits tab), after the instructions
    exp = explanation()
    r = len(READ_ME) + 2
    ws.cell(row=r, column=1, value="Why these sensors are on the list").font = Font(bold=True, size=13)
    for rs in exp["reasons"]:
        r += 1
        ws.cell(row=r, column=1, value="%s: %s Why it matters: %s" % (rs["label"], rs["rule"], rs["matters"])).alignment = Alignment(wrap_text=True, vertical="top")
    r += 2
    ws.cell(row=r, column=1, value="How the days are made").font = Font(bold=True, size=13)
    for s_ in exp["steps"]:
        r += 1
        ws.cell(row=r, column=1, value=s_).alignment = Alignment(wrap_text=True, vertical="top")
    r += 2
    ws.cell(row=r, column=1, value="Not on the list").font = Font(bold=True, size=13)
    for s_ in exp["not_flagged"]:
        r += 1
        ws.cell(row=r, column=1, value=s_).alignment = Alignment(wrap_text=True, vertical="top")

    col = {h: i for i, (h, _w) in enumerate(SHEET_COLUMNS, 1)}
    for day in days:
        ws = wb.create_sheet(_sheet_name(day))
        nrows = sum(len(st["items"]) for st in day["stops"])
        ws["A1"] = "Day %d - %s: %d stops, about %s km in straight lines%s" % (
            day["day"], day["area"], len(day["stops"]), day["km"], (", starting at " + day["start"]) if day.get("start") else "")
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = "Fill in the yellow columns on site. Do not delete the hidden columns."
        ws["A2"].font = Font(italic=True, color="555555")
        for h, i in col.items():
            c = ws.cell(row=3, column=i, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="9A6B00") if h in _FILL_IN else head_fill
            c.alignment = Alignment(wrap_text=True, vertical="center")
            c.border = box
            ws.column_dimensions[get_column_letter(i)].width = dict(SHEET_COLUMNS)[h]
            if h in _HIDDEN:
                ws.column_dimensions[get_column_letter(i)].hidden = True
        ws.row_dimensions[3].height = 32
        r = 3
        for si, st in enumerate(day["stops"]):
            for it in st["items"]:
                r += 1
                pts = {p["label"]: p for p in it["points"]}
                api, ref = pts.get("API"), pts.get("Spreadsheet")
                checks = list(it["checks"]) + (["Your note: " + it["note"]] if it["note"] else [])
                vals = {"Stop": st["code"], "Group": it["group"], "ID": it["id"], "Site code": it["site_code"], "Name": it["name"] or it["ref_name"],
                        "Why it is on the list": "\n".join(it["why"] + (["The monitor says: " + it["status"]["text"]] if it.get("status") else [])),
                        "What to check": "\n".join(checks), "Project": it["project"], "Spreadsheet name": it["ref_name"],
                        "Distance m": it["dist"], H_KEY: it["key"], H_KINDS: ",".join(it["kinds"]),
                        "API lat": round(api["lat"], 6) if api else None, "API lon": round(api["lon"], 6) if api else None,
                        "Sheet lat": round(ref["lat"], 6) if ref else None, "Sheet lon": round(ref["lon"], 6) if ref else None}
                for h, v in vals.items():
                    ws.cell(row=r, column=col[h], value=v)
                for h, p in (("Go to API position", api), ("Go to spreadsheet position", ref)):
                    if p:
                        c = ws.cell(row=r, column=col[h], value="Navigate")
                        c.hyperlink = nav_url(p["lat"], p["lon"])
                        c.font = link_font
                for h, i in col.items():
                    c = ws.cell(row=r, column=i)
                    c.border = box
                    c.alignment = wrap
                    if h in _FILL_IN:
                        c.fill = fill_in
                    elif si % 2 == 0:
                        c.fill = stop_fill
                ws.cell(row=r, column=col["Stop"]).font = Font(bold=True)
        ws.freeze_panes = "C4"
        if nrows:
            ws.auto_filter.ref = "A3:%s%d" % (get_column_letter(len(SHEET_COLUMNS)), r)
            for h, choices in ((H_FOUND, FOUND_CHOICES), (H_RIGHT, RIGHT_CHOICES)):
                dv = DataValidation(type="list", formula1='"%s"' % ",".join(choices), allow_blank=True)
                dv.error, dv.errorTitle, dv.showErrorMessage = "Pick one from the list.", "Not in the list", True
                ws.add_data_validation(dv)
                L = get_column_letter(col[h])
                dv.add("%s4:%s%d" % (L, L, r))
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "3:3"
        ws.print_options.gridLines = False
    wb.save(path)


# ── 4b. the phone page ───────────────────────────────────────────────────────

MOBILE_HEAD = ["Key", "Group", "ID", "Name", "Kinds", "Found?", "Right position", "GPS lat", "GPS lon", "Label on site",
               "Remarks", "API lat", "API lon", "Sheet lat", "Sheet lon", "Day", "Stop"]

MOBILE_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Site visits</title>
<style>
:root{--bg:#f2f4f7;--card:#fff;--ink:#1d2733;--mute:#5d6b7a;--line:#d5dbe3;--blue:#1f6fb2;--green:#1e7a3c;--gbg:#dff3e5;--wbg:#fdebd0;--rbg:#f9d6d5;--yel:#fff4c2}
@media (prefers-color-scheme:dark){:root{--bg:#10161d;--card:#1a232d;--ink:#e6ebf0;--mute:#9aa8b6;--line:#2f3b48;--blue:#5aa9e6;--green:#58c77a;--gbg:#173824;--wbg:#4a3513;--rbg:#4a1f1d;--yel:#3d3510}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.4 -apple-system,Segoe UI,Roboto,Arial,sans-serif;padding-bottom:90px}
header{position:sticky;top:0;z-index:5;background:#1f4e79;color:#fff;padding:10px 14px calc(10px)}
header h1{margin:0;font-size:17px}header .sub{font-size:12px;opacity:.85}
select{width:100%;margin-top:8px;font-size:16px;padding:10px;border-radius:8px;border:0}
#next{background:var(--card);border-bottom:1px solid var(--line);padding:10px 14px;display:flex;gap:10px;align-items:center}
#next .t{flex:1;min-width:0}#next small{color:var(--mute)}
a.btn,button.btn{display:inline-block;text-align:center;text-decoration:none;border:0;border-radius:10px;padding:12px 16px;min-height:46px;font-size:16px;font-weight:600;background:var(--blue);color:#fff;cursor:pointer}
a.btn.alt,button.btn.alt{background:transparent;color:var(--blue);border:2px solid var(--blue)}
.links{padding:8px 14px;display:flex;flex-wrap:wrap;gap:8px}
.links .btn{padding:8px 12px;min-height:40px;font-size:14px}
.stop{margin:12px;background:var(--card);border-radius:14px;box-shadow:0 1px 4px rgba(0,0,0,.15);overflow:hidden}
.stop.done{opacity:.72}
.sh{display:flex;gap:10px;align-items:center;padding:12px 14px;background:rgba(31,78,121,.08);cursor:pointer}
.stop.done .sh{background:var(--gbg)}
.sh .c{font-weight:700;white-space:nowrap}.sh .n{flex:1;min-width:0}.sh small{color:var(--mute);display:block}
.chev{font-size:18px;color:var(--mute)}
.body{padding:4px 14px 14px}.stop.closed .body{display:none}
.item{border-top:1px dashed var(--line);padding:12px 0}.item:first-child{border-top:0}
.k{display:inline-block;background:rgba(31,78,121,.12);border-radius:6px;padding:1px 8px;margin:0 4px 4px 0;font-size:12px}
.chk{margin:6px 0;color:var(--ink)}.note{color:#a35a00;margin:4px 0}
.why{margin:6px 0;color:var(--mute);font-size:14px}.why b{color:var(--ink)}.mon{font-size:14px;margin:4px 0}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}
.lbl{font-weight:600;margin:10px 0 4px;font-size:14px;color:var(--mute)}
.seg{display:flex;gap:6px;flex-wrap:wrap}
.seg button{flex:1;min-width:84px;min-height:46px;border:2px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);font-size:15px}
.seg button.on{border-color:var(--blue);background:var(--blue);color:#fff;font-weight:700}
.seg button.on.no{background:#b03a2e;border-color:#b03a2e}
input[type=text],textarea{width:100%;font-size:16px;font-family:inherit;padding:10px;border-radius:8px;border:1px solid var(--line);background:var(--yel);color:var(--ink)}
textarea{min-height:64px}.row{display:flex;gap:8px}.row input{flex:1}
.res{font-size:13px;color:var(--green);margin-top:4px}
footer{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 12px calc(10px + env(safe-area-inset-bottom));display:flex;gap:8px}
footer .btn{flex:1}
#out{display:none;margin:12px;width:calc(100% - 24px);min-height:140px;font:12px monospace}
.empty{padding:30px 16px;color:var(--mute)}
</style></head><body>
<header><h1>Site visits</h1><div class="sub" id="sub"></div><select id="day" aria-label="Day"></select></header>
<div id="next"></div>
<div class="links" id="links"></div>
<main id="main"></main>
<textarea id="out" readonly></textarea>
<footer><button class="btn" id="share">Share / save results</button><button class="btn alt" id="copy">Copy</button></footer>
<script>
(function() {
  var D = __DATA__;
  var KEY = 'sv_' + D.id, DAYKEY = 'svday_' + D.id;
  var st = {}, warned = false, cur = 0;
  try { st = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; cur = +(localStorage.getItem(DAYKEY) || 0) || 0; } catch (e) { st = {}; }
  if (cur >= D.days.length) cur = 0;
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(st)); localStorage.setItem(DAYKEY, String(cur)); }
    catch (e) { if (!warned) { warned = true; alert('This browser cannot keep your answers between visits. Use "Share / save results" before you close the page.'); } }
  }
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function ans(k) { return st[k] || (st[k] = {}); }
  function isDone(it) { var a = st[it.key]; return !!a && (a.found === 'Yes' || a.found === 'No'); }
  function pt(it, label) { for (var i = 0; i < it.points.length; i++) if (it.points[i].label === label) return it.points[i]; return null; }
  function nav(p) { return 'https://www.google.com/maps/dir/?api=1&destination=' + p.lat.toFixed(6) + ',' + p.lon.toFixed(6) + '&travelmode=driving'; }
  function stopDone(s) { return s.items.every(isDone); }

  document.getElementById('sub').textContent = 'Plan made ' + D.generated + (D.days[0] && D.days[0].start ? ' · every day starts at ' + D.days[0].start : '');
  var sel = document.getElementById('day');
  D.days.forEach(function(d, i) {
    var o = document.createElement('option'); o.value = i;
    o.textContent = 'Day ' + d.day + ' – ' + d.area + ' (' + d.stops.length + ' stops, ~' + d.km + ' km)';
    sel.appendChild(o);
  });
  sel.value = cur;
  sel.onchange = function() { cur = +sel.value; save(); render(); window.scrollTo(0, 0); };

  function render() {
    var y = window.scrollY, d = D.days[cur], main = document.getElementById('main');
    main.innerHTML = '';
    if (!d) { main.innerHTML = '<div class="empty">Nothing to visit.</div>'; return; }
    var total = 0, done = 0, next = null;
    d.stops.forEach(function(s) { s.items.forEach(function(it) { total++; if (isDone(it)) done++; }); if (!next && !stopDone(s)) next = s; });
    var nx = document.getElementById('next');
    nx.innerHTML = next
      ? '<div class="t"><small>' + done + ' of ' + total + ' done · next stop</small><br><b>' + esc(next.code) + '</b> ' + esc(next.items[0].name || next.items[0].ref_name) + '</div><a class="btn" href="' + esc(nav(next)) + '">Navigate</a>'
      : '<div class="t"><b>All ' + total + ' done.</b><br><small>Press "Share / save results" below.</small></div>';
    document.getElementById('links').innerHTML = d.links.map(function(u, i) {
      return '<a class="btn alt" href="' + esc(u) + '">Whole day in Maps' + (d.links.length > 1 ? ' (' + (i + 1) + '/' + d.links.length + ')' : '') + '</a>';
    }).join('');
    d.stops.forEach(function(s) {
      var sec = document.createElement('section'), fin = stopDone(s);
      sec.className = 'stop' + (fin ? ' done closed' : '');
      var h = '<div class="sh" data-toggle="1"><span class="c">' + esc(s.code) + '</span><span class="n"><b>' + esc(s.items[0].name || s.items[0].ref_name) + '</b>' +
              '<small>' + (fin ? '✓ done' : s.items.length + ' to check') + (s.leg_km != null ? ' - ' + s.leg_km + ' km from the previous stop' : '') + '</small></span><span class="chev">' + (fin ? '▸' : '▾') + '</span></div><div class="body">';
      s.items.forEach(function(it) {
        var a = st[it.key] || {};
        h += '<div class="item"><b>' + esc(it.group) + (it.id ? ' ' + esc(it.id) : '') + (it.site_code ? ' (site ' + esc(it.site_code) + ')' : '') + '</b>';
        if (it.project) h += ' <span class="k">' + esc(it.project) + '</span>';
        h += '<br>' + it.kinds.map(function(k) { return '<span class="k">' + esc(D.kinds[k] || k) + '</span>'; }).join('');
        if (it.why && it.why.length) h += '<div class="why"><b>Why it is on the list:</b> ' + it.why.map(esc).join(' ') + '</div>';
        if (it.status) h += '<div class="mon"><b>The monitor says:</b> ' + esc(it.status.text) + '</div>';
        h += '<div class="chk"><b>To check:</b> ' + it.checks.map(esc).join('<br>') + '</div>';
        if (it.note) h += '<div class="note">Your note: ' + esc(it.note) + '</div>';
        h += '<div class="nav">' + it.points.map(function(p, i) { return '<a class="btn' + (i ? ' alt' : '') + '" href="' + esc(nav(p)) + '">' + (it.points.length > 1 ? 'Go to ' + esc(p.label.toLowerCase()) + ' position' : 'Navigate') + '</a>'; }).join('') + '</div>';
        h += '<div class="lbl">Found?</div><div class="seg">' + ['Yes', 'No', 'Not sure'].map(function(v) {
          return '<button data-k="' + esc(it.key) + '" data-f="found" data-v="' + v + '" class="' + (a.found === v ? 'on' : '') + (v === 'No' ? ' no' : '') + '">' + v + '</button>'; }).join('') + '</div>';
        if (it.points.length > 1) h += '<div class="lbl">Which position is right?</div><div class="seg">' + ['API position', 'Spreadsheet position', 'Neither'].map(function(v) {
          return '<button data-k="' + esc(it.key) + '" data-f="right" data-v="' + v + '" class="' + (a.right === v ? 'on' : '') + '">' + v + '</button>'; }).join('') + '</div>';
        h += '<div class="lbl">GPS where the sensor really is (only if neither position is right)</div><div class="row"><input type="text" inputmode="decimal" placeholder="35.123456, 33.123456" data-k="' + esc(it.key) + '" data-f="gps" value="' + esc(a.gps || '') + '">' +
             '<button class="btn alt" data-gps="' + esc(it.key) + '">Use my location</button></div><div class="res" id="r-' + esc(it.key) + '"></div>';
        h += '<div class="lbl">Label on site</div><input type="text" data-k="' + esc(it.key) + '" data-f="label" value="' + esc(a.label || '') + '">';
        h += '<div class="lbl">Remarks</div><textarea data-k="' + esc(it.key) + '" data-f="remarks">' + esc(a.remarks || '') + '</textarea></div>';
      });
      sec.innerHTML = h + '</div>';
      main.appendChild(sec);
    });
    window.scrollTo(0, y);
  }

  document.getElementById('main').addEventListener('click', function(e) {
    var b = e.target.closest('button[data-f]');
    if (b) { var a = ans(b.getAttribute('data-k')), f = b.getAttribute('data-f'), v = b.getAttribute('data-v'); a[f] = a[f] === v ? '' : v; save(); render(); return; }
    var g = e.target.closest('button[data-gps]');
    if (g) {
      var key = g.getAttribute('data-gps'), box = document.getElementById('r-' + key);
      if (!navigator.geolocation) { box.textContent = 'This browser has no location. Copy the coordinates from your map app instead.'; return; }
      box.textContent = 'Getting your location...';
      navigator.geolocation.getCurrentPosition(function(p) {
        var txt = p.coords.latitude.toFixed(6) + ', ' + p.coords.longitude.toFixed(6);
        ans(key).gps = txt; save();
        var inp = document.querySelector('input[data-f="gps"][data-k="' + key.replace(/"/g, '\\"') + '"]'); if (inp) inp.value = txt;
        box.textContent = 'Saved (accuracy about ' + Math.round(p.coords.accuracy) + ' m).';
      }, function() { box.textContent = 'No location (not allowed, or this page is opened in a way that blocks it). Copy the coordinates from your map app instead.'; },
      {enableHighAccuracy: true, timeout: 20000, maximumAge: 0});
      return;
    }
    var t = e.target.closest('[data-toggle]');
    if (t) t.parentNode.classList.toggle('closed');
  });
  document.getElementById('main').addEventListener('input', function(e) {
    var el = e.target, k = el.getAttribute && el.getAttribute('data-k');
    if (k && el.getAttribute('data-f')) { ans(k)[el.getAttribute('data-f')] = el.value; save(); }
  });

  function csvCell(v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; }
  function rows() {
    var out = [D.head];
    D.days.forEach(function(d) { d.stops.forEach(function(s) { s.items.forEach(function(it) {
      var a = st[it.key];
      if (!a || !(a.found || a.right || a.gps || a.label || a.remarks)) return;
      var api = pt(it, 'API'), sh = pt(it, 'Spreadsheet');
      // GPS goes into "GPS lat" as typed: the import splits 'lat, lon' by itself
      out.push([it.key, it.group, it.id || '', it.name || it.ref_name || '', it.kinds.join(','), a.found || '', a.right || '', a.gps || '', '', a.label || '', a.remarks || '',
                api ? api.lat : '', api ? api.lon : '', sh ? sh.lat : '', sh ? sh.lon : '', d.day, s.code]);
    }); }); });
    return out;
  }
  function csv() { return '﻿' + rows().map(function(r) { return r.map(csvCell).join(','); }).join('\r\n'); }
  function stamp() { var n = new Date(), p = function(x) { return (x < 10 ? '0' : '') + x; }; return n.getFullYear() + p(n.getMonth() + 1) + p(n.getDate()) + '_' + p(n.getHours()) + p(n.getMinutes()); }
  function showText(text) { var o = document.getElementById('out'); o.style.display = 'block'; o.value = text; o.focus(); o.select(); o.scrollIntoView(); }
  document.getElementById('share').onclick = function() {
    if (rows().length < 2) { alert('No answers yet.'); return; }
    var name = 'site_visit_findings_' + stamp() + '.csv', text = csv(), file = null;
    try { file = new File([text], name, {type: 'text/csv'}); } catch (e) {}
    if (file && navigator.canShare && navigator.canShare({files: [file]})) {
      navigator.share({files: [file], title: name}).catch(function() {});
      return;
    }
    try {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([text], {type: 'text/csv'})); a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
    } catch (e) { showText(text); }
  };
  document.getElementById('copy').onclick = function() {
    if (rows().length < 2) { alert('No answers yet.'); return; }
    var text = csv();
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function() { alert('Copied. Paste it into an e-mail or note to yourself.'); }, function() { showText(text); });
    else showText(text);
  };
  render();
})();
</script></body></html>"""


def mobile_html(days, generated=None):
    """The phone page: one self-contained HTML file (no internet needed once it is on the phone) with the days as big,
    tappable cards: Navigate buttons (Google Maps), Found? / Right position buttons, GPS, label, remarks. Answers are kept in
    the phone's browser; "Share / save results" makes a CSV with the same columns as the field sheet, which the import reads."""
    import hashlib
    import json
    keys = sorted(it["key"] for d in days for s in d["stops"] for it in s["items"])
    data = {
        "id": hashlib.sha1("\n".join(keys).encode("utf-8")).hexdigest()[:10],      # same stops = same saved answers
        "generated": (generated or datetime.now()).strftime("%d/%m/%Y %H:%M"), "kinds": KINDS, "head": MOBILE_HEAD,
        "days": [{"day": d["day"], "area": d["area"], "km": d["km"], "start": d.get("start"), "links": d["links"],
                  "stops": [{"code": s["code"], "lat": s["lat"], "lon": s["lon"],
                             "leg_km": s.get("leg_km"),
                             "items": [{k: it[k] for k in ("key", "group", "id", "site_code", "name", "ref_name", "kinds", "checks",
                                                           "why", "status", "note", "project", "points", "dist")} for it in s["items"]]}
                            for s in d["stops"]]} for d in days]}
    return MOBILE_PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/").replace("<!--", "<\\!--"))


# ── 5. findings: read the filled-in sheet back, say what each finding means ───

CYPRUS = (34.0, 36.0, 32.0, 35.0)       # lat min/max, lon min/max: a GPS fix outside this is a typo
MAX_IMPORT_BYTES = 10_000_000


def _norm_found(v):
    s = str(v or "").strip().lower()
    if s in ("yes", "y"):
        return "yes"
    if s in ("no", "n"):
        return "no"
    if s.startswith("not sure") or s in ("unsure", "?"):
        return "unsure"
    return None if s else ""            # None = not understood


def _norm_right(v):
    s = str(v or "").strip().lower()
    if s.startswith("api"):
        return "api"
    if s.startswith("spread") or s.startswith("sheet"):
        return "sheet"
    if s.startswith("neither"):
        return "neither"
    return "" if (not s or s.startswith("not appl") or s == "n/a") else None


def _numbers(v):
    return [float(x.replace(",", ".")) for x in re.findall(r"-?\d+(?:[.,]\d+)?", str(v if v is not None else ""))]


def _gps(lat_cell, lon_cell):
    """(lat, lon, problem) from the two GPS cells; a pasted 'lat, lon' in the first cell is split."""
    a, b = _numbers(lat_cell), _numbers(lon_cell)
    if len(a) >= 2 and not b:
        a, b = a[:1], a[1:2]
    if not a and not b:
        return None, None, None
    if not a or not b:
        return None, None, "only one of GPS lat / GPS lon was filled in"
    lat, lon = a[0], b[0]
    if not (CYPRUS[0] <= lat <= CYPRUS[1] and CYPRUS[2] <= lon <= CYPRUS[3]):
        return None, None, "GPS %s, %s is not in Cyprus (lat and lon swapped?)" % (lat, lon)
    return lat, lon, None


def _table_rows(path):
    """[(where, {header: value})] from every sheet of an .xlsx (or from a .csv) that has a 'Key' header row."""
    path = str(path)
    out = []
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            sheets = [("csv", list(csv.reader(f)))]
    elif path.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        sheets = [(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    else:
        raise ValueError("choose the .xlsx field sheet (or a .csv with the same columns)")
    for title, rows in sheets:
        head = next((i for i, r in enumerate(rows[:15]) if H_KEY in [str(c).strip() for c in r if c is not None]), None)
        if head is None:
            continue
        names = [str(c).strip() if c is not None else "" for c in rows[head]]
        for n, r in enumerate(rows[head + 1:], head + 2):
            d = {names[i]: r[i] for i in range(min(len(names), len(r))) if names[i]}
            if str(d.get(H_KEY) or "").strip():
                out.append(("%s row %d" % (title, n), d))
    return out


def read_findings(path):
    """Read a filled-in field sheet. -> (rows, warnings); a row is what was written for one sensor or spreadsheet row."""
    if Path(path).stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("that file is too big to be a field sheet")
    table = _table_rows(path)
    if not table:
        raise ValueError("no field-sheet rows found: the file needs the 'Key' column of the sheet saved from the Site visits tab")
    rows, warnings = [], []
    for where, d in table:
        found, right = _norm_found(d.get(H_FOUND)), _norm_right(d.get(H_RIGHT))
        if found is None:
            warnings.append("%s: Found? '%s' not understood (use Yes, No or Not sure): ignored" % (where, d.get(H_FOUND)))
            found = ""
        if right is None:
            warnings.append("%s: Right position '%s' not understood: ignored" % (where, d.get(H_RIGHT)))
            right = ""
        lat, lon, bad = _gps(d.get(H_LAT), d.get(H_LON))
        if bad:
            warnings.append("%s: %s: GPS ignored" % (where, bad))
        def num(a, b):          # Excel gives numbers, a CSV (from the phone page) gives text
            try:
                return [float(str(d[a]).strip()), float(str(d[b]).strip())]
            except (KeyError, ValueError):
                return None
        rows.append({"key": str(d[H_KEY]).strip(), "where": where, "group": str(d.get("Group") or ""), "id": str(d.get("ID") or ""),
                     "name": str(d.get("Name") or ""), "kinds": [k for k in str(d.get(H_KINDS) or "").split(",") if k in KINDS],
                     "found": found, "right": right, "lat": lat, "lon": lon,
                     "label": str(d.get(H_LABEL) or "").strip(), "remarks": str(d.get(H_REMARKS) or "").strip(),
                     "api": num("API lat", "API lon"), "sheet": num("Sheet lat", "Sheet lon")})
    return rows, warnings


def build_entries(rows, store, now=None):
    """(entries to save, summary) from read_findings rows. Rows with nothing filled in are skipped."""
    now = (now or datetime.now()).isoformat(timespec="seconds")
    entries, skipped = {}, 0
    for r in rows:
        if not (r["found"] or r["right"] or r["lat"] is not None or r["label"] or r["remarks"]):
            skipped += 1
            continue
        entries[r["key"]] = {"found": r["found"], "right": r["right"], "lat": r["lat"], "lon": r["lon"], "label": r["label"],
                             "remarks": r["remarks"], "done": r["found"] in ("yes", "no"), "at": now, "source": "import",
                             "group": r["group"], "id": r["id"], "name": r["name"], "kinds": r["kinds"], "api": r["api"], "sheet": r["sheet"]}
    new = sum(1 for k in entries if k not in store)
    return entries, {"rows": len(rows), "answered": len(entries), "skipped_empty": skipped, "new": new,
                     "updated": len(entries) - new, "done": sum(1 for e in entries.values() if e["done"])}


def _pos(lat, lon):
    return "%.6f, %.6f" % (lat, lon)


def correct_position(e):
    """[lat, lon] the sensor really is at, according to the finding; None if it was not said."""
    if e.get("lat") is not None:
        return [e["lat"], e["lon"]]
    return {"api": e.get("api"), "sheet": e.get("sheet")}.get(e.get("right")) or None


def finding_action(e):
    """(code, text): what the finding means for you."""
    kinds, found, right = e.get("kinds") or [], e.get("found"), e.get("right")
    where = correct_position(e)
    at = " (%s)" % _pos(*where) if where else ""
    if found == "no":
        return "not_found", "Not on site. If the API still lists it, ask the contractor or SWARCO; if the spreadsheet lists it as active, mark it decommissioned."
    if found == "unsure":
        return "unsure", "Could not tell on site: visit again or ask the contractor."
    if found != "yes":
        return "open", "No answer yet."
    if right == "api":
        return "fix_sheet", "Spreadsheet position is wrong: set it to the API position%s." % at
    if right == "sheet":
        return "fix_api", "API position is wrong: ask SWARCO to move it to the spreadsheet position%s." % at
    if right == "neither":
        return "fix_both", ("Both positions are wrong: the sensor is at %s. Correct the spreadsheet and ask SWARCO to correct the API." % _pos(*where)
                            if where else "Both positions are wrong, but no GPS fix was given: measure it on the next visit.")
    if "api_only" in kinds:
        return "add_sheet", "Installed but missing in the spreadsheet: add a row%s." % at
    if "ref_only" in kinds or "retired" in kinds:
        return "not_in_api", "Installed but not reported by the API: check power and communication with the contractor or SWARCO."
    if "faulty" in kinds:
        return "check_contractor", "Installed, but its Bluetooth paths fail: pass the remarks on to the contractor."
    if {"far_apart", "pair"} & set(kinds):
        return "incomplete", "Found, but which position is right was not filled in."
    return "ok", "Found, nothing to correct."


ACTIONS = {
    "fix_sheet": "Correct in the spreadsheet", "add_sheet": "Add to the spreadsheet", "fix_api": "Ask SWARCO to correct the API position",
    "fix_both": "Correct both", "not_in_api": "Installed but not in the API", "not_found": "Not found on site",
    "check_contractor": "Pass to the contractor", "unsure": "Not sure: visit again", "incomplete": "Answer incomplete",
    "open": "No answer yet", "ok": "Found, nothing to correct",
}


ACTIONS_ORDER = list(ACTIONS)


def visit_summary(e):
    """One line for the plan: what was written for this visit."""
    bits = []
    if e.get("found"):
        bits.append("Found: " + {"yes": "yes", "no": "no", "unsure": "not sure"}[e["found"]])
    if e.get("right"):
        bits.append("right position: " + {"api": "API", "sheet": "spreadsheet", "neither": "neither"}[e["right"]])
    if e.get("lat") is not None:
        bits.append("GPS " + _pos(e["lat"], e["lon"]))
    if e.get("label"):
        bits.append("label: " + e["label"])
    if e.get("remarks"):
        bits.append("remarks: " + e["remarks"])
    return "; ".join(bits)


def note_line(e):
    """The text added to the sensor's note when findings are imported (dated, one line)."""
    return "Site visit %s: %s" % ((e.get("at") or "")[:10], visit_summary(e) or "visited")


def findings_list(store):
    """[{key, name, group, id, found, ..., action, action_text, position}] for every visit with an answer, by action."""
    order = list(ACTIONS)
    out = []
    for key, e in store.items():
        if not (e.get("found") or e.get("right") or e.get("lat") is not None or e.get("label") or e.get("remarks")):
            continue                        # only marked visited by hand: no finding to act on
        code, text = finding_action(e)
        out.append(dict(e, key=key, action=code, action_label=ACTIONS[code], action_text=text, position=correct_position(e),
                        summary=visit_summary(e)))
    out.sort(key=lambda f: (order.index(f["action"]), f.get("group") or "", f.get("name") or ""))
    return out


def review_rows(groups, notes, visits):
    """Everything you wrote or checked, whether or not it is on the plan: [{key, slug, group, id, name, site_code, lat, lon,
    note, note_at, visit}]. Not affected by the plan's filters, so you can look at your notes and visits without replanning.

    groups: [{"slug", "group", "api"}]; notes: {"<slug>:<id>": {"note", "at"}}; visits: the saved findings store.
    visit = {done, summary, action_label, action_text, at, source} (action_* only once Found? or a right position was answered)."""
    names = {g["slug"]: g["group"] for g in groups}
    api = {g["slug"]: {str(s["id"]): s for s in g.get("api", [])} for g in groups}
    rows = {}

    def row(key, slug, sid, entry=None):
        r = rows.get(key)
        if r is None:
            s = api.get(slug, {}).get(sid) if sid else None
            r = rows[key] = {"key": key, "slug": slug, "group": names.get(slug, slug), "id": sid or None,
                             "name": (s or {}).get("name") or (entry or {}).get("name") or sid or "",
                             "site_code": (s or {}).get("site_code"), "lat": (s or {}).get("lat"), "lon": (s or {}).get("lon"),
                             "note": None, "note_at": None, "visit": None}
        return r

    for k, n in notes.items():
        slug, _, sid = k.partition(":")
        if slug in names and isinstance(n, dict) and str(n.get("note") or "").strip():
            r = row("%s|api|%s" % (slug, sid), slug, sid)
            r["note"], r["note_at"] = n["note"], str(n.get("at") or "")[:10]
    for key, e in visits.items():
        slug, kind, rest = (key.split("|", 2) + ["", ""])[:3]
        if slug not in names:
            continue
        r = row(key, slug, rest if kind == "api" else None, e)
        if r["lat"] is None:                                    # a removed sensor or a spreadsheet row: where it was on the sheet
            pos = e.get("api") or e.get("sheet")
            if not pos and kind == "ref":
                try:
                    pos = [float(x) for x in rest.split("|")[0].split(",")]
                except ValueError:
                    pos = None
            if pos:
                r["lat"], r["lon"] = pos[0], pos[1]
        answered = bool(e.get("found") or e.get("right"))
        code, text = finding_action(e) if answered else (None, "")
        r["visit"] = {"done": bool(e.get("done")), "summary": visit_summary(e), "action_label": ACTIONS[code] if code else "",
                      "action_text": text, "at": str(e.get("at") or "")[:10], "source": e.get("source") or ""}
    return sorted(rows.values(), key=lambda r: (r["group"], str(r["name"]).lower()))


def corrections_rows(store):
    """CSV rows of what the visits found, one line per finding, grouped by what you need to do."""
    rows = [["What to do", "Group", "ID", "Name", "Correct lat", "Correct lon", "API lat", "API lon", "Sheet lat", "Sheet lon",
             "Label on site", "Remarks", "Visited", "Details"]]
    for f in findings_list(store):
        pos, api, sheet = f["position"], f.get("api"), f.get("sheet")
        rows.append([f["action_label"], f.get("group"), f.get("id"), f.get("name"),
                     pos[0] if pos else None, pos[1] if pos else None, api[0] if api else None, api[1] if api else None,
                     sheet[0] if sheet else None, sheet[1] if sheet else None, f.get("label"), f.get("remarks"),
                     (f.get("at") or "")[:10], f["action_text"]])
    return rows


_DAY_COLOURS = ["ff2f4fc0", "ff2283e6", "ff60ae27", "ffad448e", "ff9c8a16", "ff2b39c0", "ff5e3a27", "ff8d8c7f"]   # KML aabbggrr


def to_kml(days, title="Site visits"):
    """KML for Google My Maps: a folder per day with numbered stops, the second position of each sensor, and the route."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           "<name>%s</name>" % escape(title)]
    for i, col in enumerate(_DAY_COLOURS):
        out.append('<Style id="d%d"><IconStyle><color>%s</color></IconStyle><LineStyle><color>%s</color><width>3</width>'
                   '</LineStyle></Style>' % (i, col, col))
    out.append('<Style id="other"><IconStyle><color>ffb0b0b0</color><scale>0.7</scale></IconStyle></Style>')
    for day in days:
        sid = "#d%d" % ((day["day"] - 1) % len(_DAY_COLOURS))
        out.append("<Folder><name>Day %d - %s (%d stops)</name>" % (day["day"], escape(day["area"]), len(day["stops"])))
        for st in day["stops"]:
            desc = []
            for it in st["items"]:
                desc.append("<b>%s</b> (%s)<br>Why: %s<br>To check: %s" % (
                    escape(_sensor_label(it)), escape(", ".join(KINDS[k] for k in it["kinds"])),
                    " ".join(escape(w) for w in it["why"]), "<br>".join(escape(c) for c in it["checks"])))
                if it["note"]:
                    desc.append("Your note: " + escape(it["note"]))
                for p in it["points"]:
                    desc.append('%s position: <a href="%s">navigate</a> (%.6f, %.6f)' % (p["label"], escape(nav_url(p["lat"], p["lon"])), p["lat"], p["lon"]))
            name = "%s %s" % (st["code"], st["items"][0]["name"] or st["items"][0]["ref_name"] or "")
            out.append("<Placemark><name>%s</name><styleUrl>%s</styleUrl><description><![CDATA[%s]]></description>"
                       "<Point><coordinates>%.6f,%.6f,0</coordinates></Point></Placemark>"
                       % (escape(name), sid, "<br><br>".join(desc).replace("]]>", "]] >"), st["lon"], st["lat"]))
            for it in st["items"]:
                for p in it["points"][1:]:
                    if haversine_m(st["lat"], st["lon"], p["lat"], p["lon"]) > 20:
                        out.append("<Placemark><name>%s (%s position)</name><styleUrl>#other</styleUrl>"
                                   "<Point><coordinates>%.6f,%.6f,0</coordinates></Point></Placemark>"
                                   % (escape(st["code"]), escape(p["label"].lower()), p["lon"], p["lat"]))
        if day.get("origin"):
            out.append("<Placemark><name>Start: %s</name><styleUrl>%s</styleUrl><Point><coordinates>%.6f,%.6f,0</coordinates></Point></Placemark>"
                       % (escape(day.get("start") or "start"), sid, day["origin"][1], day["origin"][0]))
        if len(day["stops"]) > 1 or day.get("origin"):
            coords = " ".join("%.6f,%.6f,0" % (lon, lat) for lat, lon in
                              ([day["origin"]] if day.get("origin") else []) + [(s["lat"], s["lon"]) for s in day["stops"]])
            out.append("<Placemark><name>Day %d route (straight lines, in visiting order)</name><styleUrl>%s</styleUrl>"
                       "<LineString><coordinates>%s</coordinates></LineString></Placemark>" % (day["day"], sid, coords))
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(out)
