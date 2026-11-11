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
    Bluetooth only), "notes" ({id: {"note"}})}]. An item: {key, group, slug, kinds [..], id, name, site_code, project,
    ref_name, dist, points [{label, lat, lon}] (first = where to go), checks [text], note}.
    """
    items = {}

    def item(key, g, kind, api=None, ref=None, dist=None):
        it = items.get(key)
        if it is None:
            it = items[key] = {"key": "|".join(str(k) for k in key), "group": g["group"], "slug": g["slug"], "kinds": [], "checks": [],
                               "id": str(api["id"]) if api else None, "name": (api or ref or {}).get("name"),
                               "site_code": api.get("site_code") if api else None,
                               "project": (api or {}).get("project") or (ref or {}).get("project"),
                               "ref_name": ref.get("name") if ref else None, "dist": dist, "points": [], "note": None}
            if _has_coords(api):
                it["points"].append(_point("API", api["lat"], api["lon"]))
            if _has_coords(ref):
                it["points"].append(_point("Spreadsheet", ref["lat"], ref["lon"]))
        if kind not in it["kinds"]:
            it["kinds"].append(kind)
        return it

    for g in groups:
        api_only, ref_only = [], []
        for m in g.get("matches", []):
            api, ref = m.get("api"), m.get("ref")
            if m["type"] == "match" and (m.get("distance_m") or 0) >= warn_m and _has_coords(api) and _has_coords(ref):
                it = item((g["slug"], "api", str(api["id"])), g, "far_apart", api, ref, round(m["distance_m"]))
                it["checks"].append("Spreadsheet and API are %d m apart: which position is right?" % round(m["distance_m"]))
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
        for ai, a in enumerate(api_only):
            if ai not in used_a:
                item((g["slug"], "api", str(a["id"])), g, "api_only", a)["checks"].append(
                    "In the API but not in the spreadsheet: is a sensor installed here? Note its label and project.")
        for ri, r in enumerate(ref_only):
            if ri not in used_r:
                key = (g["slug"], "ref", "%.5f,%.5f" % (r["lat"], r["lon"]), r.get("name"))
                item(key, g, "ref_only", None, r)["checks"].append(
                    "In the spreadsheet (active) but no API sensor: is it installed, powered and connected?")

        by_id = {str(s["id"]): s for s in g.get("api", [])}
        for sid, s in by_id.items():
            if not s.get("active", True) and _has_coords(s):
                item((g["slug"], "api", sid), g, "retired", s)["checks"].append(
                    "Missing from the latest API data: is it still installed? Removed, damaged or without power?")
        for sid, v in (g.get("verdicts") or {}).items():
            s = by_id.get(str(sid))
            if v in ("faulty_high", "faulty_likely") and _has_coords(s) and s.get("active", True):
                item((g["slug"], "api", str(sid)), g, "faulty", s)["checks"].append(
                    "No Bluetooth path through it works: check power, antenna and communication.")
        for sid, n in (g.get("notes") or {}).items():
            s = by_id.get(str(sid))
            if _has_coords(s):
                it = item((g["slug"], "api", str(sid)), g, "note", s)
                it["note"] = n.get("note") if isinstance(n, dict) else str(n)
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


def plan(items, per_day=PER_DAY, kinds=None, groups=None, merge_m=MERGE_M, areas=AREAS):
    """[{area, day, stops, km}] - one entry per day, areas in AREAS order, stops numbered in driving order."""
    per_day = max(1, int(per_day))
    chosen = [it for it in items
              if (kinds is None or set(it["kinds"]) & set(kinds)) and (groups is None or it["slug"] in groups)]
    stops = group_stops(chosen, merge_m)
    days, n = [], 0
    for area in areas:
        mine = [s for s in stops if s["area"] == area]
        if not mine:
            continue
        route = order_route(mine, areas[area])
        for k in range(0, len(route), per_day):
            n += 1
            part = route[k:k + per_day]
            for i, s in enumerate(part, 1):
                s["code"] = "D%d-%02d" % (n, i)
            days.append({"day": n, "area": area, "stops": part,
                         "km": round(route_length((part[0]["lat"], part[0]["lon"]), part) / 1000, 1),
                         "links": route_urls([(s["lat"], s["lon"]) for s in part])})
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
    ("Stop", 8), ("Group", 15), ("ID", 7), ("Site code", 9), ("Name", 28), ("What to check", 52),
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

    col = {h: i for i, (h, _w) in enumerate(SHEET_COLUMNS, 1)}
    for day in days:
        ws = wb.create_sheet(_sheet_name(day))
        nrows = sum(len(st["items"]) for st in day["stops"])
        ws["A1"] = "Day %d - %s: %d stops, about %s km in straight lines" % (day["day"], day["area"], len(day["stops"]), day["km"])
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
        num = lambda a, b: [float(d[a]), float(d[b])] if isinstance(d.get(a), (int, float)) and isinstance(d.get(b), (int, float)) else None
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
                desc.append("<b>%s</b> (%s)<br>%s" % (escape(_sensor_label(it)), escape(", ".join(KINDS[k] for k in it["kinds"])),
                                                        "<br>".join(escape(c) for c in it["checks"])))
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
        if len(day["stops"]) > 1:
            coords = " ".join("%.6f,%.6f,0" % (s["lon"], s["lat"]) for s in day["stops"])
            out.append("<Placemark><name>Day %d route (straight lines, in visiting order)</name><styleUrl>%s</styleUrl>"
                       "<LineString><coordinates>%s</coordinates></LineString></Placemark>" % (day["day"], sid, coords))
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(out)
