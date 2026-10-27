"""
bt_wiring.py - is each Bluetooth path really wired to the sensors it is drawn between? Read-only.

In OMNIA every path has a start and an end *logical node* (the OMNIA copy of a BT sensor location), chosen by
hand when the path is created. A path only gets data from the sensors behind its two nodes, so a wrong or
missing node gives a path that is drawn between A and B but reads from C - or from nothing.

Two checks, both pure functions over the OMNIA snapshot (omnia_fetch.py), the API sensors and the drawn paths:

1. match_nodes: is each node a real, correctly named sensor? The name must equal the API sensor's name
   EXACTLY (case-sensitive: some sensors differ only in case). When several sensors share a name (they do),
   the one at the node's location is meant.
2. check_paths: for each path, are its configured start/end nodes where the path is drawn to start/end?

Thresholds come from the real data (node vs same-named sensor: median 0.2 m, so the 10 nodes farther than
15 m are real drifts; drawn path ends: median 40 m from their node, 80 % within 100 m, 7 % beyond 250 m).
"""

import re
from collections import defaultdict

import bt_sensor_suspects as bs

SAME_PLACE_M = 15      # a node and its sensor are "the same place" within this
END_OK_M = 150         # a drawn path end this close to its configured node is fine (road snapping)
CLOSE_PAIR_M = 60      # two sensors this close cannot be told apart from where a path ends
END_FAR_M = 500        # farther than this is a clear error, between END_OK_M and this only "check"

SEVERITY = {"high": 3, "medium": 2, "low": 1}

NODE_STATUS = {
    "ok": "Name and location match a sensor",
    "name_only": "Name matches a sensor, but it is somewhere else",
    "location_only": "A sensor is here, but with a different name",
    "case_or_space": "Name differs from a sensor only by capitals or spaces",
    "no_sensor": "No sensor with this name or at this place",
}

FINDINGS = {
    "no_node": ("high", "Start or end node not set"),
    "node_without_sensor": ("high", "A node has no matching sensor"),
    "wrong_node": ("high", "Wired to a different sensor than it is drawn to"),
    "swapped": ("high", "Start and end nodes are the wrong way round"),
    "same_node": ("high", "Start and end node are the same"),
    "end_far": ("high", "Drawn end is far from its node"),
    "end_off": ("medium", "Drawn end is a little off its node"),
    "name_mismatch": ("medium", "Path name disagrees with its nodes"),
    "close_pair": ("low", "Ends next to a second sensor: check by hand"),
    "speed_range": ("low", "Speed range missing or not min < max"),
    "not_in_omnia": ("medium", "Drawn path not found in OMNIA"),
    "not_in_feed": ("medium", "OMNIA path not in the API feed"),
}

_NAME_RE = re.compile(r"^\s*(\d+)\s*->\s*(\d+)\s*$")     # site codes only, not descriptive names


def _fold(text):
    return " ".join((text or "").split()).lower()


def _dist(a, b):
    return bs._haversine_m(a[0], a[1], b[0], b[1])


# ── 1. nodes <-> sensors ─────────────────────────────────────────────────────

def match_nodes(nodes, sensors):
    """{node id: {id, name, lat, lon, status, sensor, sensor_name, distance_m, similar, notes}}.

    sensors: {sensor id: {name, site_code, lat, lon}}. status: see NODE_STATUS. `sensor` is the sensor the
    node stands for (None when unknown or only a capitals/spaces look-alike exists).
    """
    by_name, by_fold = defaultdict(list), defaultdict(list)
    for sid, s in sensors.items():
        by_name[s.get("name") or ""].append(sid)
        by_fold[_fold(s.get("name"))].append(sid)
    out = {}
    for n in nodes:
        here = (n["lat"], n["lon"])
        m = {"id": n["id"], "name": n["name"], "lat": n["lat"], "lon": n["lon"], "status": "no_sensor",
             "sensor": None, "sensor_name": None, "distance_m": None, "similar": [], "notes": []}
        out[n["id"]] = m
        exact = sorted((_dist(here, (sensors[s]["lat"], sensors[s]["lon"])), s) for s in by_name.get(n["name"], []))
        if exact:
            d, sid = exact[0]
            m.update(status="ok" if d <= SAME_PLACE_M else "name_only", sensor=sid,
                     sensor_name=sensors[sid]["name"], distance_m=round(d, 1))
            if len(exact) > 1:
                m["notes"].append("%d sensors share this name; the one at the node was taken" % len(exact))
            continue
        similar = by_fold.get(_fold(n["name"]), [])
        if similar:
            m.update(status="case_or_space", similar=[sensors[s]["name"] for s in similar])
            continue
        near = sorted((_dist(here, (s["lat"], s["lon"])), sid) for sid, s in sensors.items()
                      if s.get("lat") is not None)
        if near and near[0][0] <= SAME_PLACE_M:
            d, sid = near[0]
            m.update(status="location_only", sensor=sid, sensor_name=sensors[sid]["name"], distance_m=round(d, 1))
    used = defaultdict(list)
    for m in out.values():
        if m["sensor"] is not None:
            used[m["sensor"]].append(m["id"])
    for sid, ids in used.items():
        if len(ids) > 1:
            for i in ids:
                out[i]["notes"].append("another node points to the same sensor")
    return out


def sensors_without_node(matches, sensors):
    """API sensors no node stands for: [(sensor id, name)]."""
    used = {m["sensor"] for m in matches.values() if m["sensor"] is not None}
    return [(sid, s.get("name")) for sid, s in sensors.items() if sid not in used]


# ── 2. paths ─────────────────────────────────────────────────────────────────

def _site(sensors, sid):
    return str(sensors[sid].get("site_code")) if sid in sensors else None


def close_neighbours(sensors):
    """{sensor id: (nearest other sensor id, metres)} for sensors with another one within CLOSE_PAIR_M."""
    out = {}
    for a, sa in sensors.items():
        for b, sb in sensors.items():
            if a != b and sa.get("lat") is not None and sb.get("lat") is not None:
                d = _dist((sa["lat"], sa["lon"]), (sb["lat"], sb["lon"]))
                if d <= CLOSE_PAIR_M and (a not in out or d < out[a][1]):
                    out[a] = (b, d)
    return out


def nearest_nodes(point, matches, limit=3, sensors=None, want=None):
    """Nodes within END_FAR_M of a drawn path end, nearest first: [{id, name, distance_m, level, ambiguous, ...}].

    level: 'likely' within END_OK_M of a node that stands for a sensor, otherwise 'check'. The first entry is flagged
    ambiguous when a second node is also within END_OK_M of the end and within CLOSE_PAIR_M of the first one.
    `want` is the site code the path's own name gives for this end (numeric `A->B` names only): a node whose sensor has
    another site code is only a 'check' and carries `name_says` / `sensor_code`.
    """
    ranked = sorted((_dist(point, (m["lat"], m["lon"])), nid) for nid, m in matches.items() if m.get("lat") is not None)
    out = []
    for d, nid in ranked[:limit]:
        if d > END_FAR_M:
            break
        m = matches[nid]
        item = {"id": nid, "name": m["name"], "distance_m": round(d), "sensor_name": m["sensor_name"],
                "level": "likely" if d <= END_OK_M and m["sensor"] is not None else "check", "ambiguous": False}
        code = _site(sensors, m["sensor"]) if sensors and m["sensor"] is not None else None
        if want is not None and code != want:
            item.update(level="check", name_says=want, sensor_code=code)
        out.append(item)
    if len(out) > 1 and out[1]["distance_m"] <= END_OK_M:
        a, b = matches[out[0]["id"]], matches[out[1]["id"]]
        if _dist((a["lat"], a["lon"]), (b["lat"], b["lon"])) <= CLOSE_PAIR_M:
            out[0]["ambiguous"] = True
    return out


def _suggestion_text(label, found):
    if not found:
        return "; no node within %d m of the drawn %s" % (END_FAR_M, label)
    first = found[0]
    text = "; nearest node to the drawn %s: '%s' (id %s), %d m" % (label, first["name"], first["id"], first["distance_m"])
    if first["ambiguous"]:
        text += ", but '%s' (id %s) is just as close: check by hand" % (found[1]["name"], found[1]["id"])
    elif "name_says" in first:
        text += ", but the path name says %s and this node's sensor is %s: check by hand" % (first["name_says"], first["sensor_code"] or "unknown")
    elif first["level"] == "check":
        text += ", check by hand"
    return text


def usable_suggestions(paths):
    """{path id: {'start': node id, 'end': node id}} for the clear suggestions only: a 'likely' node that has no
    close rival, and not the same node for both ends of one path."""
    out = {}
    for pid, p in paths.items():
        pick = {}
        for side, found in (p.get("suggest") or {}).items():
            if found and found[0]["level"] == "likely" and not found[0]["ambiguous"]:
                pick[side] = found[0]["id"]
        if pick.get("start") is not None and pick.get("start") == pick.get("end"):
            pick = {}
        if pick:
            out[str(pid)] = pick
    return out


def check_paths(omnia_paths, matches, feed_paths, sensors):
    """{path id: {..., findings: [{code, severity, text}], severity}} joining OMNIA paths to drawn paths by id."""
    close = close_neighbours(sensors)
    out = {}
    seen = set()
    for p in omnia_paths:
        pid = str(p["id"])
        seen.add(pid)
        feed = feed_paths.get(pid)
        coords = (feed or {}).get("coords") or []
        findings = []

        def add(code, text):
            findings.append({"code": code, "severity": FINDINGS[code][0], "text": text})

        ends = []
        suggest = {}
        named = _NAME_RE.match(p.get("name") or "")       # numeric A->B names say which sensors the path should use
        for label, nid, point in (("start", p.get("start"), coords[0] if coords else None),
                                  ("end", p.get("end"), coords[-1] if coords else None)):
            node = matches.get(nid) if nid else None
            ends.append((label, nid, node, point))
            if node is None:
                if label == "end" and not p.get("start") and not nid:
                    continue            # both missing: reported once, at the start
                both = label == "start" and not nid and not p.get("end")
                text = "Start and end node are not set" if both else \
                    "%s node %s" % (label.capitalize(), "is not set" if not nid else "%s is not a known node" % nid)
                if both:        # the end is not visited again, so look for both ends here
                    for side, pt, k in (("start", coords[0] if coords else None, 1), ("end", coords[-1] if coords else None, 2)):
                        if pt:
                            suggest[side] = nearest_nodes(pt, matches, sensors=sensors, want=named.group(k) if named else None)
                    if suggest:
                        text += "".join(_suggestion_text(s, suggest[s]) for s in ("start", "end") if s in suggest)
                elif point:
                    suggest[label] = nearest_nodes(point, matches, sensors=sensors,
                                                   want=named.group(1 if label == "start" else 2) if named else None)
                    text += _suggestion_text(label, suggest[label])
                add("no_node", text)
            elif node["sensor"] is None:
                hint = " (differs only by capitals/spaces from: %s)" % ", ".join(node["similar"]) if node["similar"] else ""
                add("node_without_sensor", "%s node '%s' has no matching sensor%s" % (label.capitalize(), node["name"], hint))

        (_, sid_, sn, spt), (_, eid_, en, ept) = ends
        if sid_ and sid_ == eid_:
            add("same_node", "Start and end are both node %s ('%s')" % (sid_, sn["name"] if sn else ""))
        elif sn and en and spt and ept:
            ds, de = _dist(spt, (sn["lat"], sn["lon"])), _dist(ept, (en["lat"], en["lon"]))
            xs, xe = _dist(spt, (en["lat"], en["lon"])), _dist(ept, (sn["lat"], sn["lon"]))
            if (ds > END_OK_M or de > END_OK_M) and xs <= END_OK_M and xe <= END_OK_M:
                add("swapped", "The start node '%s' is at the drawn end and the end node '%s' at the drawn start"
                    % (sn["name"], en["name"]))
            else:
                for label, node, point, d in (("start", sn, spt, ds), ("end", en, ept, de)):
                    if d <= END_OK_M:
                        other = close.get(node["sensor"]) if node["sensor"] is not None else None
                        if other is not None:
                            add("close_pair", "The drawn %s is at node '%s', but sensor '%s' is only %d m from that sensor: "
                                "check by hand which of the two the path really uses"
                                % (label, node["name"], sensors[other[0]]["name"], other[1]))
                        continue
                    near, _how = bs.resolve_endpoint(point, sensors)
                    if near is not None and node["sensor"] is not None and near != node["sensor"]:
                        add("wrong_node", "Drawn %s is at '%s' but its node is '%s' (%d m away)"
                            % (label, sensors[near]["name"], node["name"], d))
                    elif d > END_FAR_M:
                        add("end_far", "Drawn %s is %d m from its node '%s'" % (label, d, node["name"]))
                    else:
                        add("end_off", "Drawn %s is %d m from its node '%s'" % (label, d, node["name"]))
        if feed is None:
            add("not_in_feed", "This path is in OMNIA but not in the API feed")

        m = _NAME_RE.match(p.get("name") or "")
        if m and sn and en and sn["sensor"] is not None and en["sensor"] is not None:
            want = (_site(sensors, sn["sensor"]), _site(sensors, en["sensor"]))
            if (m.group(1), m.group(2)) != want:
                add("name_mismatch", "Named %s->%s, but its nodes are sensors %s->%s" % (m.group(1), m.group(2), want[0], want[1]))
        if sn and en:
            lo, hi = p.get("speed_min"), p.get("speed_max")
            if lo is None or hi is None or not lo < hi:
                add("speed_range", "Speed range is %s-%s km/h" % ("none" if lo is None else lo, "none" if hi is None else hi))

        out[pid] = {"id": pid, "name": p.get("name") or "", "feed_name": (feed or {}).get("name"),
                    "start_node": p.get("start"), "end_node": p.get("end"),
                    "start_name": sn["name"] if sn else None, "end_name": en["name"] if en else None,
                    "start_sensor": sn["sensor"] if sn else None, "end_sensor": en["sensor"] if en else None,
                    "speed_min": p.get("speed_min"), "speed_max": p.get("speed_max"),
                    "suggest": suggest, "findings": findings,
                    "severity": max((f["severity"] for f in findings), key=SEVERITY.get, default="ok")}
    for pid, feed in feed_paths.items():
        if str(pid) not in seen:
            out[str(pid)] = {"id": str(pid), "name": feed.get("name") or "", "feed_name": feed.get("name"),
                             "start_node": None, "end_node": None, "start_name": None, "end_name": None,
                             "start_sensor": None, "end_sensor": None, "speed_min": None, "speed_max": None,
                             "findings": [{"code": "not_in_omnia", "severity": "medium",
                                           "text": "This path is in the API feed but not in OMNIA"}],
                             "severity": "medium"}
    return out


def analyse(snapshot, feed_paths, sensors):
    """Everything in one go: {nodes, paths, sensors_without_node, meta}."""
    matches = match_nodes(snapshot["nodes"], sensors)
    paths = check_paths(snapshot["paths"], matches, feed_paths, sensors)
    by_code = defaultdict(int)
    for p in paths.values():
        for f in p["findings"]:
            by_code[f["code"]] += 1
    by_status = defaultdict(int)
    for m in matches.values():
        by_status[m["status"]] += 1
    return {"nodes": matches, "paths": paths,
            "sensors_without_node": sensors_without_node(matches, sensors),
            "meta": {"fetched_at": snapshot.get("fetched_at"), "paths": len(paths),
                     "paths_with_findings": sum(1 for p in paths.values() if p["findings"]),
                     "paths_ok": sum(1 for p in paths.values() if not p["findings"]),
                     "by_code": dict(by_code), "node_status": dict(by_status)}}


def wiring_for_sensor(result, sid):
    """{nodes: [{id, name, status}], paths: n} for one API sensor, for its popup."""
    nodes = [{"id": m["id"], "name": m["name"], "status": m["status"], "distance_m": m["distance_m"]}
             for m in result["nodes"].values() if m["sensor"] == sid]
    n = sum(1 for p in result["paths"].values() if sid in (p["start_sensor"], p["end_sensor"]))
    return {"nodes": nodes, "paths": n}


def csv_rows(result):
    """Rows for an export: paths with findings, worst first."""
    head = ["Path ID", "Path name", "Worst severity", "Findings", "Details", "Start node", "End node"]
    rows = []
    for p in sorted((p for p in result["paths"].values() if p["findings"]),
                    key=lambda p: (-SEVERITY.get(p["severity"], 0), int(p["id"]) if str(p["id"]).isdigit() else 0)):
        rows.append([p["id"], p["name"], p["severity"], ", ".join(f["code"] for f in p["findings"]),
                     " | ".join(f["text"] for f in p["findings"]),
                     "%s %s" % (p["start_node"] or "", p["start_name"] or ""),
                     "%s %s" % (p["end_node"] or "", p["end_name"] or "")])
    return [head] + rows
