"""
route_times.py - add the live travel times of Bluetooth paths along a route (the Control Room "Routes" tab).

Pure functions, no network. The network is the saved OMNIA snapshot (each path runs from a start node to an end node) plus
the drawn path geometry; the times are the live feed's travel time per path. Two questions:

    chain_total(network, [path ids], times)       the paths you picked, added up (and a note where two do not join)
    fastest(network, times, from_site, to_site)   the quickest chain of paths between two nodes, plus alternatives

Only live readings count: a path with no reading is never used for a fastest route and is called out in a chain, so a
total is never quietly short. Nodes with the same name within 100 m are one junction ("site"), because OMNIA often holds
two look-alike nodes for one sensor and a route would otherwise break there.
"""
import heapq
import math

SAME_SITE_M = 100          # look-alike nodes this close with the same name are one junction
ALTERNATIVES = 3           # the fastest route plus up to this many different ones


def haversine_m(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _fold(text):
    return " ".join((text or "").split()).lower()


def node_sites(nodes, same_m=SAME_SITE_M):
    """{node id: site id} - nodes with the same name close together share a site (the lowest node id among them)."""
    parent = {n["id"]: n["id"] for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    by_name = {}
    for n in nodes:
        by_name.setdefault(_fold(n.get("name")), []).append(n)
    for group in by_name.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) <= same_m:
                    ra, rb = find(a["id"]), find(b["id"])
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    return {nid: find(nid) for nid in parent}


def _length_m(coords):
    return sum(haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def build_network(snapshot, feed_paths):
    """{sites: {site: {id, name, lat, lon}}, paths: {path id (text): {id, name, a, b, length_m, coords}}}.

    snapshot: the saved OMNIA snapshot (nodes and paths with start/end node). feed_paths: {path id: {name, coords}} of the
    drawn (active) paths. A path is part of the network when it is drawn and both its nodes are known."""
    nodes = {n["id"]: n for n in (snapshot or {}).get("nodes", [])}
    site_of = node_sites(list(nodes.values())) if nodes else {}
    feed = {str(k): v for k, v in (feed_paths or {}).items()}
    paths, used, unlinked = {}, set(), []
    for p in (snapshot or {}).get("paths", []):
        pid = str(p["id"])
        if pid not in feed:
            continue
        if p.get("start") not in nodes or p.get("end") not in nodes:
            unlinked.append(pid)                 # drawn, but no start or end node yet: cannot be joined to anything
            continue
        coords = feed[pid].get("coords") or []
        km = p.get("length_km")
        length = km * 1000 if isinstance(km, (int, float)) and km > 0 else _length_m(coords)
        a, b = site_of[p["start"]], site_of[p["end"]]
        paths[pid] = {"id": pid, "name": p.get("name") or feed[pid].get("name") or pid, "a": a, "b": b,
                      "length_m": round(length), "coords": [[round(c[0], 5), round(c[1], 5)] for c in coords]}
        used.update((a, b))
    sites = {s: {"id": s, "name": nodes[s]["name"], "lat": nodes[s]["lat"], "lon": nodes[s]["lon"]} for s in sorted(used)}
    return {"sites": sites, "paths": paths, "unlinked": unlinked}


def live_times(live):
    """({path id: {seconds, kmh}}, {ids with no usable reading}, measured text) from the live feed's JSON.

    A reading counts when the speed is not negative (the feed uses -1 for malfunctioning) and the travel time is positive."""
    times, silent = {}, set()
    for p in (live or {}).get("paths", []):
        pid = str(p.get("id"))
        tt, sp = p.get("travel_time_s"), p.get("speed_kmh")
        if isinstance(tt, (int, float)) and tt > 0 and isinstance(sp, (int, float)) and sp >= 0:
            times[pid] = {"seconds": float(tt), "kmh": float(sp)}
        else:
            silent.add(pid)
    return times, silent, (live or {}).get("common_measurement_timestamp") or (live or {}).get("generated_at")


def _leg(network, pid, times):
    p = network["paths"][pid]
    t = times.get(pid)
    return {"id": pid, "name": p["name"], "a": p["a"], "b": p["b"], "length_m": p["length_m"],
            "seconds": t["seconds"] if t else None, "kmh": t["kmh"] if t else None}


def _site_name(network, site):
    s = network["sites"].get(site)
    return s["name"] if s else str(site)


def summarise(network, ids, times):
    legs = [_leg(network, pid, times) for pid in ids]
    known = [l["seconds"] for l in legs if l["seconds"] is not None]
    total = sum(known) if known else None
    length = sum(l["length_m"] for l in legs)
    return {"legs": legs, "seconds": total, "length_m": length,
            "kmh": round(length / 1000 / (total / 3600), 1) if total and len(known) == len(legs) else None,
            "missing": [l["id"] for l in legs if l["seconds"] is None]}


def chain_total(network, ids, times):
    """The picked paths added up: {legs, seconds, length_m, kmh, missing, problems, complete}.

    problems: where a path does not start at the node the one before it ended at, or an id that is not drawn/known."""
    problems, ok = [], []
    for pid in ids:
        pid = str(pid)
        if pid in network["paths"]:
            ok.append(pid)
        else:
            problems.append("Path %s is not in the network (not drawn, or its nodes are not in the saved OMNIA data)." % pid)
    out = summarise(network, ok, times)
    for prev, nxt in zip(out["legs"], out["legs"][1:]):
        if prev["b"] != nxt["a"]:
            problems.append("Path %s ends at %s but path %s starts at %s: they do not join." %
                            (prev["id"], _site_name(network, prev["b"]), nxt["id"], _site_name(network, nxt["a"])))
    for pid in out["missing"]:
        problems.append("Path %s has no live reading, so the total leaves it out." % pid)
    out["problems"] = problems
    out["complete"] = bool(ok) and not problems
    return out


def _dijkstra(network, weight, start, goal, banned=()):
    """Cheapest list of path ids from start to goal, or None. weight(pid) is None for a path that may not be used."""
    out = {}
    for pid, p in network["paths"].items():
        out.setdefault(p["a"], []).append(pid)
    best, queue = {start: 0.0}, [(0.0, start, [])]
    done = set()
    while queue:
        cost, site, route = heapq.heappop(queue)
        if site in done:
            continue
        done.add(site)
        if site == goal:
            return route
        for pid in out.get(site, ()):
            w = weight(pid)
            if w is None or pid in banned:
                continue
            nxt = network["paths"][pid]["b"]
            c = cost + w
            if nxt not in done and c < best.get(nxt, math.inf):
                best[nxt] = c
                heapq.heappush(queue, (c, nxt, route + [pid]))
    return None


def fastest(network, times, start, goal, alternatives=ALTERNATIVES):
    """{routes: [summarise(...)], note}: the quickest chain of live-reading paths from one site to another, then different
    ones (each found by forbidding one path of the best route). When none exists but a route does through paths with no
    reading, `note` says which paths are the gap."""
    if start not in network["sites"] or goal not in network["sites"]:
        return {"routes": [], "note": "Pick two nodes from the list."}
    if start == goal:
        return {"routes": [], "note": "The two nodes are the same place."}

    def live_weight(pid):
        t = times.get(pid)
        return t["seconds"] if t else None
    best = _dijkstra(network, live_weight, start, goal)
    if best is None:
        def any_weight(pid):                    # a path with no reading costs far more, so the route uses as few as it can
            t = times.get(pid)
            return t["seconds"] if t else 100000.0
        gap = _dijkstra(network, any_weight, start, goal)
        if gap is None:
            return {"routes": [], "note": "No chain of paths joins these two nodes."}
        missing = [p for p in gap if p not in times]
        return {"routes": [], "gap": summarise(network, gap, times),
                "note": "A route exists, but path %s has no live reading, so it cannot be timed." % ", ".join(missing)}
    routes = [best]
    seen = {tuple(best)}
    for pid in best:
        if len(routes) > alternatives:
            break
        alt = _dijkstra(network, live_weight, start, goal, banned={pid})
        if alt and tuple(alt) not in seen:
            seen.add(tuple(alt))
            routes.append(alt)
    out = [summarise(network, r, times) for r in routes]
    out.sort(key=lambda r: r["seconds"])
    return {"routes": out, "note": ""}


def next_paths(network, site, times):
    """Paths that start at a site, for 'continue with ...': [{id, name, seconds}] by id."""
    out = [_leg(network, pid, times) for pid, p in network["paths"].items() if p["a"] == site]
    return sorted(out, key=lambda l: int(l["id"]) if l["id"].isdigit() else 0)


# ── gaps: nearby nodes the paths do not join sensibly ────────────────────────

GAP_RADIUS_M = 3000        # only nodes this close in a straight line are compared
GAP_MIN_RATIO = 3.0        # a chain this many times longer than the straight line counts as a detour
GAP_MAX_ROWS = 600
GAP_SAME_PLACE_M = 150      # nodes this close are one place: a replacement sensor beside the old one


def _all_from(network, start, out):
    """Shortest chain length (metres) from one site to every site it can reach, and the path used to get into each."""
    dist, prev, done, queue = {start: 0.0}, {}, set(), [(0.0, start)]
    while queue:
        d, site = heapq.heappop(queue)
        if site in done:
            continue
        done.add(site)
        for pid in out.get(site, ()):
            p = network["paths"][pid]
            nd = d + p["length_m"]
            if p["b"] not in done and nd < dist.get(p["b"], math.inf):
                dist[p["b"]] = nd
                prev[p["b"]] = pid
                heapq.heappush(queue, (nd, p["b"]))
    return dist, prev


def _chain(network, prev, start, goal):
    legs, site = [], goal
    while site != start:
        pid = prev[site]
        legs.append(pid)
        site = network["paths"][pid]["a"]
    return legs[::-1]


def _places(network, same_m):
    """{site: place id} - sites within same_m of each other are one place (a replacement sensor put up beside an old one)."""
    sites = network["sites"]
    ids = sorted(sites)
    parent = {s: s for s in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if haversine_m((sites[a]["lat"], sites[a]["lon"]), (sites[b]["lat"], sites[b]["lon"])) <= same_m:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
    return {s: find(s) for s in ids}


def find_gaps(network, radius_m=GAP_RADIUS_M, min_ratio=GAP_MIN_RATIO, limit=GAP_MAX_ROWS, same_m=GAP_SAME_PLACE_M):
    """Pairs of places close together whose shortest chain of paths is far longer than the straight line (or does not exist).

    Nodes within same_m of each other are one place: a sensor added next to an old one is a replacement, not a neighbour,
    so such a pair is never listed and a chain may pass from one to the other. Lengths only: live readings do not matter,
    a path with no reading still joins two places. Each pair is judged in both directions and reported by its worse one.
    Returns {rows, total, none, detour}; rows are worst first: no chain at all (closest pairs first), then by how many times
    longer the chain is. Row: {a, b (place ids: the lowest node id of each; the worse direction runs a to b), a_names,
    b_names, straight_m, chain_m (None = no chain), ratio (None = no chain), legs [path ids], back: {chain_m, ratio}}."""
    sites = network["sites"]
    place = _places(network, same_m)
    members = {}
    for s, pl in place.items():
        members.setdefault(pl, []).append(s)
    paths = {}
    for pid, p in network["paths"].items():
        pa, pb = place[p["a"]], place[p["b"]]
        if pa != pb:                                  # a path between two sensors of one place joins nothing new
            paths[pid] = dict(p, a=pa, b=pb)
    net2 = {"sites": {pl: sites[pl] for pl in members}, "paths": paths}
    out = {}
    for pid, p in paths.items():
        out.setdefault(p["a"], []).append(pid)
    ids = sorted(members)
    runs = {}

    def run(s):
        if s not in runs:
            runs[s] = _all_from(net2, s, out)
        return runs[s]

    def apart(x, y):
        return min(haversine_m((sites[i]["lat"], sites[i]["lon"]), (sites[j]["lat"], sites[j]["lon"]))
                   for i in members[x] for j in members[y])

    rows = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            straight = apart(a, b)
            if straight > radius_m:
                continue
            base = max(straight, 50.0)
            res = {}
            for x, y in ((a, b), (b, a)):
                d = run(x)[0].get(y)
                res[(x, y)] = {"chain_m": None if d is None else round(d), "ratio": None if d is None else round(d / base, 2)}
            bad = [k for k, v in res.items() if v["ratio"] is None or v["ratio"] >= min_ratio]
            if not bad:
                continue
            worst = sorted(bad, key=lambda k: (res[k]["ratio"] is not None, -(res[k]["ratio"] or 0)))[0]
            back = res[(worst[1], worst[0])]
            rows.append({"a": worst[0], "b": worst[1], "straight_m": round(straight), "chain_m": res[worst]["chain_m"],
                         "a_names": [sites[m]["name"] for m in members[worst[0]]], "b_names": [sites[m]["name"] for m in members[worst[1]]],
                         "ratio": res[worst]["ratio"],
                         "legs": [] if res[worst]["chain_m"] is None else _chain(net2, run(worst[0])[1], worst[0], worst[1]),
                         "back": back})
    rows.sort(key=lambda r: (r["ratio"] is not None, r["straight_m"] if r["ratio"] is None else -r["ratio"]))
    none = sum(1 for r in rows if r["ratio"] is None)
    return {"rows": rows[:limit], "total": len(rows), "none": none, "detour": len(rows) - none}
