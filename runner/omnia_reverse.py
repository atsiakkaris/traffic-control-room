"""
omnia_reverse.py - work out (and check) the reverse of an OMNIA path, and create it. Everything up to plan_reverse only READS;
create_reverse is the one function that WRITES (a new path in OMNIA), and the Control Room only calls it after a passing
preview and a typed YES.

How OMNIA stores a path (seen in its own map editor, see ARCHITECTURE.md): a chain of directed road arcs. Three read calls:
  * arcs     (iPathId=<id>)                         the arc chain of an existing path, in order
  * nearest  (x=<lon>, y=<lat>)                     the arcs around a point: id, street, length, direction arrow (arrowN)
  * calc     (repeated listTimingPoints=<arc id>)   the route through those arcs; saves nothing
A road is often two separate sets of arcs, one per direction, and the opposite arc is NOT always the same id +500,000,000
(it is on some roads, not on others: a hand-made reverse of a Limassol avenue used unrelated ids). What does hold, tested on a
hand-made reverse: giving OMNIA's own router only the opposite-direction arc at each END returns the whole reverse route.
So the reverse is found like this: at each end of the forward path ask for the nearest arcs, keep those on the same street
running the opposite way (arrow code + 4, the 8 codes go round the compass), let OMNIA calculate the route between the two end
arcs, and accept it only if its length is close to the forward length and its ends sit where the forward path's ends are.

Creating, as OMNIA's own editor does it on Save (captured in the browser):
  * newid    (GET, _dc=<time>)                      the id for the new path, handed out by OMNIA's server
  * build    (form: repeated listPathArcs=<arc id>, pathName, pathId)   saves the path
then the existing edit call sets speeds and nodes (copied from the forward path, nodes swapped).

The addresses are never in the code, not even their names (the repo is public): each one is a full address in the
git-ignored .env (CONTROL_ROOM_OMNIA_ARCS_URL, _CALC_URL, _NEAREST_URL, _NEWID_URL, _BUILD_URL).
"""
import math
import re

LENGTH_TOLERANCE = 0.10        # the reverse route may differ from the forward length by this share
END_TOLERANCE_M = 30           # an end arc must start/finish within this many metres of the forward path's end
CANDIDATES = 3                 # nearest opposite-direction arcs tried at each end
NAME_MAX = 100
CREATE_LOG_HEAD = ["time", "forward_id", "new_id", "name", "end_arcs", "built", "arcs_match", "speeds_nodes_set", "note"]
_NAME_RE = re.compile(r"^\s*(\S+)\s*->\s*(\S+)\s*$")


READ_KEYS = ("arcs", "calc", "nearest")
CREATE_KEYS = ("newid", "build")


def derive_urls(omnia):
    """(arcs, calculate, nearest) addresses from the .env settings; None for one that is not set."""
    return tuple(omnia.get(k) or None for k in READ_KEYS)


def derive_create_urls(omnia):
    """(new id, build) addresses from the .env settings; None for one that is not set."""
    return tuple(omnia.get(k) or None for k in CREATE_KEYS)


def arrow(html):
    """The direction code of an arc (0 east, counter-clockwise in 45 degree steps: 2 north, 4 west, 6 south), or None."""
    m = re.search(r"arrow(\d)_", html or "")
    return int(m.group(1)) % 8 if m else None


def opposite(code):
    return None if code is None else (code + 4) % 8


def metres(lat1, lon1, lat2, lon2):
    return math.hypot((lat2 - lat1) * 111320, (lon2 - lon1) * 111320 * math.cos(math.radians(lat1)))


def _rows(resp, label, no_rows_ok=False):
    if resp.status_code != 200:
        raise ValueError("OMNIA refused the %s request (HTTP %s)." % (label, resp.status_code))
    try:
        rows = resp.json().get("data")
    except Exception:
        rows = None
    if not isinstance(rows, list):
        if no_rows_ok:
            return []                       # OMNIA answers without a route list when no route exists
        raise ValueError("OMNIA's answer to the %s request had an unexpected shape." % label)
    return [{"id": int(r["arcId"]), "length": r.get("arcLength") or 0, "street": r.get("arcStreetName") or "",
             "dir": arrow(r.get("arcDirection")), "lat": r.get("arcLat"), "lon": r.get("arcLon"), "order": r.get("arcOrder", 0)} for r in rows]


def _call(fn, label, *args, **kw):
    try:
        return fn(*args, **kw)
    except Exception as exc:
        raise ValueError("OMNIA did not answer the %s request (%s)." % (label, type(exc).__name__)) from None


def forward_arcs(path_id, arcs_url, post):
    """The arc chain of a path, in order."""
    resp = _call(post, "arcs of path %s" % path_id, arcs_url, params={"iPathId": str(path_id)}, timeout=60)
    return sorted(_rows(resp, "arcs of path %s" % path_id), key=lambda a: a["order"])


def calculate(points, calc_url, post):
    """OMNIA's own calculation of the chain through these arcs (saves nothing)."""
    resp = _call(post, "route calculation", calc_url, data={"listTimingPoints": [str(p) for p in points]}, timeout=60)
    return _rows(resp, "route calculation", no_rows_ok=True)


def nearest_arcs(lat, lon, nearest_url, get):
    resp = _call(get, "nearest arcs", nearest_url, params={"x": lon, "y": lat, "page": 1, "start": 0, "limit": 50}, timeout=60)
    return _rows(resp, "nearest arcs")


_BEARING = {0: (1, 0), 1: (.7071, .7071), 2: (0, 1), 3: (-.7071, .7071), 4: (-1, 0), 5: (-.7071, -.7071), 6: (0, -1), 7: (.7071, -.7071)}


def _offset(point, arc):
    """(distance in metres from `point` to the arc's centre, how far along the arc's own direction the centre lies: > 0 means the
    arc starts at the point and runs away from it, < 0 means it arrives at the point)."""
    dy = (arc["lat"] - point[0]) * 111320
    dx = (arc["lon"] - point[1]) * 111320 * math.cos(math.radians(point[0]))
    bx, by = _BEARING[arc["dir"]]
    return math.hypot(dx, dy), dx * bx + dy * by


def end_candidates(point, street, forward_dir, role, nearest_url, get):
    """Arcs near `point` running the opposite way to `forward_dir`, best first.

    role 'begin': the reverse route STARTS here, so the arc must leave the point; 'finish': it must arrive at the point.
    An arc whose far end is at the point has its centre half its length away, so that is what is scored (street mismatch
    costs a little: junction arcs carry other names).
    """
    want = opposite(forward_dir)
    if want is None:
        return []
    scored = []
    for a in nearest_arcs(point[0], point[1], nearest_url, get):
        if a["dir"] != want or a["lat"] is None:
            continue
        dist, along = _offset(point, a)
        if (along > 0) != (role == "begin"):
            continue
        score = abs(dist - a["length"] / 2) + (0 if not street or a["street"] == street else 10)
        if score <= END_TOLERANCE_M:
            scored.append((score, a))
    return [a for _s, a in sorted(scored, key=lambda t: t[0])[:CANDIDATES]]


def forward_end_points(fwd):
    """Start and end of a path estimated from its first and last arc (centre moved half a length against/along its direction).
    Only used when the drawn route is not available; the 45 degree direction codes make it approximate."""
    def moved(arc, sign):
        bx, by = _BEARING.get(arc["dir"], (0, 0))
        half = arc["length"] / 2 * sign
        return (arc["lat"] + by * half / 111320, arc["lon"] + bx * half / (111320 * math.cos(math.radians(arc["lat"]))))
    return moved(fwd[0], -1), moved(fwd[-1], +1)


def check_reverse(forward, calculated, start_pt, end_pt):
    """Problems that make a reverse route unsafe to create; an empty list means it can be created."""
    if not calculated:
        return ["OMNIA found no route between those two arcs"]
    problems = []
    f_len, r_len = sum(a["length"] for a in forward), sum(a["length"] for a in calculated)
    if f_len and abs(r_len - f_len) / f_len > LENGTH_TOLERANCE:
        problems.append("the reverse route is %d m, the forward one %d m" % (r_len, f_len))
    first, last = calculated[0], calculated[-1]
    for arc, point, role, text in ((first, end_pt, "begin", "start where the forward path ends"), (last, start_pt, "finish", "end where the forward path starts")):
        if arc["lat"] is None or arc["dir"] is None:
            continue
        dist, along = _offset(point, arc)
        if (along > 0) != (role == "begin") or abs(dist - arc["length"] / 2) > END_TOLERANCE_M:
            problems.append("the reverse route does not " + text)
    return problems


def plan_reverse(path_id, name, urls, post, get, ends=None):
    """What the reverse of one path would be, checked against OMNIA's own calculation. Never writes.

    urls = (arcs, calculate, nearest); ends = ((lat, lon) of the forward start, (lat, lon) of its end) from the drawn route;
    without it the centres of the first and last arc are used. Returns {id, name, ok, problems, forward, reverse,
    timing_points: [start arc, end arc], streets}.
    """
    arcs_url, calc_url, nearest_url = urls
    fwd = forward_arcs(path_id, arcs_url, post)
    if not fwd:
        raise ValueError("OMNIA has no arcs for path %s (is it drawn?)." % path_id)
    first, last = fwd[0], fwd[-1]
    approx = forward_end_points(fwd) if first["dir"] is not None and last["dir"] is not None else (None, None)
    start_pt = tuple(ends[0]) if ends and ends[0] else approx[0]
    end_pt = tuple(ends[1]) if ends and ends[1] else approx[1]
    if start_pt is None or end_pt is None:
        raise ValueError("path %s has no usable direction, so its ends cannot be placed." % path_id)
    out = {"id": str(path_id), "name": name, "ok": False, "problems": [], "timing_points": [],
           "forward": {"arcs": len(fwd), "length": sum(a["length"] for a in fwd)}, "reverse": {"arcs": 0, "length": 0},
           "streets": list(dict.fromkeys(a["street"] for a in fwd if a["street"]))}
    begin = end_candidates(end_pt, last["street"], last["dir"], "begin", nearest_url, get)       # the reverse starts where the forward ends
    finish = end_candidates(start_pt, first["street"], first["dir"], "finish", nearest_url, get)  # and ends where the forward starts
    if not begin or not finish:
        out["problems"] = ["no arc running the opposite way was found near the %s of the path (the street may be one-way there)" % ("end" if not begin else "start")]
        return out
    best = None
    for b in begin:
        for f in finish:
            if b["id"] == f["id"]:
                continue
            calc = calculate([b["id"], f["id"]], calc_url, post)
            problems = check_reverse(fwd, calc, start_pt, end_pt)
            if best is None or len(problems) < len(best[0]):
                best = (problems, calc, [b["id"], f["id"]])
            if not problems:
                break
        if best and not best[0]:
            break
    problems, calc, points = best
    out.update(ok=not problems, problems=problems, timing_points=points, route=[a["id"] for a in calc],
               reverse={"arcs": len(calc), "length": sum(a["length"] for a in calc)})
    return out


# -- creating (WRITES to OMNIA) ------------------------------------------------

def reverse_name(name, path_id):
    """'7052->7053' -> '7053->7052'; any other name gets ' reverse' added; an empty one becomes 'Reverse of <id>'."""
    name = (name or "").strip()
    m = _NAME_RE.match(name)
    if m:
        return "%s->%s" % (m.group(2), m.group(1))
    return (name + " reverse")[:NAME_MAX] if name else "Reverse of %s" % path_id


def duplicate_of(forward, paths, new_name):
    """An existing path that already looks like this reverse: same two nodes swapped, or already carrying the reverse name."""
    fs, fe = forward.get("start"), forward.get("end")
    for p in paths:
        if str(p["id"]) == str(forward["id"]):
            continue
        if fs and fe and p.get("start") == fe and p.get("end") == fs:
            return "path %s already runs between the same two nodes the other way" % p["id"]
        if new_name and _NAME_RE.match(new_name) and (p.get("name") or "").strip() == new_name:
            return "path %s is already called %s" % (p["id"], new_name)
    return None


def duplicate_by_route(path_id, ends, feed, tolerance_m=END_TOLERANCE_M):
    """An existing drawn path that starts where this one ends and ends where it starts (catches reverses without nodes)."""
    if not ends or not ends[0] or not ends[1]:
        return None
    (s_lat, s_lon), (e_lat, e_lon) = ends
    for pid, p in (feed or {}).items():
        c = (p or {}).get("coords") or []
        if str(pid) == str(path_id) or len(c) < 2:
            continue
        if metres(e_lat, e_lon, c[0][0], c[0][1]) <= tolerance_m and metres(s_lat, s_lon, c[-1][0], c[-1][1]) <= tolerance_m:
            return "path %s is already drawn the other way between the same two ends" % pid
    return None


def parse_new_id(resp):
    """The id in the new-id call's answer (a bare number, a JSON number, or {data|id|pathId: n}); ValueError if there is none."""
    if resp.status_code != 200:
        raise ValueError("OMNIA refused to hand out a new path id (HTTP %s)." % resp.status_code)
    try:
        body = resp.json()
    except Exception:
        body = (resp.text or "").strip().strip('"')
    if isinstance(body, dict):
        body = next((body[k] for k in ("data", "id", "pathId", "newPathId") if k in body), None)
    try:
        new_id = int(str(body).strip())
    except (TypeError, ValueError):
        raise ValueError("OMNIA's answer to the new path id request was not a number.") from None
    if new_id <= 0:
        raise ValueError("OMNIA handed out an impossible path id (%s)." % new_id)
    return new_id


def _log_create(path, row, now=None):
    import csv
    from datetime import datetime, timezone
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CREATE_LOG_HEAD)
        w.writerow([(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")] + [row.get(k, "") for k in CREATE_LOG_HEAD[1:]])


def create_reverse(item, forward, existing_ids, urls, edit_url, post, get, log_path, now=None, clock=None):
    """Create one reverse path in OMNIA. WRITES. Returns {forward_id, new_id, name, built, arcs_match, speeds_nodes_set, note, error}.

    item     = a passing plan_reverse result (timing_points, route) plus new_name
    forward  = the forward path from a fresh OMNIA read (speeds and nodes are copied, nodes swapped)
    urls     = (new id, build, arcs) addresses; edit_url = the existing edit address (None skips speeds/nodes)
    Order: new id (refused if OMNIA already has a path with it, so nothing is ever overwritten) -> build -> read the new path's
    arcs back and compare with the previewed route -> speeds and nodes. Stops at the first failure; every attempt is logged.
    """
    import time
    newid_url, build_url, arcs_url = urls
    out = {"forward_id": str(item["id"]), "new_id": None, "name": item["new_name"], "built": False, "arcs_match": False,
           "speeds_nodes_set": False, "note": "", "error": None, "end_arcs": " ".join(str(a) for a in item["timing_points"])}
    try:
        stamp = int((clock or time.time)() * 1000)
        new_id = parse_new_id(_call(get, "new path id", newid_url, params={"_dc": stamp}, timeout=60))
        if str(new_id) in existing_ids:
            raise ValueError("OMNIA handed out id %s, which an existing path already has; nothing was created." % new_id)
        out["new_id"] = new_id
        resp = _call(post, "build path", build_url, data={"listPathArcs": [str(a) for a in item["timing_points"]],
                                                          "pathName": item["new_name"], "pathId": str(new_id)}, timeout=60)
        if resp.status_code != 200:
            raise ValueError("OMNIA refused to build path %s (HTTP %s); nothing else was sent." % (new_id, resp.status_code))
        out["built"] = True
        got = [a["id"] for a in forward_arcs(new_id, arcs_url, post)]
        out["arcs_match"] = got == list(item["route"])
        if not out["arcs_match"]:
            raise ValueError("Path %s was created but its arcs differ from the preview (%d arcs instead of %d): check it in OMNIA "
                             "and delete it if it is wrong." % (new_id, len(got), len(item["route"])))
        values = {k: forward.get(k) for k in ("speed_min", "speed_max", "start", "end")}
        if not edit_url:
            out["note"] = "speeds and nodes not set: the edit address is not set on this PC"
        elif not all(values.values()):
            out["note"] = "speeds and nodes not set: the forward path has none to copy (use Bulk edit)"
        else:
            params = {"pathId": str(new_id), "pathName": item["new_name"], "speedMin": values["speed_min"], "speedMax": values["speed_max"],
                      "idLogicalNodeStart": values["end"], "idLogicalNodeEnd": values["start"]}
            resp = _call(post, "edit path", edit_url, params=params, timeout=60)
            if resp.status_code == 200 and str(resp.text).strip().lower() == "true":
                out["speeds_nodes_set"] = True
            else:
                raise ValueError("Path %s was created, but OMNIA refused its speeds and nodes (HTTP %s); set them with Bulk edit."
                                 % (new_id, resp.status_code))
    except ValueError as exc:
        out["error"] = str(exc)
    _log_create(log_path, dict(out, note=out["note"] or out["error"] or ""), now)
    return out
