#!/usr/bin/env python3
"""
bt_paths_map.py — standalone map of Bluetooth travel-time paths only.

Reads bt_path_coords and Bluetooth sensor_coords from the local SQLite DB and
writes a self-contained HTML page with a Leaflet map showing just the path
polylines plus the sensor inventory that anchors them — no other groups, no
health data. Useful for checking path geometry/routing in isolation from
everything else on the main dashboard.

Usage:
    python runner/bt_paths_map.py
    python runner/bt_paths_map.py --out reports/bt_paths_map.html
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("DB_PATH", str(Path(__file__).parent.parent / "results" / "history.db"))

# Load .env from repo root if present
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#') and '=' in _line:
            _k, _, _v = _line.partition('=')
            os.environ.setdefault(_k.strip(), _v.strip())

from db import fetch_bt_path_coords, fetch_sensor_coords, get_connection
from qa import load_reference, _haversine_m
from stability import CYPRUS_TZ, to_cyprus

REPORT_DIR = Path(__file__).parent.parent / "reports"
DOCS_DIR = Path(__file__).parent.parent / "docs"
WORKBOOK = Path(__file__).parent.parent / "QA Locations.xlsx"

# Rotating categorical palette so adjacent/overlapping paths stay visually
# distinct without health data to color by.
PALETTE = [
    "#d02525", "#FF7A5C", "#b8860b", "#F4C800", "#007D34",
    "#6cdf49", "#49df8f", "#49c4df", "#2575d0", "#000075",
    "#a149df", "#df49b3", "#d02561", "#808080", "#FF6800",
    "#593315",
]


# Geometric-overlap detection for colour assignment — deliberately ignores path
# names/IDs. The whole point of this map is to spot data problems, e.g. a pair
# that's *supposed* to be a simple reverse of each other but whose traced
# coordinates actually diverge — so "do these run alongside each other" has to
# be answered from real coordinates, not from the A->B naming convention.
SAMPLE_POINTS     = 16   # points sampled evenly along each path for proximity checks
# 25m and 50m both undershot real cases (a motorway's opposite carriageways, with a
# wide median and interchange ramps, traced up to ~100m apart). There's no clean gap
# in the data between "same road" and "different nearby road" separations, so this
# leans generous: a false "these run alongside each other" costs nothing worse than
# two unrelated paths getting different colours, while undershooting reproduces the
# exact same-colour-on-parallel-carriageways bug this exists to prevent.
OVERLAP_M         = 120  # metres apart counts as "running alongside"
OVERLAP_FRACTION  = 0.5  # fraction of a path's samples that must be this close to the other

# Duplicate-candidate geometry check for unnamed paths — much tighter than the
# "running alongside" check above, since this is standing in for an exact name
# match: it needs to catch the same route re-traced almost point-for-point,
# not merely two paths sharing a road.
DUPLICATE_OVERLAP_M        = 40   # metres — tight; duplicates trace the same physical route
DUPLICATE_OVERLAP_FRACTION = 0.7  # both directions must be almost fully coincident
# A second, one-directional tier for pairs with very uneven point coverage —
# e.g. an early, rougher registration (fewer points, doesn't trace quite as
# far) later re-registered properly. The short side can still be almost
# entirely contained in the long side's route even though the long side
# only partly overlaps the short one, which fails the bidirectional check
# above outright. Caught this way, not auto-collapsed like a confirmed dup.
DUPLICATE_CONTAINED_FRACTION = 0.85

# A path traced with only a couple of points is usually a relic of an early,
# rougher registration pass rather than a deliberately simple short hop —
# worth a reviewer's eye first when working through a decade of accumulated
# paths.
SUSPICIOUS_MIN_POINTS = 5


def _path_length_m(coords):
    """Total length of a traced path — sum of haversine distance between each
    consecutive pair of points, not a straight line between the endpoints."""
    return sum(
        _haversine_m(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
        for i in range(len(coords) - 1)
    )


def _sample_points(coords, n=SAMPLE_POINTS):
    if len(coords) <= n:
        return coords
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


def _bbox(coords):
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    return min(lats), max(lats), min(lons), max(lons)


def _bboxes_overlap(a, b, buffer_deg):
    a_min_lat, a_max_lat, a_min_lon, a_max_lon = a
    b_min_lat, b_max_lat, b_min_lon, b_max_lon = b
    return (a_min_lat - buffer_deg <= b_max_lat and b_min_lat - buffer_deg <= a_max_lat and
            a_min_lon - buffer_deg <= b_max_lon and b_min_lon - buffer_deg <= a_max_lon)


def _overlap_fraction(samples_a, samples_b, threshold_m):
    if not samples_a:
        return 0.0
    close = sum(
        1 for pa in samples_a
        if any(_haversine_m(pa[0], pa[1], pb[0], pb[1]) <= threshold_m for pb in samples_b)
    )
    return close / len(samples_a)


def assign_contrasting_colors(features):
    """Colour each path so that any two paths running alongside each other in
    real space — by any margin, however their names relate — never share a
    colour. Bounding-box pre-filtering keeps the pairwise geometry check cheap
    across hundreds of paths.
    """
    with_coords = [f for f in features if f["coords"]]
    samples = {f["id"]: _sample_points(f["coords"]) for f in with_coords}
    bboxes  = {f["id"]: _bbox(f["coords"]) for f in with_coords}
    buffer_deg = (OVERLAP_M + 100) / 111_000  # generous; exact check follows via haversine

    conflicts = {f["id"]: set() for f in features}
    ids = [f["id"] for f in with_coords]
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1:]:
            if not _bboxes_overlap(bboxes[id_a], bboxes[id_b], buffer_deg):
                continue
            frac_a = _overlap_fraction(samples[id_a], samples[id_b], OVERLAP_M)
            frac_b = _overlap_fraction(samples[id_b], samples[id_a], OVERLAP_M)
            if max(frac_a, frac_b) >= OVERLAP_FRACTION:
                conflicts[id_a].add(id_b)
                conflicts[id_b].add(id_a)

    color_idx = {}
    use_count = [0] * len(PALETTE)
    for f in features:
        used = {color_idx[pid] for pid in conflicts[f["id"]] if pid in color_idx}
        available = [idx for idx in range(len(PALETTE)) if idx not in used]
        # Picking the first free slot (as before) meant every conflict-free path —
        # the vast majority, since most paths don't run alongside anything — grabbed
        # palette index 0 every time, piling the same color onto unrelated paths
        # across the whole map. Picking the least-used available color instead
        # spreads all 16 colors out evenly regardless of conflicts.
        chosen = min(available, key=lambda idx: use_count[idx]) if available else 0
        color_idx[f["id"]] = chosen
        use_count[chosen] += 1
        f["color"] = PALETTE[chosen]


# Duplicate-registration detection ------------------------------------------
# Some path names carry two active path_ids in bt_path_coords — SWARCO
# re-registering a route under a new ID without retiring the old one.
# Coordinate overlap alone can't reliably separate a true duplicate from a
# coincidental name collision: the one confirmed non-duplicate found by hand
# (7084->7086, IDs 443/444) overlaps just as much on coordinates as the real
# duplicates do. What actually told them apart is pass/fail history — a true
# duplicate is the same measurement mirrored under two IDs, so it succeeds and
# fails on the exact same test runs; independent paths that merely share a
# name don't. Threshold matches stability.ATTENTION_FAIL_RATIO's convention.
DUPLICATE_STATUS_MATCH = 0.8


def _find_geometric_duplicate_groups(paths, ids):
    """Group paths whose geometry is a near-exact match, regardless of name —
    two differently-named paths tracing the same road (e.g. re-registered
    under a new ID without retiring the old one) are just as much a
    duplicate as two unnamed ones, and location doesn't care what either one
    is called. Flags a pair if either:
      - both directions of the overlap fraction clear the tight bidirectional
        threshold (the two traces are almost fully coincident), or
      - one path is almost entirely contained in the other (one-directional),
        which catches a shorter/rougher trace re-registered more completely
        later without failing the bidirectional check outright.
    A short path that merely starts along a long one still won't count,
    since even full containment requires clearing DUPLICATE_CONTAINED_FRACTION
    of its own points, not just a partial run alongside.

    Uses every traced point rather than the fixed 16-point sample used
    elsewhere: that sample is fine for the loose "runs alongside" check at
    120m (used across hundreds of paths at once, where speed matters more),
    but 16 points spread over a ~20km path are >1km apart — nowhere near
    dense enough for a real match to reliably land within this check's much
    tighter 40m tolerance. This check only ever runs pairwise on paths whose
    bounding boxes already overlap, so the extra cost of comparing full
    point lists is negligible.
    """
    ids = [pid for pid in ids if len(paths[pid].get('coords') or []) > 1]
    samples = {pid: paths[pid]['coords'] for pid in ids}
    bboxes = {pid: _bbox(paths[pid]['coords']) for pid in ids}
    buffer_deg = (DUPLICATE_OVERLAP_M + 50) / 111_000

    adjacency = {pid: set() for pid in ids}
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1:]:
            if not _bboxes_overlap(bboxes[id_a], bboxes[id_b], buffer_deg):
                continue
            frac_a = _overlap_fraction(samples[id_a], samples[id_b], DUPLICATE_OVERLAP_M)
            frac_b = _overlap_fraction(samples[id_b], samples[id_a], DUPLICATE_OVERLAP_M)
            if min(frac_a, frac_b) >= DUPLICATE_OVERLAP_FRACTION:
                adjacency[id_a].add(id_b)
                adjacency[id_b].add(id_a)

    seen, groups = set(), []
    for pid in ids:
        if pid in seen or not adjacency[pid]:
            continue
        group, stack = set(), [pid]
        while stack:
            cur = stack.pop()
            if cur in group:
                continue
            group.add(cur)
            stack.extend(adjacency[cur] - group)
        seen |= group
        groups.append(sorted(group))
    return groups


def find_duplicate_groups(paths):
    """Group active paths that might be the same route registered twice; for
    any candidate group, decide from status history whether it's a true
    duplicate (collapse to one) or genuinely different paths that just
    happened to look alike (keep both, flag for manual review).

    Named paths sharing an exact name are grouped directly. Everything else —
    two differently-named paths, or a named path and an unnamed one — is
    checked geometrically instead, since exact-name matching alone misses a
    route re-registered under a new name (or never named at all) without the
    old copy being retired.

    Paths named in the "A->B" node convention are excluded from the
    geometric check entirely — that's the reversed-direction checker's
    territory (see find_reversed_direction_pairs), not this one. A same-
    direction bug pair's coordinates are, by definition, near-identical to
    each other, so without this exclusion the geometric check here would
    catch every bug pair too and silently collapse one path out of each —
    hiding the exact evidence the bug check exists to surface, and treating
    a real (if buggy) forward/reverse registration as a spurious duplicate.

    Returns (keep_ids, collapsed, ambiguous):
      keep_ids   — path_ids to actually render
      collapsed  — [(label, kept_id, dropped_ids, match_pct)]
      ambiguous  — [(label, ids, match_pct)] — status diverges, kept as-is
    """
    by_name = {}
    for pid, p in paths.items():
        name = p.get('name')
        if name:
            by_name.setdefault(name, []).append(pid)
    dup_groups = {name: ids for name, ids in by_name.items() if len(ids) > 1}

    # Geometric check spans every active path not claimed by the
    # reversed-direction naming convention, named or not — exact-name groups
    # above already cover the case where names agree, so this only adds
    # groups that name matching alone couldn't have found.
    geometry_candidates = [pid for pid, p in paths.items() if not _REVERSED_NAME_RE.match(p.get('name') or '')]
    existing_sets = {frozenset(ids) for ids in dup_groups.values()}
    for group_ids in _find_geometric_duplicate_groups(paths, geometry_candidates):
        if frozenset(group_ids) in existing_sets:
            continue
        names = ', '.join(paths[pid].get('name') or pid for pid in group_ids)
        dup_groups[f'(geometry match) {names}'] = group_ids

    keep_ids = set(paths.keys())
    collapsed, ambiguous = [], []
    if not dup_groups:
        return keep_ids, collapsed, ambiguous

    conn = get_connection()
    for name, ids in dup_groups.items():
        histories = {}
        for pid in ids:
            rows = conn.execute(
                "SELECT run_id, status FROM sensor_results "
                "WHERE group_name='Bluetooth Paths' AND sensor_id=?", (pid,)
            ).fetchall()
            histories[pid] = {r['run_id']: r['status'] for r in rows}

        base = ids[0]
        match_pcts = []
        for other in ids[1:]:
            common = set(histories[base]) & set(histories[other])
            if common:
                matching = sum(1 for r in common if histories[base][r] == histories[other][r])
                match_pcts.append(matching / len(common))
        match_pct = min(match_pcts) if match_pcts else 0.0

        if match_pct >= DUPLICATE_STATUS_MATCH:
            kept = max(ids, key=lambda pid: len(paths[pid].get('coords') or []))
            dropped = [pid for pid in ids if pid != kept]
            keep_ids -= set(dropped)
            collapsed.append((name, kept, dropped, match_pct))
        else:
            ambiguous.append((name, ids, match_pct))
    conn.close()

    return keep_ids, collapsed, ambiguous


# Reversed-direction check -----------------------------------------------
# Confirmed via manual API reverse-engineering (SWARCO Mistic PathsManager):
# BuildPathArcs saves the geometry in one fixed direction regardless of which
# end a reverse-registered path names first. A path named "A->B" whose
# saved coordinates don't actually start near A and end near B relative to
# its "B->A" counterpart is evidence of that defect, not a genuine duplicate.
#
# Each endpoint is checked independently against this threshold, rather than
# summing both distances against one flat cutoff — a summed check lets one
# noisy endpoint (e.g. 31/32: 75m + 127m = 202m) drag an otherwise-obvious
# same-direction pair over the line into "ambiguous", even though 127m alone
# is well within normal GPS/arc-snapping noise for a single endpoint.
REVERSED_CHECK_ENDPOINT_M = 150  # metres — each endpoint independently this close counts as "matches"
REVERSED_CHECK_CLEAR_M    = 300  # metres — the *other* pairing's combined distance must clear this to be ruled out

_REVERSED_NAME_RE = re.compile(r'^\s*(\S+)\s*->\s*(\S+)\s*$')


def find_reversed_direction_pairs(paths):
    """Check every named "A->B" / "B->A" path pair for whether the saved
    coordinates are actually reversed, or just a copy in the same order.
    Only the start/end points are compared — enough to tell "mirrored" from
    "same order" regardless of path length, and cheap across hundreds of
    pairs.

    Returns (by_id, groups):
      by_id  — {path_id: {category, partner_id, same_dist, mirror_dist}}
      groups — {pair_key: {ids: [id1, id2], category, same_dist, mirror_dist}}
    category is one of 'bug' (same direction, not reversed), 'ok' (properly
    mirrored), or 'ambiguous' (neither — likely a genuinely different route
    between the same two named nodes, not a direction problem).
    """
    by_name = {}
    for pid, p in paths.items():
        m = _REVERSED_NAME_RE.match(p.get('name') or '')
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        by_name.setdefault(frozenset([a, b]), []).append((pid, a, b))

    by_id, groups = {}, {}
    for entries in by_name.values():
        if len(entries) != 2:
            continue
        (pid1, a1, b1), (pid2, a2, b2) = entries
        if not (a1 == b2 and b1 == a2):
            continue
        c1, c2 = paths[pid1].get('coords') or [], paths[pid2].get('coords') or []
        if len(c1) < 2 or len(c2) < 2:
            continue
        start1, end1 = c1[0], c1[-1]
        start2, end2 = c2[0], c2[-1]
        mirror_start = _haversine_m(start1[0], start1[1], end2[0], end2[1])
        mirror_end   = _haversine_m(end1[0], end1[1], start2[0], start2[1])
        same_start   = _haversine_m(start1[0], start1[1], start2[0], start2[1])
        same_end     = _haversine_m(end1[0], end1[1], end2[0], end2[1])
        mirror_dist = mirror_start + mirror_end
        same_dist = same_start + same_end

        if (same_start < REVERSED_CHECK_ENDPOINT_M and same_end < REVERSED_CHECK_ENDPOINT_M
                and mirror_dist > REVERSED_CHECK_CLEAR_M):
            category = 'bug'
        elif (mirror_start < REVERSED_CHECK_ENDPOINT_M and mirror_end < REVERSED_CHECK_ENDPOINT_M
                and same_dist > REVERSED_CHECK_CLEAR_M):
            category = 'ok'
        else:
            category = 'ambiguous'

        by_id[pid1] = {'category': category, 'partner_id': pid2, 'same_dist': same_dist, 'mirror_dist': mirror_dist}
        by_id[pid2] = {'category': category, 'partner_id': pid1, 'same_dist': same_dist, 'mirror_dist': mirror_dist}
        groups[f'{a1}->{b1} / {a2}->{b2}'] = {
            'ids': [pid1, pid2], 'category': category,
            'same_dist': same_dist, 'mirror_dist': mirror_dist,
        }
    return by_id, groups


def fetch_latest_path_live_data():
    """Latest recorded speed_kmh/travel_time_s per BT path, from sensor_results.data —
    populated only on runs where LIVE_MODE=true. Paths with no such run (LIVE_MODE
    was off every time, or the path is brand new) are simply absent from the result.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT sensor_id, run_at, data FROM sensor_results "
        "WHERE group_name='Bluetooth Paths' AND data IS NOT NULL ORDER BY sensor_id, run_at"
    ).fetchall()
    conn.close()

    latest = {}
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        latest[r["sensor_id"]] = {
            "speed_kmh": d.get("speed_kmh"),
            "travel_time_s": d.get("travel_time_s"),
            "last_seen": to_cyprus(r["run_at"]),
        }
    return latest


def load_reference_sensors():
    """Bluetooth sensor rows from QA Locations.xlsx, with usable coordinates only."""
    if not WORKBOOK.exists():
        print(f"WARNING: {WORKBOOK.name} not found — skipping the spreadsheet sensor layer.")
        return []
    ref_sensors, _not_electrified = load_reference([f"{WORKBOOK}::Bluetooth"])
    return [s for s in ref_sensors if s.get('lat') is not None and s.get('lon') is not None]


def build_html(paths, sensors, ref_sensors, duplicate_groups=None,
                dropped_ids=None, show_dropped_by_default=False, include_live_data=True,
                reversed_by_id=None, reversed_groups=None):
    now = datetime.now(CYPRUS_TZ).strftime("%Y-%m-%d %H:%M")
    dropped_ids = dropped_ids or set()
    reversed_by_id = reversed_by_id or {}
    reversed_groups = reversed_groups or {}
    ids = sorted(paths.keys())
    # Live speed/travel-time is kept local-only — colleagues viewing the published
    # copy shouldn't see snapshot data that goes stale the moment it's published.
    live_data = fetch_latest_path_live_data() if include_live_data else {}
    features = []
    for pid in ids:
        p = paths[pid]
        coords = p.get("coords") or []
        features.append({
            "id": pid,
            "name": p.get("name") or pid,
            "coords": coords,
            "suspicious": len(coords) < SUSPICIOUS_MIN_POINTS,
            "dup_dropped": pid in dropped_ids,
            "live": live_data.get(pid),
            "length_m": round(_path_length_m(coords)) if len(coords) > 1 else None,
            "reversed_check": reversed_by_id.get(pid),
        })
    assign_contrasting_colors(features)
    features_json = json.dumps(features)
    duplicate_groups = duplicate_groups or {}
    duplicate_groups_json = json.dumps(duplicate_groups)
    reversed_groups_json = json.dumps(reversed_groups)
    show_dropped_default_json = json.dumps(bool(show_dropped_by_default))
    confirmed_dup_count = sum(1 for v in duplicate_groups.values() if v.get("confirmed"))
    ambiguous_dup_count = sum(1 for v in duplicate_groups.values() if not v.get("confirmed"))
    rev_bug_count = sum(1 for v in reversed_groups.values() if v.get("category") == "bug")
    rev_amb_count = sum(1 for v in reversed_groups.values() if v.get("category") == "ambiguous")
    rev_ok_count = sum(1 for v in reversed_groups.values() if v.get("category") == "ok")
    active_count = len(features) - len(dropped_ids)
    suspicious_count = sum(1 for f in features if f["suspicious"])

    sensor_list = [
        {"id": sid, "name": s.get("name") or sid, "lat": s["lat"], "lon": s["lon"],
         "site_code": s.get("site_code")}
        for sid, s in sorted(sensors.items())
    ]
    sensors_json = json.dumps(sensor_list)

    ref_list = [
        {"name": s["name"], "lat": s["lat"], "lon": s["lon"],
         "commissioning": s.get("commissioning", "active"),
         "project": (s.get("extra", {}) or {}).get("project") or None}
        for s in ref_sensors
    ]
    ref_json = json.dumps(ref_list)

    # Same fixed default view as the dashboard map — not a centroid of the
    # paths, which can land in the middle of nowhere depending on their spread.
    center_lat, center_lon = 34.95, 33.15

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluetooth Paths — Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">
<script data-goatcounter="https://traffic-control-room.goatcounter.com/count"
        async src="//gc.zgo.at/count.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#f4f6f9; --surface:#fff; --surface2:#f8f9fa; --border:#e5e7eb; --border-soft:#eee;
  --text:#222; --muted:#666; --hover:#f0f6ff;
}}
body.dark{{
  --bg:#12151a; --surface:#1c1f26; --surface2:#20242c; --border:#2b3038; --border-soft:#262a32;
  --text:#e8eaed; --muted:#9aa1ab; --hover:#232a35;
}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:Arial,sans-serif;font-size:13px;background:var(--bg);color:var(--text);
      display:flex;flex-direction:column;transition:background .15s,color .15s}}
header{{flex:0 0 auto;background:#1f4e79;color:#fff;padding:16px 24px;
        display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
header h1{{font-size:20px;font-weight:bold}}
header p{{font-size:12px;opacity:.8;margin-top:4px}}
.dm-btn{{background:rgba(255,255,255,.14);color:#fff;border:none;border-radius:6px;
      padding:8px 14px;font-size:12px;font-weight:bold;cursor:pointer;white-space:nowrap;
      display:flex;align-items:center;gap:6px}}
.dm-btn:hover{{background:rgba(255,255,255,.24)}}
.summary{{flex:0 0 auto;display:flex;gap:20px;padding:16px 24px;flex-wrap:wrap;align-items:flex-start}}
.summary-group{{display:flex;flex-direction:column;gap:6px}}
.summary-group + .summary-group{{border-left:1px solid var(--border);padding-left:20px}}
.summary-group-title{{font-size:10.5px;font-weight:bold;text-transform:uppercase;letter-spacing:.04em;
      color:var(--muted)}}
.summary-group-cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{background:var(--surface);border-radius:6px;padding:14px 20px;min-width:130px;
       box-shadow:0 1px 4px rgba(0,0,0,.1);text-align:center;border-top:3px solid #1f4e79}}
.card .num{{font-size:26px;font-weight:bold;color:#1f4e79}}
.card .lbl{{font-size:11px;color:var(--muted);margin-top:3px}}
.card.dup{{border-top-color:#c0392b}}
.card.dup .num{{color:#c0392b}}
.card.dup-warn{{border-top-color:#e67e22}}
.card.dup-warn .num{{color:#e67e22}}
.card.susp{{border-top-color:#b7791f}}
.card.susp .num{{color:#b7791f}}
.card.rev-bug{{border-top-color:#c0392b}}
.card.rev-bug .num{{color:#c0392b}}
.card.rev-amb{{border-top-color:#e67e22}}
.card.rev-amb .num{{color:#e67e22}}
.card.rev-ok{{border-top-color:#27ae60}}
.card.rev-ok .num{{color:#27ae60}}
.sensor-toggle-btn.reversed{{background:#6d28d9}}
.sensor-toggle-btn.reversed:hover{{background:#5b21b6}}
#mapFsWrap{{flex:1 1 auto;min-height:0;margin:0 24px 24px;display:flex}}
#mapFsWrap:fullscreen{{margin:0;padding:12px;background:var(--bg);box-sizing:border-box}}
#mapFsWrap:-webkit-full-screen{{margin:0;padding:12px;background:var(--bg);box-sizing:border-box}}
#map{{flex:1 1 auto;min-height:0;border-radius:6px;
      box-shadow:0 1px 6px rgba(0,0,0,.15)}}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{{background:var(--surface);color:var(--text)}}
.leaflet-popup-content b{{color:var(--text)}}
.leaflet-bar a{{background:var(--surface);color:var(--text);border-bottom-color:var(--border)}}
.leaflet-bar a:hover{{background:var(--hover)}}
.leaflet-control-attribution{{background:rgba(255,255,255,.7)}}
body.dark .leaflet-control-attribution{{background:rgba(28,31,38,.75);color:var(--muted)}}
body.dark .leaflet-control-attribution a{{color:var(--muted)}}
.sensor-toggle-btn{{background:#1f4e79;color:#fff;border:none;border-radius:4px;
      padding:8px 12px;font-size:12px;font-weight:bold;cursor:pointer;
      box-shadow:0 1px 4px rgba(0,0,0,.4);white-space:nowrap}}
.sensor-toggle-btn:hover{{background:#163a5f}}
.sensor-toggle-btn.off{{background:#9ca3af}}
.sensor-toggle-btn.off:hover{{background:#7d8590}}
.sensor-toggle-btn.ref{{background:#8e44ad;margin-top:6px}}
.sensor-toggle-btn.ref:hover{{background:#6c3483}}
.sensor-toggle-btn.ref.off{{background:#9ca3af}}
.sensor-toggle-btn.ref.off:hover{{background:#7d8590}}
.search-ctl{{position:absolute;top:12px;left:50%;transform:translateX(-50%);
      z-index:1000;width:320px}}
.search-ctl input{{width:100%;padding:8px 12px;border:none;border-radius:4px;font-size:13px;
      box-shadow:0 1px 4px rgba(0,0,0,.4);box-sizing:border-box;background:var(--surface);color:var(--text)}}
.search-results{{background:var(--surface);border-radius:4px;margin-top:4px;max-height:280px;overflow-y:auto;
      box-shadow:0 2px 8px rgba(0,0,0,.3);display:none}}
.search-results.visible{{display:block}}
.search-result{{padding:8px 12px;cursor:pointer;font-size:12px;border-bottom:1px solid var(--border-soft)}}
.search-result:last-child{{border-bottom:none}}
.search-result:hover{{background:var(--hover)}}
.search-result .type{{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.03em}}
.search-empty{{padding:8px 12px;font-size:12px;color:var(--muted)}}
.sensor-toggle-btn.susp{{background:#0f766e}}
.sensor-toggle-btn.susp:hover{{background:#0b5c56}}
.sensor-toggle-btn.susp.active{{background:#b7791f}}
.sensor-toggle-btn.susp.active:hover{{background:#92600f}}
.sensor-toggle-btn.dup{{background:#c0392b}}
.sensor-toggle-btn.dup:hover{{background:#992d22}}
.sensor-toggle-btn.dup.off{{background:#9ca3af}}
.sensor-toggle-btn.dup.off:hover{{background:#7d8590}}
.sensor-toggle-btn.export{{background:#27ae60}}
.sensor-toggle-btn.export:hover{{background:#1e8449}}
.sensor-toggle-btn.import{{background:#3949ab;margin-top:6px}}
.sensor-toggle-btn.import:hover{{background:#2c3a82}}
.inspector{{position:absolute;left:12px;bottom:12px;z-index:1000;width:300px;max-height:60%;
      display:flex;flex-direction:column;background:var(--surface);border-radius:8px;
      box-shadow:0 2px 10px rgba(0,0,0,.35);font-size:12px;overflow:hidden;color:var(--text)}}
.inspector-hd{{flex:0 0 auto;background:#1f4e79;color:#fff;padding:7px 10px;cursor:move;
      display:flex;align-items:center;justify-content:space-between;gap:8px;font-weight:bold}}
.inspector-hd .nav{{display:flex;align-items:center;gap:6px;font-weight:normal}}
.inspector-hd .nav button{{background:rgba(255,255,255,.18);border:none;color:#fff;border-radius:4px;
      padding:3px 9px;cursor:pointer;font-size:12px}}
.inspector-hd .nav button:hover{{background:rgba(255,255,255,.32)}}
.inspector-hd .nav span{{font-size:11px;opacity:.9;min-width:52px;text-align:center}}
.inspector-body{{padding:10px 12px;overflow-y:auto}}
.inspector-body .empty{{color:var(--muted);font-style:italic}}
.flag-summary{{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);
      padding-bottom:8px;margin-bottom:8px;border-bottom:1px solid var(--border-soft);flex-wrap:wrap}}
.flag-cycle-toggle{{cursor:pointer;user-select:none;display:flex;align-items:center;gap:4px}}
.flag-cycle-toggle input{{cursor:pointer;margin:0}}
.flag-summary-link{{color:#4a90c4;text-decoration:underline;margin-left:auto;cursor:pointer}}
.flag-list-row{{padding:7px 4px;font-size:12px;cursor:pointer;border-bottom:1px solid var(--border-soft)}}
.flag-list-row:hover{{background:var(--hover)}}
.flag-list-id{{color:var(--muted);font-size:11px}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:10px;font-weight:bold;
      margin:6px 4px 0 0}}
.badge.suspicious{{background:#fde68a;color:#92400e}}
.flag-row{{display:flex;gap:6px;margin-top:9px}}
.flag-row button{{flex:1;border:1px solid var(--border);background:var(--surface2);border-radius:4px;padding:5px 4px;
      font-size:11px;cursor:pointer;color:var(--text)}}
.flag-row button:hover{{background:var(--hover)}}
.flag-row button.active-flag{{background:#c0392b;color:#fff;border-color:#c0392b}}
.flag-row button.active-ok{{background:#27ae60;color:#fff;border-color:#27ae60}}
.inspector-body textarea{{width:100%;margin-top:6px;font-size:11px;padding:5px;border:1px solid var(--border);
      border-radius:4px;resize:vertical;min-height:36px;font-family:inherit;background:var(--surface);color:var(--text)}}
.legend-ctl{{background:var(--surface);color:var(--text);border-radius:8px;
      box-shadow:0 2px 10px rgba(0,0,0,.3);font-size:11.5px;width:190px;overflow:hidden}}
.legend-hd{{padding:7px 10px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;
      font-weight:bold;user-select:none}}
.legend-hd .chev{{font-size:9px;transition:transform .15s}}
.legend-ctl.collapsed .chev{{transform:rotate(-90deg)}}
.legend-body{{padding:0 10px 10px}}
.legend-ctl.collapsed .legend-body{{display:none}}
.legend-sec{{margin-top:8px}}
.legend-sec:first-child{{margin-top:0}}
.legend-sec-title{{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:4px}}
.legend-row{{display:flex;align-items:center;gap:7px;padding:2px 0}}
.legend-swatch{{flex:0 0 auto;width:14px;height:14px;display:flex;align-items:center;justify-content:center}}
.legend-dot{{width:9px;height:9px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.2)}}
.legend-diamond{{width:8px;height:8px;transform:rotate(45deg)}}
.legend-line{{width:16px;height:0;border-top:3px solid}}
.legend-line.dashed{{border-top-style:dashed}}
/* Collapsed = same 30x30 square-icon look as the fullscreen/recentre/zoom
   buttons above it in the stack, so it reads as one of them, not a stray
   pill. Only widens into the labelled panel once opened. */
.filters-ctl{{background:var(--surface);color:var(--text);border-radius:4px;
      box-shadow:0 1px 5px rgba(0,0,0,.65);font-size:12px;overflow:hidden;width:186px}}
.filters-ctl.collapsed{{width:30px}}
.filters-hd{{height:30px;padding:0 10px;cursor:pointer;display:flex;align-items:center;gap:6px;
      font-weight:bold;user-select:none;white-space:nowrap}}
.filters-ctl.collapsed .filters-hd{{padding:0;justify-content:center}}
.filters-hd i{{font-size:15px;flex:0 0 auto}}
.filters-ctl.collapsed .filters-hd .lbl,
.filters-ctl.collapsed .filters-hd .chev{{display:none}}
.filters-hd .chev{{margin-left:auto;font-size:9px}}
.filters-body{{display:flex;flex-direction:column;gap:6px;padding:0 10px 10px}}
.filters-ctl.collapsed .filters-body{{display:none}}
.filters-body .sensor-toggle-btn{{box-shadow:none;margin-top:0 !important;width:100%}}
</style>
</head>
<body>
<header>
  <div>
    <h1>Bluetooth Paths — Map</h1>
    <p>Generated {now}&nbsp;&nbsp;|&nbsp;&nbsp;{active_count} active paths&nbsp;&nbsp;|&nbsp;&nbsp;
       {len(sensor_list)} API sensors&nbsp;&nbsp;|&nbsp;&nbsp;{len(ref_list)} spreadsheet sensors</p>
  </div>
  <button class="dm-btn" onclick="toggleDark()" id="dmBtn" aria-label="Toggle dark mode">
    <i class="ti ti-sun" id="dmIcon" aria-hidden="true"></i><span id="dmLabel">Light</span>
  </button>
</header>

<div class="summary">
  <div class="summary-group">
    <div class="summary-group-title">Inventory</div>
    <div class="summary-group-cards">
      <div class="card"><div class="num">{active_count}</div><div class="lbl">Bluetooth paths</div></div>
      <div class="card"><div class="num">{len(sensor_list)}</div><div class="lbl">API sensors</div></div>
      <div class="card"><div class="num">{len(ref_list)}</div><div class="lbl">Spreadsheet sensors</div></div>
    </div>
  </div>
  <div class="summary-group">
    <div class="summary-group-title">Duplicate registrations</div>
    <div class="summary-group-cards">
      {f'<div class="card dup" id="dupCard" style="cursor:pointer" title="Click to jump to it on the map"><div class="num">{confirmed_dup_count}</div><div class="lbl">Duplicate paths</div></div>' if confirmed_dup_count else '<div class="card dup"><div class="num">0</div><div class="lbl">Duplicate paths</div></div>'}
      {f'<div class="card dup-warn" id="ambiguousCard" style="cursor:pointer" title="Click to jump to it on the map"><div class="num">{ambiguous_dup_count}</div><div class="lbl">Same name, needs manual check</div></div>' if ambiguous_dup_count else ''}
    </div>
  </div>
  <div class="summary-group">
    <div class="summary-group-title">Data quality checks</div>
    <div class="summary-group-cards">
      <div class="card susp"><div class="num">{suspicious_count}</div><div class="lbl">Suspicious (&lt;{SUSPICIOUS_MIN_POINTS} points)</div></div>
      {f'<div class="card rev-bug" id="revBugCard" style="cursor:pointer" title="Click to jump to it on the map"><div class="num">{rev_bug_count}</div><div class="lbl">Reversed-direction bug</div></div>' if rev_bug_count else ''}
      {f'<div class="card rev-amb" id="revAmbCard" style="cursor:pointer" title="Click to jump to it on the map"><div class="num">{rev_amb_count}</div><div class="lbl">Reversed check: ambiguous</div></div>' if rev_amb_count else ''}
      {f'<div class="card rev-ok" id="revOkCard" style="cursor:pointer" title="Click to jump to it on the map"><div class="num">{rev_ok_count}</div><div class="lbl">Reversed check: OK</div></div>' if rev_ok_count else ''}
    </div>
  </div>
</div>

<div id="mapFsWrap"><div id="map"></div></div>

<script>
// Flagging/notes and CSV import-export are the reviewer's own working tools —
// meaningless (and confusing) on the published copy colleagues open read-only,
// since flags live in each browser's own localStorage and never sync between
// viewers. Reuse the include_live_data signal: it already means "this is the
// published copy", not the local review session.
var QA_MODE = {json.dumps(bool(include_live_data))};
var FEATURES = {features_json};
var SENSORS  = {sensors_json};
var REF_SENSORS = {ref_json};
var DUPLICATE_GROUPS = {duplicate_groups_json};
var SHOW_DUPLICATES = {show_dropped_default_json};
var REVERSED_GROUPS = {reversed_groups_json};
// Keyed by path id rather than name — features fall back to their id as a
// display name when the API sends none (see FEATURES below), which would
// otherwise never match a group keyed by the real (possibly null) name.
var DUPLICATE_BY_ID = {{}};
Object.keys(DUPLICATE_GROUPS).forEach(function(name) {{
  var g = DUPLICATE_GROUPS[name];
  g.ids.forEach(function(id) {{ DUPLICATE_BY_ID[id] = g; }});
}});

// doubleClickZoom off: cycling through a stack of overlapping paths means
// clicking the same spot repeatedly, which Leaflet's default dblclick
// handling would otherwise read as "zoom in" and yank the view out from
// under you mid-review.
var DEFAULT_VIEW = {{center: [{center_lat}, {center_lon}], zoom: 10}};
var map = L.map('map', {{zoomControl:false, doubleClickZoom:false}}).setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

// Topright stack, added in visual top-to-bottom order: fullscreen, recentre, zoom.
var FullscreenControl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    var link = L.DomUtil.create('a', '', container);
    link.href = '#';
    link.id = 'mapFsBtn';
    link.title = 'Toggle full screen';
    link.innerHTML = '<i class="ti ti-arrows-maximize" style="font-size:15px;line-height:26px"></i>';
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(link, 'click', function(e) {{
      L.DomEvent.preventDefault(e);
      toggleMapFullscreen();
    }});
    return container;
  }}
}});
map.addControl(new FullscreenControl());

var RecentreControl = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var container = L.DomUtil.create('div', 'leaflet-bar leaflet-control');
    var link = L.DomUtil.create('a', '', container);
    link.href = '#';
    link.title = 'Recentre map';
    link.innerHTML = '<i class="ti ti-crosshair" style="font-size:15px;line-height:26px"></i>';
    L.DomEvent.disableClickPropagation(container);
    L.DomEvent.on(link, 'click', function(e) {{
      L.DomEvent.preventDefault(e);
      map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
    }});
    return container;
  }}
}});
map.addControl(new RecentreControl());

L.control.zoom({{position: 'topright'}}).addTo(map);

// Every sensor/duplicate/reversed-check/export toggle below lives inside this
// one collapsible panel instead of each being its own floating button —
// otherwise the topright corner grows a new button per feature (was up to
// 7 stacked buttons) and none of them read as a group.
var FiltersPanel = L.Control.extend({{
  options: {{position: 'topright'}},
  onAdd: function() {{
    var box = L.DomUtil.create('div', 'filters-ctl collapsed');
    L.DomEvent.disableClickPropagation(box);
    var hd = L.DomUtil.create('div', 'filters-hd', box);
    hd.innerHTML = '<i class="ti ti-adjustments-horizontal" aria-hidden="true"></i><span class="lbl">Filters</span><span class="chev">&#9660;</span>';
    hd.title = 'Filters';
    var body = L.DomUtil.create('div', 'filters-body', box);
    hd.onclick = function() {{ box.classList.toggle('collapsed'); }};
    this._body = body;
    return box;
  }}
}});
var filtersPanel = new FiltersPanel();
map.addControl(filtersPanel);
var filtersBody = filtersPanel._body;

function toggleMapFullscreen() {{
  var el = document.getElementById('mapFsWrap');
  var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  if (!fsEl) {{
    (el.requestFullscreen || el.webkitRequestFullscreen || function(){{}}).call(el);
  }} else {{
    (document.exitFullscreen || document.webkitExitFullscreen || function(){{}}).call(document);
  }}
}}
function _onFsChange() {{
  var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
  var icon = document.querySelector('#mapFsBtn i');
  if (icon) icon.className = 'ti ' + (fsEl ? 'ti-arrows-minimize' : 'ti-arrows-maximize');
  setTimeout(function() {{ map.invalidateSize(); }}, 120);
}}
document.addEventListener('fullscreenchange', _onFsChange);
document.addEventListener('webkitfullscreenchange', _onFsChange);
var LIGHT_TILES = {{url: 'https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  attribution: '© OpenStreetMap contributors'}};
var DARK_TILES = {{url: 'https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
  attribution: '© OpenStreetMap contributors © <a href="https://carto.com/attributions">CARTO</a>'}};
var tileLayer = L.tileLayer(LIGHT_TILES.url, {{attribution: LIGHT_TILES.attribution, maxZoom: 19}}).addTo(map);

var _dark = true;
document.body.classList.add('dark');
map.removeLayer(tileLayer);
tileLayer = L.tileLayer(DARK_TILES.url, {{attribution: DARK_TILES.attribution, maxZoom: 19}}).addTo(map);
function toggleDark() {{
  _dark = !_dark;
  document.body.classList.toggle('dark', _dark);
  document.getElementById('dmIcon').className = _dark ? 'ti ti-sun' : 'ti ti-moon';
  document.getElementById('dmLabel').textContent = _dark ? 'Light' : 'Dark';
  map.removeLayer(tileLayer);
  var t = _dark ? DARK_TILES : LIGHT_TILES;
  tileLayer = L.tileLayer(t.url, {{attribution: t.attribution, maxZoom: 19}}).addTo(map);
  tileLayer.bringToBack();
}}

var SUSPICIOUS_MIN_POINTS = {SUSPICIOUS_MIN_POINTS};
var SUSPICIOUS_ONLY = false;

// Reviewer's findings, keyed by path id, persisted locally so a multi-session
// review of ~500 legacy paths doesn't lose progress on reload: {{id: {{status: 'flag'|'ok', note: string}}}}
// Always declared (even off QA_MODE) since a few read-only helpers below
// reference it unconditionally — but the published copy never touches
// localStorage for it, so it stays permanently empty for every viewer.
var FLAGS = {{}};
if (QA_MODE) {{
  try {{ FLAGS = JSON.parse(localStorage.getItem('btPathFlags') || '{{}}'); }} catch (e) {{ FLAGS = {{}}; }}
}}

/* -- Paths: click to highlight ------------------------------------ */
var pathObjs = [];
var selectedId = null;

// Selected path gets a fixed high-contrast colour of its own (not just a
// thicker version of its palette colour) so it's unmistakable even against
// a dozen overlapping neighbours of similar hue.
var SELECTED_COLOR = '#facc15';

// Reversed-direction check layer: recolors every path that's part of a
// checked "A->B"/"B->A" pair by category, and dims everything else, so the
// bug's footprint reads at a glance without losing the underlying palette
// (restored the moment the toggle goes back off).
var REVERSED_MODE = false;
var REVERSED_COLORS = {{bug: '#c0392b', ambiguous: '#e67e22', ok: '#27ae60'}};
var REVERSED_DIM_COLOR = '#cfd3d8';

function baseStyle(f, selected) {{
  if (selected) return {{color: SELECTED_COLOR, weight: 10, opacity: 1}};
  if (REVERSED_MODE) {{
    if (f.reversed_check) return {{color: REVERSED_COLORS[f.reversed_check.category], weight: 5, opacity: 0.9}};
    return {{color: REVERSED_DIM_COLOR, weight: 2, opacity: 0.35}};
  }}
  return {{color: f.color, weight: 4, opacity: 0.8}};
}}

function applySelection() {{
  pathObjs.forEach(function(o) {{
    var isSel = (o.feature.id === selectedId);
    o.polyline.setStyle(baseStyle(o.feature, isSel));
    if (isSel) {{ o.polyline.bringToFront(); o.decorator.bringToFront(); }}
  }});
}}

/* -- Overlap geometry: hover indicator + interior-touch detection --- */
/* These paths overlap constantly (a short leg often traces a single stretch
   of a much longer route), so "which path is this line" is frequently
   ambiguous from a click alone. Point-to-*polyline* distance (not just
   point-to-endpoint) catches a path whose end lands partway along another
   one's length, not only where the two share an actual endpoint. */
var _ADJACENT_TOL_M = 30;

function _projectToLocal(origin, pt) {{
  var dLat = (pt[0] - origin[0]) * 110574;
  var dLon = (pt[1] - origin[1]) * 111320 * Math.cos(origin[0] * Math.PI / 180);
  return [dLon, dLat];
}}
function _metresBetween(a, b) {{
  var p = _projectToLocal(a, b);
  return Math.hypot(p[0], p[1]);
}}
function _distPointToSegment(pt, a, b) {{
  var P = _projectToLocal(a, pt), B = _projectToLocal(a, b), A = [0, 0];
  var abx = B[0]-A[0], aby = B[1]-A[1];
  var len2 = abx*abx + aby*aby;
  var t = len2 > 0 ? Math.max(0, Math.min(1, ((P[0]-A[0])*abx + (P[1]-A[1])*aby) / len2)) : 0;
  var cx = A[0] + abx*t, cy = A[1] + aby*t;
  return Math.hypot(P[0]-cx, P[1]-cy);
}}
function _distPointToPolyline(pt, coords) {{
  var best = Infinity;
  for (var i = 0; i < coords.length - 1; i++) {{
    var d = _distPointToSegment(pt, coords[i], coords[i+1]);
    if (d < best) best = d;
  }}
  return best;
}}

/* Every path passing within tolerance of a map point — powers both the hover
   tooltip ("N paths overlap here") and click-to-cycle. */
function _overlappingPathsAt(latlng, tol) {{
  var pt = [latlng.lat, latlng.lng];
  tol = tol == null ? _ADJACENT_TOL_M : tol;
  return pathObjs.filter(function(o) {{
    return o.polyline._coords && _distPointToPolyline(pt, o.polyline._coords) <= tol;
  }});
}}

// Bolds whichever path is currently selected so, when cycling through a
// stack via repeated clicks, the list reflects which one you're now on.
function _overlapTooltipHtml(here, f) {{
  if (here.length <= 1) return f.name;
  var rows = here.map(function(o) {{
    return o.feature.id === selectedId ? '<b>' + o.feature.name + '</b>' : o.feature.name;
  }});
  return '<b>' + here.length + ' paths overlap here</b> — click to cycle<br>&bull; ' + rows.join('<br>&bull; ');
}}

/* -- Path start / end markers --------------------------------------- */
var endpointLayer = L.layerGroup().addTo(map);
var _endpointCycle = {{}};
var _lastEndpointClickKey = null;

function _endpointIcon(letter, color, size) {{
  size = size || 19;
  var fs = size >= 18 ? 11 : 9;
  return L.divIcon({{
    className: '',
    html: '<div style="width:'+size+'px;height:'+size+'px;border-radius:50%;'+
          'background:'+color+';border:2.5px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,0.5);'+
          'display:flex;align-items:center;justify-content:center;'+
          'font-size:'+fs+'px;font-weight:700;color:#fff;font-family:sans-serif">'+letter+'</div>',
    iconSize: [size,size], iconAnchor: [size/2,size/2]
  }});
}}

function _clearPathEndpoints() {{
  endpointLayer.clearLayers();
}}

function _showPathEndpoints(pl) {{
  _clearPathEndpoints();
  if (!pl._startLL || !pl._endLL) return;

  var wanted = [
    {{ll: pl._startLL, letter: 'A', color: '#1d9e75', size: 19, name: pl._pathName, id: pl._pathId, kind: 'start'}},
    {{ll: pl._endLL,   letter: 'B', color: '#1a1a2e', size: 19, name: pl._pathName, id: pl._pathId, kind: 'end'}}
  ];
  pathObjs.forEach(function(o) {{
    var p = o.polyline;
    if (p === pl || !p._startLL || !p._endLL) return;
    var touches =
      _distPointToPolyline(pl._startLL, p._coords)  <= _ADJACENT_TOL_M ||
      _distPointToPolyline(pl._endLL,   p._coords)  <= _ADJACENT_TOL_M ||
      _distPointToPolyline(p._startLL,  pl._coords) <= _ADJACENT_TOL_M ||
      _distPointToPolyline(p._endLL,    pl._coords) <= _ADJACENT_TOL_M;
    if (!touches) return;
    wanted.push({{ll: p._startLL, letter: 'a', color: '#7bc4a4', size: 15, name: p._pathName, id: p._pathId, kind: 'start'}});
    wanted.push({{ll: p._endLL,   letter: 'b', color: '#6b7280', size: 15, name: p._pathName, id: p._pathId, kind: 'end'}});
  }});

  // Merge markers landing on the same point so a busy junction gets one
  // marker per position, not a pile of overlapping ones.
  var spots = [];
  wanted.forEach(function(w) {{
    for (var i = 0; i < spots.length; i++) {{
      if (_metresBetween(spots[i].ll, w.ll) <= _ADJACENT_TOL_M) {{
        spots[i].owners.push({{name: w.name, id: w.id, kind: w.kind}});
        return;
      }}
    }}
    spots.push({{ll: w.ll, letter: w.letter, color: w.color, size: w.size,
                owners: [{{name: w.name, id: w.id, kind: w.kind}}]}});
  }});

  // Cycling an endpoint spot re-selects a different path (which rebuilds
  // this whole marker layer, since who's A/B vs a/b depends on the current
  // selection) — so, unlike line-click cycling where the same tooltip object
  // just gets new content, here we must explicitly reopen a tooltip on the
  // freshly-rebuilt marker at the spot that was just clicked, or the list
  // appears to vanish instead of cycling in place.
  var markersByKey = {{}};
  spots.forEach(function(spot) {{
    var names = spot.owners.map(function(o) {{
      var row = o.name + ' — ' + o.kind;
      return o.id === selectedId ? '<b>' + row + '</b>' : row;
    }});
    var label = spot.owners.length > 1
      ? '<b>' + names.length + ' paths meet here</b> — click to cycle<br>&bull; ' + names.join('<br>&bull; ')
      : names[0] + '<br><i style="color:#888">Click to select</i>';
    var key = 'pt:' + spot.ll[0].toFixed(4) + ',' + spot.ll[1].toFixed(4);
    var mk = L.marker(spot.ll, {{icon: _endpointIcon(spot.letter, spot.color, spot.size),
                                interactive: true, zIndexOffset: 500}});
    mk.bindTooltip(label, {{direction: 'top', opacity: 0.95}});
    mk.on('click', function(e) {{
      L.DomEvent.stopPropagation(e);
      var idx = (_endpointCycle[key] || 0) % spot.owners.length;
      _endpointCycle[key] = idx + 1;
      _lastEndpointClickKey = key;
      _applySelectionAndEndpoints(spot.owners[idx].id);
    }});
    mk.addTo(endpointLayer);
    markersByKey[key] = mk;
  }});
  if (_lastEndpointClickKey && markersByKey[_lastEndpointClickKey]) {{
    markersByKey[_lastEndpointClickKey].openTooltip();
  }}
  _lastEndpointClickKey = null;
}}

/* Select-by-id keeps the original toggle behaviour (click the same path again
   to deselect) for the common case of one path under the cursor. */
function _applySelectionAndEndpoints(id) {{
  selectedId = id;
  applySelection();
  if (id === null) {{ _clearPathEndpoints(); renderInspector(); return; }}
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
  if (obj) {{ _ensureVisible(obj); _showPathEndpoints(obj.polyline); }}
  renderInspector();
}}

function selectPath(id) {{
  _applySelectionAndEndpoints(selectedId === id ? null : id);
}}

FEATURES.forEach(function(f) {{
  if (!f.coords.length) return;
  var latlngs = f.coords.map(function(c) {{ return [c[0], c[1]]; }});
  var pl = L.polyline(latlngs, {{color: f.color, weight: 4, opacity: 0.8}}).addTo(map);
  pl._pathName = f.name;
  pl._pathId   = f.id;
  pl._startLL  = latlngs[0];
  pl._endLL    = latlngs[latlngs.length - 1];
  pl._coords   = latlngs;
  var detailHtml = '<b>' + f.name + '</b><br>Path ID: ' + f.id + '<br>' + f.coords.length + ' points';
  if (f.length_m != null) {{
    detailHtml += '<br>Length: ' + (f.length_m >= 1000 ? (f.length_m / 1000).toFixed(2) + ' km' : f.length_m + ' m');
  }}
  if (f.live) {{
    var spd = f.live.speed_kmh, tt = f.live.travel_time_s;
    detailHtml += '<br>Speed: ' + (spd === -1 ? '<span style="color:#c0392b">malfunctioning</span>' : (spd != null ? spd + ' km/h' : '&#8212;'));
    detailHtml += '<br>Travel time: ' + (tt != null ? tt + ' s' : '&#8212;');
    detailHtml += '<br><span style="color:#888;font-size:11px">as of ' + f.live.last_seen + '</span>';
  }} else {{
    detailHtml += '<br><span style="color:#888;font-size:11px">No live speed/travel-time data recorded</span>';
  }}
  var dup = DUPLICATE_BY_ID[f.id];
  if (dup) {{
    var otherIds = dup.ids.filter(function(id) {{ return id !== f.id; }});
    detailHtml += '<br><span style="color:#c0392b;font-weight:bold">⚠ Duplicate registration</span>' +
                 '<br>Also registered as: ID ' + otherIds.join(', ID ') +
                 '<br>Status match: ' + Math.round(dup.match_pct * 100) + '%';
    if (!dup.confirmed) {{
      detailHtml += '<br><span style="color:#e67e22">Not a clean duplicate — needs manual check</span>';
    }}
  }}
  var rc = f.reversed_check;
  if (rc) {{
    var rcLabel = {{bug: 'Same-direction bug — saved coordinates are not actually reversed',
                    ambiguous: 'Ambiguous — likely a different route between the same nodes, not a direction issue',
                    ok: 'Properly reversed'}}[rc.category];
    var rcColor = REVERSED_COLORS[rc.category] || '#888';
    detailHtml += '<br><span style="color:' + rcColor + ';font-weight:bold">Reversed-direction check: ' + rcLabel + '</span>' +
                 '<br>Paired with: ID ' + rc.partner_id +
                 '<br>Same-order distance: ' + Math.round(rc.same_dist) + 'm &nbsp;|&nbsp; mirrored distance: ' + Math.round(rc.mirror_dist) + 'm';
  }}
  // Selection detail (id, point count, duplicate warning, flag/note controls)
  // lives in the fixed Inspector panel, not a popup here — a popup anchored
  // at the click point fights the sticky hover tooltip for the same spot on
  // screen; a panel docked in a corner never competes with it.
  pl.bindTooltip('', {{sticky: true, direction: 'top', opacity: 0.95, className: 'bt-overlap-tip'}});
  pl.on('mouseover', function(e) {{
    if (selectedId === null) pl.setStyle({{weight: 7}});
    pl.setTooltipContent(_overlapTooltipHtml(_overlappingPathsAt(e.latlng), f));
  }});
  pl.on('mouseout',  function() {{ if (selectedId === null) pl.setStyle({{weight: 4}}); }});
  pl.on('click', function(e) {{
    L.DomEvent.stopPropagation(e);
    var here = _overlappingPathsAt(e.latlng);
    if (here.length <= 1) {{ selectPath(f.id); return; }}
    // Multiple paths stacked at this exact spot: step to the next one on each
    // click instead of only ever reaching whichever line Leaflet hit-tested,
    // keyed by click position so repeated clicks near the same spot advance
    // the same cycle.
    var key = 'line:' + e.latlng.lat.toFixed(4) + ',' + e.latlng.lng.toFixed(4);
    var idx = (_endpointCycle[key] || 0) % here.length;
    _endpointCycle[key] = idx + 1;
    var chosen = here[idx];
    _applySelectionAndEndpoints(chosen.feature.id);
    pl.setTooltipContent(_overlapTooltipHtml(here, f));
  }});
  var decorator = L.polylineDecorator(pl, {{
    patterns: [{{
      offset: 20, repeat: 80,
      symbol: L.Symbol.arrowHead({{
        pixelSize: 9, headAngle: 40,
        pathOptions: {{color: '#333', fillOpacity: 0.8, weight: 0, fillColor: '#333', interactive: false}}
      }})
    }}]
  }}).addTo(map);
  pathObjs.push({{feature: f, polyline: pl, decorator: decorator, detailHtml: detailHtml}});
}});
map.on('click', function() {{ selectPath(null); }});

/* -- Suspicious-path styling ----------------------------------------- */
// Baseline dashed outline for low-point paths so they stand out even before
// you click anything — the whole point of a systematic review is spotting
// these without having to hover every single one.
pathObjs.forEach(function(o) {{
  if (o.feature.suspicious) o.polyline.setStyle({{dashArray: '2,7'}});
}});

/* -- Duplicate-registration visibility --------------------------------- */
// The "dropped" side of a confirmed duplicate is still drawn (so it can be
// selected/searched/inspected), just not shown on load — otherwise two
// near-identical lines sit exactly on top of each other with no visual cue
// that anything is duplicated. The "Show duplicates" toggle reveals them all
// at once; selecting one directly (click, search, step) reveals just that one
// regardless of the toggle, so it's never possible to select something
// invisible.
var HAS_DROPPED_DUPLICATES = pathObjs.some(function(o) {{ return o.feature.dup_dropped; }});
if (!SHOW_DUPLICATES) {{
  pathObjs.forEach(function(o) {{
    if (o.feature.dup_dropped) {{ map.removeLayer(o.polyline); map.removeLayer(o.decorator); }}
  }});
}}
function _ensureVisible(o) {{
  if (o && !map.hasLayer(o.polyline)) {{ o.polyline.addTo(map); o.decorator.addTo(map); }}
}}

/* -- Sequential step-through ------------------------------------------ */
// Walking every path in a fixed order — not just whatever happens to be
// stacked under a click — is how you actually get through several hundred
// of them systematically instead of only sampling wherever you clicked.
//
// Cycling alphabetically through all 496 is useless once you've actually
// flagged a handful worth revisiting — the next one alphabetically is
// probably nowhere near the last, geographically. "Cycle flagged only"
// narrows Prev/Next to just the paths you've marked, in whichever order
// still makes sense once the set is small.
var CYCLE_FLAGGED_ONLY = false;
function stepList() {{
  if (CYCLE_FLAGGED_ONLY && Object.keys(FLAGS).filter(function(id) {{ return FLAGS[id] && FLAGS[id].status; }}).length === 0) {{
    CYCLE_FLAGGED_ONLY = false;   // nothing left to cycle through — fall back automatically
  }}
  // While the flagged list itself is open, Prev/Next stepping through all ~500
  // paths would make the bold-current-row indicator basically never light up
  // — showingFlagList forces the same scoping as the checkbox, whether or not
  // it's ticked.
  var useFlaggedOnly = CYCLE_FLAGGED_ONLY || showingFlagList;
  var base = useFlaggedOnly
    ? pathObjs.filter(function(o) {{ return FLAGS[o.feature.id] && FLAGS[o.feature.id].status; }})
    : pathObjs.filter(function(o) {{ return !SUSPICIOUS_ONLY || o.feature.suspicious; }});
  var list = base.map(function(o) {{ return o.feature; }});
  list.sort(function(a, b) {{ return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; }});
  return list;
}}
function stepSelect(id) {{
  _applySelectionAndEndpoints(id);
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
  if (obj) map.fitBounds(obj.polyline.getBounds(), {{padding: [60, 60], maxZoom: 16}});
}}
function stepTo(delta) {{
  var list = stepList();
  if (!list.length) return;
  var idx = list.findIndex(function(f) {{ return f.id === selectedId; }});
  idx = idx === -1 ? 0 : (idx + delta + list.length) % list.length;
  stepSelect(list[idx].id);
}}

/* -- "Duplicate paths" / "Same name, needs manual check" cards: click to jump
   straight to one -- there's no other way to tell which path(s) they refer to
   without hunting through the console output. Clicking selects the first path
   (the "kept" one, for confirmed duplicates — the dropped copy stays visible
   too once selected, regardless of the "Show duplicates" toggle) of the first
   matching group and flies the map to it, same as clicking a path directly, so
   its warning shows in the inspector panel. Cycles through groups on repeat
   clicks, in case more than one exists. */
function _wireStatCard(cardId, confirmed) {{
  var card = document.getElementById(cardId);
  if (!card) return;
  var groups = Object.keys(DUPLICATE_GROUPS)
    .filter(function(name) {{ return !!DUPLICATE_GROUPS[name].confirmed === confirmed; }})
    .map(function(name) {{ return DUPLICATE_GROUPS[name].ids[0]; }});
  var i = 0;
  card.addEventListener('click', function() {{
    if (!groups.length) return;
    stepSelect(groups[i % groups.length]);
    i++;
  }});
}}
_wireStatCard('dupCard', true);
_wireStatCard('ambiguousCard', false);

/* -- Reversed-direction check cards: same click-to-jump pattern, but also
   switches on the category-colour layer so the group being jumped to is
   immediately visible in context, not just selected. */
function _wireReversedCard(cardId, category) {{
  var card = document.getElementById(cardId);
  if (!card) return;
  var groups = Object.keys(REVERSED_GROUPS)
    .filter(function(key) {{ return REVERSED_GROUPS[key].category === category; }})
    .map(function(key) {{ return REVERSED_GROUPS[key].ids[0]; }});
  var i = 0;
  card.addEventListener('click', function() {{
    if (!groups.length) return;
    if (!REVERSED_MODE) {{
      REVERSED_MODE = true;
      var btn = document.querySelector('.sensor-toggle-btn.reversed');
      if (btn) {{ btn.innerHTML = 'Reversed check: ON'; btn.classList.remove('off'); }}
    }}
    stepSelect(groups[i % groups.length]);
    i++;
  }});
}}
_wireReversedCard('revBugCard', 'bug');
_wireReversedCard('revAmbCard', 'ambiguous');
_wireReversedCard('revOkCard', 'ok');

/* -- Flagging + export (QA_MODE only — see the QA_MODE comment above) --- */
if (QA_MODE) {{
var flagLayer = L.layerGroup().addTo(map);
var flagMarkers = {{}};
function saveFlags() {{ localStorage.setItem('btPathFlags', JSON.stringify(FLAGS)); }}

// A bare emoji has no background of its own, so a red flag glyph disappears
// against a red (issue-coloured) path underneath it. A solid-fill teardrop
// pin — different colour, different silhouette from both the lines and the
// round A/B endpoint markers, tip anchored on the path rather than centred
// on it — stays legible regardless of what's under it.
function _flagIcon(status) {{
  var color = status === 'flag' ? '#c0392b' : '#1d9e75';
  var symbol = status === 'flag' ? '!' : '&#10003;';
  return L.divIcon({{
    className: '',
    html: '<div style="width:22px;height:22px;border-radius:50% 50% 50% 0;transform:rotate(-45deg);' +
          'background:' + color + ';border:2.5px solid #fff;box-shadow:0 1px 5px rgba(0,0,0,.5);' +
          'display:flex;align-items:center;justify-content:center">' +
          '<span style="transform:rotate(45deg);font-size:12px;color:#fff;font-weight:800;' +
          'font-family:sans-serif">' + symbol + '</span></div>',
    iconSize: [22, 26], iconAnchor: [11, 26]
  }});
}}

function updateFlagMarker(id) {{
  if (flagMarkers[id]) {{ flagLayer.removeLayer(flagMarkers[id]); delete flagMarkers[id]; }}
  var status = (FLAGS[id] || {{}}).status;
  if (!status) return;
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
  if (!obj || !obj.polyline._coords.length) return;
  var mid = obj.polyline._coords[Math.floor(obj.polyline._coords.length / 2)];
  var m = L.marker(mid, {{icon: _flagIcon(status), interactive: true, zIndexOffset: 1000}});
  m.bindTooltip((status === 'flag' ? 'Flagged' : 'OK’d') + ': ' + obj.feature.name,
                {{direction: 'top', opacity: 0.95}});
  m.on('click', function(e) {{ L.DomEvent.stopPropagation(e); stepSelect(id); }});
  m.addTo(flagLayer);
  flagMarkers[id] = m;
}}
function setFlag(id, status) {{
  if (!FLAGS[id]) FLAGS[id] = {{}};
  FLAGS[id].status = (FLAGS[id].status === status) ? '' : status;
  saveFlags();
  updateFlagMarker(id);
  renderInspector();
}}
function setNote(id, note) {{
  if (!FLAGS[id]) FLAGS[id] = {{}};
  FLAGS[id].note = note;
  saveFlags();
}}
// Prune flags for paths retired from the API (e.g. removed as duplicates) —
// otherwise they'd linger forever as dead entries: unclickable in the list
// (nothing left in pathObjs to select) and shown by bare ID since the name
// lookup fails too.
(function _pruneOrphanedFlags() {{
  var knownIds = {{}};
  pathObjs.forEach(function(o) {{ knownIds[o.feature.id] = true; }});
  var changed = false;
  Object.keys(FLAGS).forEach(function(id) {{
    if (!knownIds[id]) {{ delete FLAGS[id]; changed = true; }}
  }});
  if (changed) saveFlags();
}})();
Object.keys(FLAGS).forEach(updateFlagMarker);

function exportFindings() {{
  var rows = [['Path ID', 'Name', 'Status', 'Note', 'Suspicious', 'Duplicate']];
  Object.keys(FLAGS).forEach(function(id) {{
    var flag = FLAGS[id];
    if (!flag || flag.status !== 'flag') return;
    var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
    var name = obj ? obj.feature.name : id;
    var susp = obj && obj.feature.suspicious ? 'yes' : 'no';
    var dup = DUPLICATE_GROUPS[name] ? 'yes' : 'no';
    rows.push([id, name, flag.status || '', (flag.note || '').replace(/\\n/g, ' '), susp, dup]);
  }});
  if (rows.length === 1) {{ alert('No flagged paths yet — use Flag in the panel at bottom-left first.'); return; }}
  var csv = rows.map(function(r) {{
    return r.map(function(v) {{ return '"' + String(v).replace(/"/g, '""') + '"'; }}).join(',');
  }}).join('\\n');
  var blob = new Blob([csv], {{type: 'text/csv'}});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'bt_path_review_findings.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}}

// Minimal RFC4180-ish CSV parser — handles quoted fields, escaped quotes
// (""), and both CRLF and LF line endings, matching what exportFindings()
// itself writes (and what Excel/Sheets produce when re-saving that file).
function _parseCsv(text) {{
  var rows = [], row = [], field = '', inQuotes = false;
  for (var i = 0; i < text.length; i++) {{
    var c = text[i];
    if (inQuotes) {{
      if (c === '"') {{
        if (text[i + 1] === '"') {{ field += '"'; i++; }} else {{ inQuotes = false; }}
      }} else {{
        field += c;
      }}
    }} else if (c === '"') {{
      inQuotes = true;
    }} else if (c === ',') {{
      row.push(field); field = '';
    }} else if (c === '\\r') {{
      // skip; \\n handles the line break
    }} else if (c === '\\n') {{
      row.push(field); rows.push(row); row = []; field = '';
    }} else {{
      field += c;
    }}
  }}
  if (field.length || row.length) {{ row.push(field); rows.push(row); }}
  return rows;
}}

// Re-imports a CSV from exportFindings() (this tool's own, or one edited in
// Excel/Sheets) — the only way findings survive a different browser, a
// different machine, or localStorage getting cleared. Merges by Path ID:
// rows in the file overwrite that path's status/note, anything already
// flagged locally but absent from the file is left alone.
function importFindings(file) {{
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function() {{
    var rows = _parseCsv(String(reader.result));
    if (!rows.length) {{ alert("That file looks empty."); return; }}
    var header = rows[0].map(function(h) {{ return h.trim().toLowerCase(); }});
    var idIdx = header.indexOf('path id');
    var statusIdx = header.indexOf('status');
    var noteIdx = header.indexOf('note');
    if (idIdx === -1) {{
      alert("That doesn't look like a findings CSV exported from this tool — no \\"Path ID\\" column.");
      return;
    }}
    var imported = 0;
    for (var r = 1; r < rows.length; r++) {{
      var row = rows[r];
      var id = row[idIdx];
      if (!id) continue;
      var status = statusIdx !== -1 ? row[statusIdx] : '';
      var note = noteIdx !== -1 ? row[noteIdx] : '';
      if (!status && !note) continue;
      FLAGS[id] = {{status: status || '', note: note || ''}};
      updateFlagMarker(id);
      imported++;
    }}
    saveFlags();
    renderInspector();
    alert("Imported " + imported + " path finding(s).");
  }};
  reader.readAsText(file);
}}
}} // end if (QA_MODE) — flagging + export

/* -- Inspector panel: selection detail, suspicious badge, flag/note ---- */
var insBody, insCounter;
(function() {{
  var wrap = L.DomUtil.create('div', 'inspector', map.getContainer());
  L.DomEvent.disableClickPropagation(wrap);
  L.DomEvent.disableScrollPropagation(wrap);
  var hd = L.DomUtil.create('div', 'inspector-hd', wrap);
  hd.appendChild(document.createTextNode('Path review'));
  var nav = L.DomUtil.create('div', 'nav', hd);
  var prevBtn = L.DomUtil.create('button', '', nav);
  prevBtn.innerHTML = '&#9664;'; prevBtn.title = 'Previous path';
  insCounter = L.DomUtil.create('span', '', nav);
  var nextBtn = L.DomUtil.create('button', '', nav);
  nextBtn.innerHTML = '&#9654;'; nextBtn.title = 'Next path';
  prevBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); stepTo(-1); }};
  nextBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); stepTo(1); }};

  // Drag the panel by its header — it starts pinned bottom-left, but a
  // reviewer working a dense overlap cluster often needs it out of the way
  // of whatever part of the map they're currently looking at.
  hd.addEventListener('mousedown', function(e) {{
    if (e.target.closest('.nav')) return;
    e.preventDefault();
    var mapRect = map.getContainer().getBoundingClientRect();
    var wrapRect = wrap.getBoundingClientRect();
    var offsetX = e.clientX - wrapRect.left;
    var offsetY = e.clientY - wrapRect.top;
    wrap.style.left = (wrapRect.left - mapRect.left) + 'px';
    wrap.style.top = (wrapRect.top - mapRect.top) + 'px';
    wrap.style.bottom = 'auto';
    function onMove(ev) {{
      var x = ev.clientX - mapRect.left - offsetX;
      var y = ev.clientY - mapRect.top - offsetY;
      x = Math.max(0, Math.min(x, mapRect.width - wrapRect.width));
      y = Math.max(0, Math.min(y, mapRect.height - wrapRect.height));
      wrap.style.left = x + 'px';
      wrap.style.top = y + 'px';
    }}
    function onUp() {{
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }}
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }});

  insBody = L.DomUtil.create('div', 'inspector-body', wrap);
}})();

// Once you've flagged/OK'd a path, clicking around to find it again isn't
// realistic across ~500 paths — this is the way back to any of them.
var showingFlagList = false;
function _flaggedList() {{
  return Object.keys(FLAGS)
    .filter(function(id) {{ return FLAGS[id] && FLAGS[id].status; }})
    .map(function(id) {{
      var obj = pathObjs.filter(function(p) {{ return p.feature.id === id; }})[0];
      return {{id: id, name: obj ? obj.feature.name : id, status: FLAGS[id].status}};
    }})
    .sort(function(a, b) {{ return a.name < b.name ? -1 : a.name > b.name ? 1 : 0; }});
}}
function _flagSummaryHtml() {{
  var flagged = _flaggedList();
  if (!flagged.length) return '';
  var flagCount = flagged.filter(function(x) {{ return x.status === 'flag'; }}).length;
  var okCount = flagged.filter(function(x) {{ return x.status === 'ok'; }}).length;
  return '<div class="flag-summary">' +
    (flagCount ? '&#128681;' + flagCount + ' ' : '') + (okCount ? '&#9989;' + okCount + ' ' : '') +
    '<label class="flag-cycle-toggle"><input type="checkbox" id="cycleFlaggedChk"' +
      (CYCLE_FLAGGED_ONLY || showingFlagList ? ' checked' : '') + (showingFlagList ? ' disabled' : '') +
      '> Cycle flagged only</label>' +
    '<span class="flag-summary-link" id="flagSummaryToggle">' + (showingFlagList ? 'Hide list' : 'View list') + '</span>' +
    '</div>';
}}
function _wireFlagSummaryToggle() {{
  var link = document.getElementById('flagSummaryToggle');
  if (link) link.onclick = function(e) {{ L.DomEvent.stopPropagation(e); showingFlagList = !showingFlagList; renderInspector(); }};
  var chk = document.getElementById('cycleFlaggedChk');
  if (chk) {{
    chk.onclick = function(e) {{ L.DomEvent.stopPropagation(e); }};
    chk.onchange = function() {{ CYCLE_FLAGGED_ONLY = chk.checked; renderInspector(); }};
  }}
}}

function renderInspector() {{
  var list = stepList();
  var flagSummary = _flagSummaryHtml();

  if (showingFlagList) {{
    var flagged = _flaggedList();
    // Bolds whichever path is currently selected, same as the overlap-list
    // tooltip does — stepping with &#9664; &#9654; while this list is open should
    // show which one you're now looking at, not just move the map underneath.
    var rows = flagged.map(function(x) {{
      var icon = x.status === 'flag' ? '&#128681;' : '&#9989;';
      var label = icon + ' ' + x.name + ' <span class="flag-list-id">(ID ' + x.id + ')</span>';
      if (x.id === selectedId) label = '<b>' + label + '</b>';
      return '<div class="flag-list-row" data-id="' + x.id + '">' + label + '</div>';
    }}).join('');
    insBody.innerHTML = flagSummary + (flagged.length ? rows : '<div class="empty">Nothing flagged yet.</div>');
    Array.prototype.forEach.call(insBody.querySelectorAll('.flag-list-row'), function(row) {{
      row.onclick = function(e) {{
        L.DomEvent.stopPropagation(e);
        stepSelect(row.getAttribute('data-id'));
      }};
    }});
    insCounter.textContent = flagged.length + ' flagged';
    _wireFlagSummaryToggle();
    return;
  }}

  if (selectedId === null) {{
    insBody.innerHTML = flagSummary + '<div class="empty">Click a path, or use &#9664; &#9654; to step through ' +
      (SUSPICIOUS_ONLY ? 'the ' + list.length + ' suspicious paths' : 'all ' + list.length + ' paths') + '.</div>';
    insCounter.textContent = list.length ? '0 / ' + list.length : '';
    _wireFlagSummaryToggle();
    return;
  }}
  var obj = pathObjs.filter(function(p) {{ return p.feature.id === selectedId; }})[0];
  if (!obj) return;
  var f = obj.feature;
  var idx = list.findIndex(function(x) {{ return x.id === selectedId; }});
  insCounter.textContent = idx === -1 ? '—' : (idx + 1) + ' / ' + list.length;

  var susp = f.suspicious
    ? '<span class="badge suspicious" title="Fewer than ' + SUSPICIOUS_MIN_POINTS + ' points">Suspicious</span>'
    : '';
  insBody.innerHTML = flagSummary + obj.detailHtml + susp +
    (QA_MODE ? '<div class="flag-row"><button id="flagBtn">&#128681; Flag</button><button id="okBtn">&#9989; OK</button></div>' +
    '<textarea id="insNote" placeholder="Notes for this path…"></textarea>' : '');

  if (QA_MODE) {{
    var flagState = (FLAGS[f.id] || {{}}).status || '';
    var flagBtn = document.getElementById('flagBtn'), okBtn = document.getElementById('okBtn');
    flagBtn.classList.toggle('active-flag', flagState === 'flag');
    okBtn.classList.toggle('active-ok', flagState === 'ok');
    // Both buttons replace insBody's contents (to refresh flag state), and that
    // DOM mutation mid-click can let the click "escape" disableClickPropagation
    // — stop it explicitly before triggering the refresh, or the click reaches
    // the map underneath and deselects the very path being flagged.
    flagBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); setFlag(f.id, 'flag'); }};
    okBtn.onclick = function(e) {{ L.DomEvent.stopPropagation(e); setFlag(f.id, 'ok'); }};
    var noteEl = document.getElementById('insNote');
    noteEl.value = (FLAGS[f.id] || {{}}).note || '';
    noteEl.onchange = function() {{ setNote(f.id, noteEl.value); }};
  }}
  _wireFlagSummaryToggle();
}}
renderInspector();

/* -- Bluetooth sensor inventory (API / local DB) -------------------- */
var sensorLayer = L.layerGroup();
var sensorMarkersById = {{}};
SENSORS.forEach(function(s) {{
  var m = L.circleMarker([s.lat, s.lon], {{
    radius: 6, color: '#fff', weight: 2, fillColor: '#1f4e79', fillOpacity: 0.95
  }});
  var rows = '<b>' + s.name + '</b><br>ID: ' + s.id;
  if (s.site_code) rows += '<br>Site: ' + s.site_code;
  rows += '<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5);
  m.bindPopup(rows);
  m.addTo(sensorLayer);
  sensorMarkersById[s.id] = m;
}});
// Off by default — these two inventory overlays are diagnostic detail, not
// what most visits to this map need to see first.

function makeDiamondIcon(color) {{
  return L.divIcon({{
    className: '',
    html: '<div style="width:12px;height:12px;background:' + color + ';border:2px solid #fff;' +
          'box-shadow:0 1px 3px rgba(0,0,0,.4);transform:rotate(45deg)"></div>',
    iconSize: [12, 12], iconAnchor: [6, 6]
  }});
}}

/* -- Bluetooth sensor inventory (spreadsheet / reference) ----------- */
var COMMISSIONING_LABEL = {{not_electrified: 'Awaiting power', decommissioned: 'Decommissioned'}};
var refLayer = L.layerGroup();
var refMarkersByIdx = [];
REF_SENSORS.forEach(function(s, idx) {{
  var m = L.marker([s.lat, s.lon], {{icon: makeDiamondIcon('#8e44ad')}});
  var rows = '<b>' + s.name + '</b><br>Spreadsheet location';
  rows += '<br>Project: ' + (s.project || '<span style="color:#999">none listed</span>');
  var statusLabel = COMMISSIONING_LABEL[s.commissioning];
  if (statusLabel) rows += '<br>Status: ' + statusLabel;
  rows += '<br>' + s.lat.toFixed(5) + ', ' + s.lon.toFixed(5);
  m.bindPopup(rows);
  m.addTo(refLayer);
  refMarkersByIdx[idx] = m;
}});
// Off by default — see sensorLayer above.

function makeToggleBtn(label, layer, extraClass) {{
  var btn = L.DomUtil.create('button', 'sensor-toggle-btn off' + (extraClass ? ' ' + extraClass : ''), filtersBody);
  btn.innerHTML = label + ': OFF';
  btn.onclick = function() {{
    if (map.hasLayer(layer)) {{
      map.removeLayer(layer);
      btn.innerHTML = label + ': OFF';
      btn.classList.add('off');
    }} else {{
      layer.addTo(map);
      btn.innerHTML = label + ': ON';
      btn.classList.remove('off');
    }}
  }};
  return btn;
}}
var apiToggleCtl = makeToggleBtn('API sensors', sensorLayer);
var refToggleCtl = makeToggleBtn('Spreadsheet sensors', refLayer, 'ref');

(function() {{
  var btn = L.DomUtil.create('button', 'sensor-toggle-btn susp', filtersBody);
  btn.innerHTML = 'Suspicious only: OFF';
  btn.onclick = function(e) {{
    L.DomEvent.stopPropagation(e);
    SUSPICIOUS_ONLY = !SUSPICIOUS_ONLY;
    btn.innerHTML = 'Suspicious only: ' + (SUSPICIOUS_ONLY ? 'ON' : 'OFF');
    btn.classList.toggle('active', SUSPICIOUS_ONLY);
    pathObjs.forEach(function(o) {{
      var show = !SUSPICIOUS_ONLY || o.feature.suspicious;
      var has = map.hasLayer(o.polyline);
      if (show && !has) {{ o.polyline.addTo(map); o.decorator.addTo(map); }}
      if (!show && has) {{ map.removeLayer(o.polyline); map.removeLayer(o.decorator); }}
    }});
    renderInspector();
  }};
}})();

if (HAS_DROPPED_DUPLICATES) {{
  (function() {{
    var btn = L.DomUtil.create('button', 'sensor-toggle-btn dup' + (SHOW_DUPLICATES ? '' : ' off'), filtersBody);
    btn.innerHTML = 'Show duplicates: ' + (SHOW_DUPLICATES ? 'ON' : 'OFF');
    btn.onclick = function(e) {{
      L.DomEvent.stopPropagation(e);
      SHOW_DUPLICATES = !SHOW_DUPLICATES;
      btn.innerHTML = 'Show duplicates: ' + (SHOW_DUPLICATES ? 'ON' : 'OFF');
      btn.classList.toggle('off', !SHOW_DUPLICATES);
      pathObjs.forEach(function(o) {{
        if (!o.feature.dup_dropped) return;
        var has = map.hasLayer(o.polyline);
        if (SHOW_DUPLICATES && !has) {{ o.polyline.addTo(map); o.decorator.addTo(map); }}
        if (!SHOW_DUPLICATES && has) {{ map.removeLayer(o.polyline); map.removeLayer(o.decorator); }}
      }});
      renderInspector();
    }};
  }})();
}}

var HAS_REVERSED_CHECK = pathObjs.some(function(o) {{ return !!o.feature.reversed_check; }});
if (HAS_REVERSED_CHECK) {{
  (function() {{
    var btn = L.DomUtil.create('button', 'sensor-toggle-btn reversed off', filtersBody);
    btn.innerHTML = 'Reversed check: OFF';
    btn.onclick = function(e) {{
      L.DomEvent.stopPropagation(e);
      REVERSED_MODE = !REVERSED_MODE;
      btn.innerHTML = 'Reversed check: ' + (REVERSED_MODE ? 'ON' : 'OFF');
      btn.classList.toggle('off', !REVERSED_MODE);
      applySelection();
    }};
  }})();
}}

var LegendControl = L.Control.extend({{
  options: {{position: 'bottomright'}},
  onAdd: function() {{
    var box = L.DomUtil.create('div', 'legend-ctl');
    L.DomEvent.disableClickPropagation(box);
    var reversedRows = HAS_REVERSED_CHECK ?
      '<div class="legend-sec">' +
        '<div class="legend-sec-title">Reversed-direction check<br>(when toggle is on)</div>' +
        '<div class="legend-row"><span class="legend-swatch"><span class="legend-line" style="border-color:' + REVERSED_COLORS.bug + '"></span></span>Bug — direction reversed</div>' +
        '<div class="legend-row"><span class="legend-swatch"><span class="legend-line" style="border-color:' + REVERSED_COLORS.ambiguous + '"></span></span>Ambiguous — needs a look</div>' +
        '<div class="legend-row"><span class="legend-swatch"><span class="legend-line" style="border-color:' + REVERSED_COLORS.ok + '"></span></span>OK — direction confirmed</div>' +
      '</div>' : '';
    box.innerHTML =
      '<div class="legend-hd" id="legendHd">Legend <span class="chev">&#9660;</span></div>' +
      '<div class="legend-body">' +
        '<div class="legend-sec">' +
          '<div class="legend-sec-title">Sensors</div>' +
          '<div class="legend-row"><span class="legend-swatch"><span class="legend-dot" style="background:#1f4e79"></span></span>API sensor</div>' +
          '<div class="legend-row"><span class="legend-swatch"><span class="legend-diamond" style="background:#8e44ad"></span></span>Spreadsheet sensor</div>' +
        '</div>' +
        '<div class="legend-sec">' +
          '<div class="legend-sec-title">Paths</div>' +
          '<div class="legend-row"><span class="legend-swatch"><span class="legend-line" style="border-color:#3b82c4"></span></span>Each path has its own color</div>' +
          '<div class="legend-row"><span class="legend-swatch"><span class="legend-line dashed" style="border-color:#3b82c4"></span></span>Suspicious (&lt;' + SUSPICIOUS_MIN_POINTS + ' points)</div>' +
          '<div class="legend-row"><span class="legend-swatch"><span class="legend-line" style="border-color:' + SELECTED_COLOR + '"></span></span>Selected path</div>' +
        '</div>' +
        reversedRows +
      '</div>';
    var hd = box.querySelector('#legendHd');
    hd.onclick = function() {{ box.classList.toggle('collapsed'); }};
    return box;
  }}
}});
map.addControl(new LegendControl());

if (QA_MODE) {{
  var exportBtn = L.DomUtil.create('button', 'sensor-toggle-btn export', filtersBody);
  exportBtn.innerHTML = 'Export findings &#8659;';
  exportBtn.onclick = exportFindings;

  var importWrap = L.DomUtil.create('div', '', filtersBody);
  var importBtn = L.DomUtil.create('button', 'sensor-toggle-btn import', importWrap);
  importBtn.innerHTML = 'Import findings &#8657;';
  var fileInput = L.DomUtil.create('input', '', importWrap);
  fileInput.type = 'file';
  fileInput.accept = '.csv';
  fileInput.style.display = 'none';
  importBtn.onclick = function() {{ fileInput.click(); }};
  fileInput.onchange = function() {{
    importFindings(fileInput.files[0]);
    fileInput.value = '';   // allow re-importing the same file
  }};
}}

// Turns a toggleable layer back on (and syncs its button) if search jumps to
// something currently hidden — otherwise the marker/popup a search result
// points at wouldn't actually be visible.
function showLayer(layer, btn, label) {{
  if (map.hasLayer(layer)) return;
  layer.addTo(map);
  btn.innerHTML = label + ': ON';
  btn.classList.remove('off');
  var panel = btn.closest('.filters-ctl');
  if (panel) panel.classList.remove('collapsed');
}}

/* -- Search: paths + both sensor inventories ------------------------ */
var SEARCH_INDEX = [];
FEATURES.forEach(function(f) {{
  SEARCH_INDEX.push({{type: 'Path', label: f.name, sub: 'ID ' + f.id, kind: 'path', id: f.id}});
}});
SENSORS.forEach(function(s) {{
  SEARCH_INDEX.push({{type: 'API sensor', label: s.name, sub: 'ID ' + s.id, kind: 'api', id: s.id}});
}});
REF_SENSORS.forEach(function(s, idx) {{
  SEARCH_INDEX.push({{type: 'Spreadsheet sensor', label: s.name, sub: '', kind: 'ref', idx: idx}});
}});

function goToSearchResult(item) {{
  if (item.kind === 'path') {{
    var o = pathObjs.filter(function(p) {{ return p.feature.id === item.id; }})[0];
    if (!o) return;
    selectPath(item.id);
    map.fitBounds(o.polyline.getBounds(), {{padding: [40, 40], maxZoom: 16}});
  }} else if (item.kind === 'api') {{
    showLayer(sensorLayer, apiToggleCtl, 'API sensors');
    var m = sensorMarkersById[item.id];
    map.setView(m.getLatLng(), 16);
    m.openPopup();
  }} else {{
    showLayer(refLayer, refToggleCtl, 'Spreadsheet sensors');
    var m2 = refMarkersByIdx[item.idx];
    map.setView(m2.getLatLng(), 16);
    m2.openPopup();
  }}
}}

(function() {{
    // Built as a plain overlay appended directly to the map container, not a
    // corner L.Control — Leaflet's corner boxes shrink-wrap to their content,
    // so a CSS left:50% inside one centers on its own tiny width, not the
    // map's. Appending straight to the map container (position:relative,
    // full map width) makes centering work correctly.
    var wrap = L.DomUtil.create('div', 'search-ctl', map.getContainer());
    L.DomEvent.disableClickPropagation(wrap);
    L.DomEvent.disableScrollPropagation(wrap);
    var input = L.DomUtil.create('input', '', wrap);
    input.type = 'text';
    input.placeholder = 'Search paths or sensors…';
    var results = L.DomUtil.create('div', 'search-results', wrap);

    function render(matches, query) {{
      results.innerHTML = '';
      if (!query) {{ results.classList.remove('visible'); return; }}
      if (!matches.length) {{
        results.innerHTML = '<div class="search-empty">No matches</div>';
      }} else {{
        matches.slice(0, 25).forEach(function(item) {{
          var row = L.DomUtil.create('div', 'search-result', results);
          row.innerHTML = '<div class="type">' + item.type + '</div>' + item.label +
                           (item.sub ? ' <span style="color:#999">(' + item.sub + ')</span>' : '');
          row.onclick = function() {{
            goToSearchResult(item);
            results.classList.remove('visible');
            input.value = item.label;
          }};
        }});
      }}
      results.classList.add('visible');
    }}

    input.addEventListener('input', function() {{
      var q = input.value.trim().toLowerCase();
      if (!q) {{ render([], ''); return; }}
      var matches = SEARCH_INDEX.filter(function(item) {{
        return item.label.toLowerCase().indexOf(q) !== -1 || item.sub.toLowerCase().indexOf(q) !== -1;
      }});
      render(matches, q);
    }});
}})();

</script>
</body>
</html>"""
    return page


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPORT_DIR / "bt_paths_map.html"))
    ap.add_argument("--show-duplicates", action="store_true",
                     help="Start with dropped duplicate registrations visible on load, instead "
                          "of hidden behind the 'Show duplicates' toggle — useful for eyeballing "
                          "them on the map before removing any from the API.")
    ap.add_argument("--publish", action="store_true",
                     help="Also write a copy to docs/bt-paths-map.html, the file served by "
                          "GitHub Pages for sharing this tool with colleagues.")
    args = ap.parse_args()

    paths = fetch_bt_path_coords()

    keep_ids, collapsed, ambiguous = find_duplicate_groups(paths)
    dropped_ids = {pid for _, _, dropped, _ in collapsed for pid in dropped}
    if collapsed:
        print(f"Found {len(collapsed)} duplicate path registration(s) - dropped copies stay on "
              f"the map, hidden behind the 'Show duplicates' toggle:")
        for name, kept, dropped, pct in collapsed:
            print(f"  {name}: kept {kept}, dropped {', '.join(dropped)}  (status match {pct*100:.0f}%)")
    if ambiguous:
        print(f"{len(ambiguous)} same-name group(s) left as-is — status diverges, needs manual check:")
        for name, ids, pct in ambiguous:
            print(f"  {name}: ids {', '.join(ids)}  (status match {pct*100:.0f}%)")

    duplicate_groups = {}
    for name, kept, dropped, pct in collapsed:
        duplicate_groups[name] = {"ids": [kept] + dropped, "match_pct": pct, "confirmed": True}
    for name, ids, pct in ambiguous:
        duplicate_groups[name] = {"ids": ids, "match_pct": pct, "confirmed": False}

    reversed_by_id, reversed_groups = find_reversed_direction_pairs(paths)
    if reversed_groups:
        bug_n = sum(1 for v in reversed_groups.values() if v['category'] == 'bug')
        amb_n = sum(1 for v in reversed_groups.values() if v['category'] == 'ambiguous')
        ok_n = sum(1 for v in reversed_groups.values() if v['category'] == 'ok')
        print(f"Reversed-direction check: {bug_n} bug, {amb_n} ambiguous, {ok_n} ok "
              f"(of {len(reversed_groups)} named reverse pairs)")

    sensors = fetch_sensor_coords().get("Bluetooth", {})
    ref_sensors = load_reference_sensors()
    page = build_html(paths, sensors, ref_sensors, duplicate_groups,
                       dropped_ids=dropped_ids, show_dropped_by_default=args.show_duplicates,
                       reversed_by_id=reversed_by_id, reversed_groups=reversed_groups)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    active_count = len(paths) - len(dropped_ids)
    print(f"Bluetooth paths map written to {out_path}  "
          f"({active_count} active paths, {len(dropped_ids)} duplicate(s) hidden by default, "
          f"{len(sensors)} API sensors, {len(ref_sensors)} spreadsheet sensors)")

    if args.publish:
        published_page = build_html(paths, sensors, ref_sensors, duplicate_groups,
                                     dropped_ids=dropped_ids, show_dropped_by_default=args.show_duplicates,
                                     include_live_data=False,
                                     reversed_by_id=reversed_by_id, reversed_groups=reversed_groups)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        published_path = DOCS_DIR / "bt-paths-map.html"
        published_path.write_text(published_page, encoding="utf-8")
        print(f"Published copy written to {published_path} (no live speed/travel-time data) — "
              f"commit and push to update the shared page.")


if __name__ == "__main__":
    main()
