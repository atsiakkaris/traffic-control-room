#!/usr/bin/env python3
"""
bt_direction_check.py - ad-hoc check for NON-REVERSED Bluetooth path pairs.

SWARCO BT paths come in directional pairs ("A->B" and "B->A") and each pair
should be stored in OPPOSITE point order. A SWARCO defect (BuildPathArcs) once
saved the reverse-registered path in the same direction as its partner. This
tool finds pairs purely from GEOMETRY (names are only a label afterwards) and
reports whether that problem has come back. It is read-only.

Usage:
    python runner/bt_direction_check.py                  # live API geometry
    python runner/bt_direction_check.py --db             # results/history.db
    python runner/bt_direction_check.py --snapshot HEAD  # published map at a git rev
    python runner/bt_direction_check.py --snapshot 879419b --save

Snapshot runs are for replay/testing: they compare against the last saved
state but do not overwrite the state file or CSV unless --save is given.
"""

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("DB_PATH", str(ROOT / "results" / "history.db"))

# Load .env from repo root if present (same convention as bt_paths_map.py)
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ---- Tunables (calibrated on real data) --------------------------------------
LAT0_DEG          = 35      # equirectangular projection centre latitude
RESAMPLE_M        = 25      # resample every path at this spacing
GRID_CELL_M       = 100     # spatial grid cell size
CORRIDOR_M        = 100     # a point is "covered" if the other path is this close
COVERAGE_MIN      = 0.8     # candidate (a): min coverage of both paths
LENGTH_RATIO_MIN  = 0.8     # candidate (a): shorter/longer length
ENDPOINT_PAIR_M   = 400     # candidate (b): best start/end distance sum
DIRECTION_R       = 0.8     # |Pearson r| at or above this decides the direction
MIN_MATCHED       = 6       # fewer matched points -> fall back to endpoints
DUP_ENDPOINT_M    = 50      # hint: both endpoints this close = likely duplicate
WRONG_MEMBER_GAIN = 100     # metres the swapped endpoint fit must win by

STATE_PATH = ROOT / "results" / "bt_direction_last.json"
CSV_PATH   = ROOT / "reports" / "bt_direction_check.csv"
CONFIG_PATH = ROOT / "config" / "endpoints.yaml"
LIVE_TIMEOUT_S = 60
USER_AGENT = "traffic-control-room/bt_direction_check (read-only)"

NAME_RE = re.compile(r"^\s*(\S+)\s*->\s*(\S+)\s*$")

B_NON_REVERSED = "1-non-reversed"
B_SAME_UNEXPL  = "2-same-direction-unexplained"
B_HEALTHY      = "3-healthy"
B_UNCLEAR      = "4-unclear"

CSV_COLUMNS = ["path_a", "name_a", "path_b", "name_b", "bucket", "direction", "r",
               "cov_a", "cov_b", "length_ratio", "endpoint_sum_m", "hint"]


# ---- Geometry ----------------------------------------------------------------
_R = 6371000.0
_COS0 = math.cos(math.radians(LAT0_DEG))


def project(coords):
    """[[lat,lon],...] -> [(x_m, y_m),...] (equirectangular around LAT0_DEG)."""
    return [(math.radians(lon) * _R * _COS0, math.radians(lat) * _R) for lat, lon in coords]


def polyline_length(pts):
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


def resample(pts, step=RESAMPLE_M):
    """Points every `step` metres along the polyline, always including the last point."""
    out = [pts[0]]
    carry = 0.0  # distance travelled since the last emitted point
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        d = step - carry
        while d <= seg:
            t = d / seg
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
            d += step
        carry = seg - (d - step)
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class PathGeom:
    """Everything the pair test needs about one path, computed once."""

    def __init__(self, pid, name, coords):
        self.pid = pid
        self.name = name or ""
        pts = project(coords)
        self.length = polyline_length(pts)
        self.start, self.end = pts[0], pts[-1]
        self.pts = resample(pts)
        xs = [p[0] for p in self.pts]
        ys = [p[1] for p in self.pts]
        self.bbox = (min(xs), max(xs), min(ys), max(ys))
        self.grid = {}
        for idx, (x, y) in enumerate(self.pts):
            self.grid.setdefault((int(x // GRID_CELL_M), int(y // GRID_CELL_M)), []).append((idx, x, y))


def _bbox_near(a, b, buf):
    return (a[0] - buf <= b[1] and b[0] - buf <= a[1] and
            a[2] - buf <= b[3] and b[2] - buf <= a[3])


def _coverage(p, q, max_uncovered=None):
    """Match each point of p to its nearest point of q within CORRIDOR_M.

    Returns (coverage_fraction, [(i, j), ...]) or None when more than
    `max_uncovered` of p's points are uncovered (early exit for hopeless pairs).
    """
    n = len(p.pts)
    limit = None if max_uncovered is None else int(max_uncovered * n)
    corridor2 = CORRIDOR_M * CORRIDOR_M
    grid = q.grid
    matches, uncovered = [], 0
    for i, (x, y) in enumerate(p.pts):
        cx, cy = int(x // GRID_CELL_M), int(y // GRID_CELL_M)
        best, best_j = corridor2, -1
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for j, qx, qy in grid.get((gx, gy), ()):
                    d2 = (x - qx) ** 2 + (y - qy) ** 2
                    if d2 <= best:
                        best, best_j = d2, j
        if best_j < 0:
            uncovered += 1
            if limit is not None and uncovered > limit:
                return None
        else:
            matches.append((i, best_j))
    return (n - uncovered) / n, matches


def _pearson(pairs):
    n = len(pairs)
    mi = sum(i for i, _ in pairs) / n
    mj = sum(j for _, j in pairs) / n
    sii = sum((i - mi) ** 2 for i, _ in pairs)
    sjj = sum((j - mj) ** 2 for _, j in pairs)
    if sii == 0 or sjj == 0:
        return None
    sij = sum((i - mi) * (j - mj) for i, j in pairs)
    return sij / math.sqrt(sii * sjj)


def endpoint_sums(p, q):
    """(same-order sum, mirrored sum) of start/end distances in metres."""
    same = _dist(p.start, q.start) + _dist(p.end, q.end)
    mirrored = _dist(p.start, q.end) + _dist(p.end, q.start)
    return same, mirrored


def _direction(m_pq, m_qp, same, mirrored):
    """('same'|'opposite'|'unclear', r) from matched point indices.

    Correlates the indices of matched points (using the richer match set); with
    too few matches falls back to which endpoint pairing is closer.
    """
    matches = m_pq if len(m_pq) >= len(m_qp) else [(j, i) for i, j in m_qp]
    r = _pearson(matches) if len(matches) >= MIN_MATCHED else None
    if r is None:
        return ("same" if same < mirrored else "opposite"), None
    if r >= DIRECTION_R:
        return "same", r
    if r <= -DIRECTION_R:
        return "opposite", r
    return "unclear", r


def orientation(coords_a, coords_b):
    """Direction of two [[lat,lon],...] polylines already known to share a road:
    'same', 'opposite' or 'unclear'. Skips the pair-candidate tests, so callers
    that have their own notion of "overlapping" (e.g. the map's duplicate
    detection) can ask just the direction question.
    """
    p, q = PathGeom("a", "", coords_a), PathGeom("b", "", coords_b)
    same, mirrored = endpoint_sums(p, q)
    cp, cq = _coverage(p, q), _coverage(q, p)
    return _direction(cp[1], cq[1], same, mirrored)[0]


def compare_paths(p, q):
    """Geometric pair test. Returns a dict, or None when p and q are not a pair.

    Keys: direction ('same'|'opposite'|'unclear'), r, cov_a, cov_b, length_ratio,
    endpoint_sum_m (best of same/mirrored).
    """
    if not _bbox_near(p.bbox, q.bbox, CORRIDOR_M):
        return None
    same, mirrored = endpoint_sums(p, q)
    ep_best = min(same, mirrored)
    ep_candidate = ep_best <= ENDPOINT_PAIR_M
    longer = max(p.length, q.length)
    length_ratio = (min(p.length, q.length) / longer) if longer > 0 else 0.0

    if ep_candidate:
        cp = _coverage(p, q)
        cq = _coverage(q, p)
    else:
        if length_ratio < LENGTH_RATIO_MIN:
            return None
        slack = 1 - COVERAGE_MIN
        cp = _coverage(p, q, slack)
        if cp is None:
            return None
        cq = _coverage(q, p, slack)
        if cq is None:
            return None
    cov_p, m_pq = cp
    cov_q, m_qp = cq
    cov_candidate = min(cov_p, cov_q) >= COVERAGE_MIN and length_ratio >= LENGTH_RATIO_MIN
    if not (cov_candidate or ep_candidate):
        return None

    direction, r_out = _direction(m_pq, m_qp, same, mirrored)
    return {"direction": direction, "r": r_out, "cov_a": cov_p, "cov_b": cov_q,
            "length_ratio": length_ratio, "endpoint_sum_m": ep_best,
            "same_ends_m": (_dist(p.start, q.start), _dist(p.end, q.end))}


# ---- Naming (labels only, applied after pairing) ------------------------------
def parse_name(name):
    m = NAME_RE.match(name or "")
    return (m.group(1), m.group(2)) if m else None


def is_unnamed(pid, name):
    n = (name or "").strip()
    return n == "" or n == str(pid)


def names_reversed(name_a, name_b):
    a, b = parse_name(name_a), parse_name(name_b)
    return bool(a and b and a[0] == b[1] and a[1] == b[0])


def hint_for(pa, pb, result):
    """Explanation for a same-direction pair that the names do not explain."""
    if is_unnamed(pa.pid, pa.name) or is_unnamed(pb.pid, pb.name):
        return "unnamed"
    if pa.name.strip() == pb.name.strip():
        return "names equal"
    se, ee = result["same_ends_m"]
    if se <= DUP_ENDPOINT_M and ee <= DUP_ENDPOINT_M:
        return "endpoints within 50 m (likely duplicate registration)"
    a, b = parse_name(pa.name), parse_name(pb.name)
    if a and b and set(a) != set(b):
        return "different node ids (likely overlapping paths from two projects)"
    return ""


def wrong_member_hint(pa, pb, nodes):
    """For a non-reversed named pair, say which member's geometry looks wrong.

    nodes: {node_id: (x, y)} from Bluetooth sensor_coords (site_code == node id).
    The correct path starts near its first named node and ends near its second.
    """
    verdicts = {}
    for p in (pa, pb):
        first, second = parse_name(p.name)
        if first not in nodes or second not in nodes:
            return "which member is wrong: unknown (sensor position missing)"
        fit = _dist(p.start, nodes[first]) + _dist(p.end, nodes[second])
        swapped = _dist(p.start, nodes[second]) + _dist(p.end, nodes[first])
        if swapped + WRONG_MEMBER_GAIN < fit:
            verdicts[p.pid] = "wrong"
        elif fit + WRONG_MEMBER_GAIN < swapped:
            verdicts[p.pid] = "ok"
        else:
            verdicts[p.pid] = "?"
    wrong = [pid for pid, v in verdicts.items() if v == "wrong"]
    if len(wrong) == 1 and verdicts[pa.pid if wrong[0] == pb.pid else pb.pid] == "ok":
        p = pa if wrong[0] == pa.pid else pb
        return "likely wrong: %s (%s) starts near its second node" % (p.pid, p.name)
    return "which member is wrong: undetermined"


# ---- Analysis ----------------------------------------------------------------
def analyse(paths, nodes=None):
    """paths: {pid: {name, coords}}. Returns a result dict.

    pairs: list of dicts (path_a, name_a, path_b, name_b, bucket, direction, r,
    cov_a, cov_b, length_ratio, endpoint_sum_m, hint).
    """
    nodes = nodes or {}
    geoms = {}
    skipped = 0
    for pid, p in paths.items():
        coords = p.get("coords") or []
        if len(coords) < 2:
            skipped += 1
            continue
        geoms[pid] = PathGeom(pid, p.get("name"), coords)

    ids = sorted(geoms, key=_id_key)
    pairs = []
    for ai, a_id in enumerate(ids):
        for b_id in ids[ai + 1:]:
            pa, pb = geoms[a_id], geoms[b_id]
            res = compare_paths(pa, pb)
            if res is None:
                continue
            if res["direction"] == "opposite":
                bucket, hint = B_HEALTHY, ""
            elif res["direction"] == "unclear":
                bucket, hint = B_UNCLEAR, ""
            elif names_reversed(pa.name, pb.name):
                bucket, hint = B_NON_REVERSED, wrong_member_hint(pa, pb, nodes)
            else:
                bucket, hint = B_SAME_UNEXPL, hint_for(pa, pb, res)
            pairs.append({
                "path_a": a_id, "name_a": pa.name, "path_b": b_id, "name_b": pb.name,
                "bucket": bucket, "direction": res["direction"], "r": res["r"],
                "cov_a": res["cov_a"], "cov_b": res["cov_b"],
                "length_ratio": res["length_ratio"],
                "endpoint_sum_m": res["endpoint_sum_m"], "hint": hint,
            })

    paired = {x for pr in pairs for x in (pr["path_a"], pr["path_b"])}
    return {"pairs": pairs, "n_paths": len(geoms), "skipped": skipped,
            "no_partner": sorted((i for i in geoms if i not in paired), key=_id_key),
            "named_stats": _named_stats(geoms, pairs)}


def _id_key(pid):
    s = str(pid)
    return (0, int(s), s) if s.isdigit() else (1, 0, s)


def _named_stats(geoms, pairs):
    """Diagnostic only: how the geometric result lines up with A->B / B->A names."""
    by_nodes = {}
    for pid, g in geoms.items():
        n = parse_name(g.name)
        if n:
            by_nodes.setdefault(frozenset(n), []).append((pid, n))
    named_pairs = set()
    for entries in by_nodes.values():
        if len(entries) == 2 and entries[0][1] == entries[1][1][::-1]:
            named_pairs.add(frozenset((entries[0][0], entries[1][0])))
    found = {"same": 0, "opposite": 0, "unclear": 0}
    for pr in pairs:
        if frozenset((pr["path_a"], pr["path_b"])) in named_pairs:
            found[pr["direction"]] += 1
    return {"named_pairs": len(named_pairs), "captured": sum(found.values()), **found}


# ---- Data sources ------------------------------------------------------------
def load_live():
    import httpx
    import yaml
    from geo import extract_bt_path_coords

    base, swarco = os.environ.get("BASE_URL"), os.environ.get("SWARCO")
    if not base or not swarco:
        raise SystemExit("BASE_URL / SWARCO not set (put them in .env or the environment).")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    path = None
    for group in cfg.get("groups", []):
        for ep in group.get("endpoints", []):
            if ep.get("coords_extract") == "bt_paths":
                path = ep["path"]
    if path is None:
        raise SystemExit("No endpoint with coords_extract: bt_paths in config/endpoints.yaml.")
    url = "%s/%s/%s" % (base.rstrip("/"), swarco.strip("/"), path)
    print("Fetching %s (slow feed, timeout %ds) ..." % (path, LIVE_TIMEOUT_S))
    resp = httpx.get(url, timeout=LIVE_TIMEOUT_S, follow_redirects=True,
                     headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    paths = extract_bt_path_coords(resp.text)
    if not paths:
        raise SystemExit("API returned no parsable path geometry.")
    return paths


def load_db():
    from db import fetch_bt_path_coords
    return fetch_bt_path_coords()  # active=1 only


def load_snapshot(rev):
    out = subprocess.run(["git", "show", "%s:docs/bt-paths-map.html" % rev],
                         cwd=str(ROOT), capture_output=True)
    if out.returncode != 0:
        raise SystemExit("git show failed: %s" % out.stderr.decode("utf-8", "replace").strip())
    text = out.stdout.decode("utf-8")
    marker = "var FEATURES = "
    pos = text.find(marker)
    if pos < 0:
        raise SystemExit("No 'var FEATURES = ' in docs/bt-paths-map.html at %s" % rev)
    features, _ = json.JSONDecoder().raw_decode(text, pos + len(marker))
    return {str(f["id"]): {"name": f.get("name"), "coords": f.get("coords") or []} for f in features}


def load_node_positions():
    """{site_code: (x, y)} for Bluetooth sensors, for the wrong-member hint."""
    try:
        from db import fetch_sensor_coords
        sensors = fetch_sensor_coords().get("Bluetooth", {})
    except Exception:
        return {}
    nodes = {}
    for s in sensors.values():
        code = s.get("site_code")
        if code:
            nodes[str(code)] = project([[s["lat"], s["lon"]]])[0]
    return nodes


# ---- State / output ----------------------------------------------------------
def _pair_key(pr):
    return "%s|%s" % (pr["path_a"], pr["path_b"])


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(source, pairs, now):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "timestamp": now, "source": source,
        "pairs": {_pair_key(pr): pr["bucket"] for pr in pairs},
    }, indent=1), encoding="utf-8")


def write_csv(pairs, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for pr in pairs:
            row = []
            for c in CSV_COLUMNS:
                v = pr[c]
                if c == "r":
                    v = "" if v is None else "%.3f" % v
                elif c in ("cov_a", "cov_b", "length_ratio"):
                    v = "%.3f" % v
                elif c == "endpoint_sum_m":
                    v = "%.0f" % v
                row.append(v)
            w.writerow(row)


def _a(s):
    """ASCII-only text for the Windows console."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _table(pairs):
    rows = [("A", "name A", "B", "name B", "r", "cov", "hint")]
    for pr in pairs:
        rows.append((pr["path_a"], pr["name_a"], pr["path_b"], pr["name_b"],
                     "n/a" if pr["r"] is None else "%.2f" % pr["r"],
                     "%.0f%%" % (100 * min(pr["cov_a"], pr["cov_b"])), pr["hint"]))
    rows = [tuple(_a(c)[:60] for c in r) for r in rows]
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for k, r in enumerate(rows):
        print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        if k == 0:
            print("  " + "  ".join("-" * w for w in widths))


def report(result, source, state, now):
    pairs = result["pairs"]
    by = {b: [p for p in pairs if p["bucket"] == b]
          for b in (B_NON_REVERSED, B_SAME_UNEXPL, B_HEALTHY, B_UNCLEAR)}
    prev_count = None
    if state:
        prev_count = sum(1 for b in state["pairs"].values() if b == B_NON_REVERSED)

    print()
    print("NON-REVERSED PAIRS: %d (previous run: %s)" %
          (len(by[B_NON_REVERSED]), "n/a" if prev_count is None else prev_count))
    n = len(by[B_NON_REVERSED])
    if n == 0:
        print("Verdict: OK - no named reverse pair is stored in the same direction.")
    elif prev_count == 0:
        print("Verdict: THE ISSUE IS BACK - %d pair(s) stored in the same direction." % n)
    else:
        print("Verdict: ISSUE PRESENT - %d pair(s) stored in the same direction." % n)

    print()
    print("Source: %s | paths analysed: %d (skipped, <2 points: %d)" %
          (_a(source), result["n_paths"], result["skipped"]))
    print("  1 NON-REVERSED (names A->B / B->A, same direction): %d" % n)
    print("  2 SAME-DIRECTION, UNEXPLAINED:                      %d" % len(by[B_SAME_UNEXPL]))
    print("  3 HEALTHY (opposite direction):                     %d" % len(by[B_HEALTHY]))
    print("  4 UNCLEAR (|r| < %.1f):                              %d" % (DIRECTION_R, len(by[B_UNCLEAR])))
    print("  Info: paths with no partner:                        %d" % len(result["no_partner"]))
    ns = result["named_stats"]
    print("  Info: A->B/B->A named pairs by name: %d, found by geometry: %d "
          "(opposite %d, same %d, unclear %d)" %
          (ns["named_pairs"], ns["captured"], ns["opposite"], ns["same"], ns["unclear"]))

    if by[B_NON_REVERSED]:
        print("\n[1] NON-REVERSED pairs")
        _table(by[B_NON_REVERSED])
    if by[B_SAME_UNEXPL]:
        print("\n[2] SAME-DIRECTION, UNEXPLAINED pairs (review)")
        _table(by[B_SAME_UNEXPL])
    if by[B_UNCLEAR]:
        print("\n[4] UNCLEAR pairs")
        _table(by[B_UNCLEAR])

    # Change since last run: compare everything except healthy pairs.
    print()
    if state is None:
        print("Changes: no previous run on record - this run is the baseline.")
        return
    cur = {_pair_key(p): p["bucket"] for p in pairs if p["bucket"] != B_HEALTHY}
    old = {k: b for k, b in state["pairs"].items() if b != B_HEALTHY}
    new = sorted((k for k in cur if old.get(k) != cur[k]), key=lambda k: k)
    resolved = sorted((k for k in old if cur.get(k) != old[k]), key=lambda k: k)
    when = state.get("timestamp", "?")
    if not new and not resolved:
        print("Changes: no change since %s (previous source: %s)." % (when, _a(state.get("source", "?"))))
        return
    print("Changes since %s (previous source: %s):" % (when, _a(state.get("source", "?"))))
    for k in new:
        print("  NEW      %-14s %s" % (k, cur[k]))
    for k in resolved:
        print("  RESOLVED %-14s %s" % (k, old[k]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Detect non-reversed BT path pairs by geometry (read-only).")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--db", action="store_true", help="use results/history.db (bt_path_coords, active only)")
    src.add_argument("--snapshot", metavar="REV", help="replay docs/bt-paths-map.html at a git revision")
    ap.add_argument("--save", action="store_true",
                    help="with --snapshot: also write the state file and CSV (normally skipped)")
    ap.add_argument("--csv", metavar="PATH", help="CSV output path (default reports/bt_direction_check.csv)")
    args = ap.parse_args(argv)

    if args.snapshot:
        source, paths = "snapshot:%s" % args.snapshot, load_snapshot(args.snapshot)
    elif args.db:
        source, paths = "db", load_db()
    else:
        source, paths = "live-api", load_live()

    result = analyse(paths, load_node_positions())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_state()
    report(result, source, state, now)

    persist = not args.snapshot or args.save
    if persist:
        write_csv(result["pairs"], Path(args.csv) if args.csv else CSV_PATH)
        save_state(source, result["pairs"], now)
        print("\nWrote %s and %s" % (_a(args.csv or CSV_PATH.relative_to(ROOT)), STATE_PATH.relative_to(ROOT)))
    else:
        if args.csv:
            write_csv(result["pairs"], Path(args.csv))
        print("\nSnapshot replay: state file not updated (use --save to keep it).")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # keep the console readable; .bat pauses either way
        print("ERROR: %s" % _a(exc))
        sys.exit(1)
