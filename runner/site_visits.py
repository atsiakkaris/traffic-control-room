"""
site_visits.py - plan on-site checks for sensors whose position or existence is in doubt.

Pure functions (no DB, no network). The Control Room feeds in what the sensor-location pages already compute
(API sensors, spreadsheet rows, their matches, the Bluetooth verdicts and the sensor notes) and gets back:

  1. the things to check (collect_items): positions that disagree, a sensor on one side only, retired sensors,
     faulty Bluetooth sensors, sensors you wrote a note on;
  2. stops: things closer than MERGE_M are one visit (group_stops);
  3. days: stops grouped by area (nearest of AREAS), put in driving order (nearest neighbour, then 2-opt on the
     straight-line distance) and cut into days of at most `per_day` stops (plan);
  4. exports: a field sheet (CSV, with empty columns to fill in on site) and a KML file for Google My Maps.

Straight-line distance is good enough to put stops in order; nothing is sent to a routing service.
"""
import math
from html import escape
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

FIELD_COLUMNS = ["Found? (yes/no)", "Right position (API/sheet/neither)", "GPS lat", "GPS lon",
                 "Label / site code on site", "Photo", "Remarks"]


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
            it = items[key] = {"key": key, "group": g["group"], "slug": g["slug"], "kinds": [], "checks": [],
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


def field_sheet_rows(days):
    """The field sheet: one row per thing to check, in driving order, with empty columns to fill in on site."""
    rows = [["Day", "Stop", "Area", "Group", "Sensor ID", "Site code", "Name", "Spreadsheet name", "Project",
             "What to check", "API lat", "API lon", "Sheet lat", "Sheet lon", "Distance m", "Navigate"] + FIELD_COLUMNS]
    for day in days:
        for st in day["stops"]:
            for it in st["items"]:
                pts = {p["label"]: p for p in it["points"]}
                api, ref = pts.get("API"), pts.get("Spreadsheet")
                checks = list(it["checks"]) + (["Your note: " + it["note"]] if it["note"] else [])
                rows.append([day["day"], st["code"], day["area"], it["group"], it["id"], it["site_code"], it["name"],
                             it["ref_name"], it["project"], " | ".join(checks),
                             round(api["lat"], 6) if api else None, round(api["lon"], 6) if api else None,
                             round(ref["lat"], 6) if ref else None, round(ref["lon"], 6) if ref else None,
                             it["dist"], nav_url(st["lat"], st["lon"])] + [None] * len(FIELD_COLUMNS))
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
