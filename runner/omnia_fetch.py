"""
omnia_fetch.py - read the configured paths and logical nodes from OMNIA (SWARCO path manager).

Read-only: only GET requests. Each path there has a start and an end *logical node* (the OMNIA copy of a
Bluetooth sensor location) plus a min/max speed; `bt_wiring.py` compares those with the API sensors and the
drawn paths. The address is never stored in the code (the repo is public): it comes from the git-ignored
.env (see tools/control_room.py), and the fetched data is saved as results/omnia_snapshot.json (git-ignored) so
the check works offline afterwards.

Notes from exploring the service: nodes answer in milliseconds, but path-list can hang for 30 s or more, and
concurrent requests on one session make OMNIA abort - so requests are sent one at a time, with retries.

Node coordinates are Web Mercator metres (EPSG:3857), not degrees; `Latitude` is the y and `Longitude` the x.
"""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

PAGE_LIMIT = 1000
TIMEOUT_S = 60
RETRIES = 3
RETRY_WAIT_S = 5
EARTH_R = 6378137.0


class OmniaError(Exception):
    """A readable reason the fetch failed (never contains the address)."""


def mercator_to_wgs84(x, y):
    """(lat, lon) in degrees from Web Mercator metres."""
    lon = x / EARTH_R * 180 / math.pi
    lat = (2 * math.atan(math.exp(y / EARTH_R)) - math.pi / 2) * 180 / math.pi
    return lat, lon


def _get_json(get, base, name, sleep, timeout=TIMEOUT_S, retries=RETRIES, **params):
    last = "no answer"
    for attempt in range(1, retries + 1):
        try:
            resp = get(f"{base.rstrip('/')}/{name}", params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            last = f"HTTP {resp.status_code}"
        except Exception as exc:    # network down, timeout, bad JSON ...
            last = type(exc).__name__
        if attempt < retries:
            sleep(RETRY_WAIT_S)
    raise OmniaError(f"OMNIA did not answer {name} after {retries} tries ({last}). "
                     "Check you are on the internal network and try again.")


def _all_pages(get, base, name, key, total_key, sleep):
    rows, total = [], None
    while True:
        data = _get_json(get, base, name, sleep, page=len(rows) // PAGE_LIMIT + 1, start=len(rows), limit=PAGE_LIMIT)
        page = data.get(key)
        if not isinstance(page, list):
            raise OmniaError(f"OMNIA's answer to {name} had an unexpected shape.")
        rows += page
        total = data.get(total_key, len(rows))
        if not page or len(rows) >= (total or 0):
            return rows


def fetch_snapshot(base, get=None, sleep=time.sleep, now=None):
    """Nodes and paths as {fetched_at, nodes, paths}; raises OmniaError with a readable message."""
    if not base:
        raise OmniaError("No OMNIA address is set on this PC (CONTROL_ROOM_OMNIA_URL in .env).")
    if get is None:
        import httpx
        get = httpx.get
    raw_nodes = _all_pages(get, base, "node-list", "data", "totalCount", sleep)
    raw_paths = _all_pages(get, base, "path-list", "listPaths", "total", sleep)
    nodes = []
    for n in raw_nodes:
        lat, lon = mercator_to_wgs84(n["Longitude"], n["Latitude"])
        nodes.append({"id": n["Idlogicalnode"], "name": n.get("Description") or "",
                      "lat": round(lat, 6), "lon": round(lon, 6)})
    paths = [{"id": p["pathId"], "name": p.get("pathName") or "",
              "start": p.get("idLogicalNodeStart"), "end": p.get("idLogicalNodeEnd"),
              "speed_min": p.get("speedMin"), "speed_max": p.get("speedMax"), "length_km": p.get("pathLength")}
             for p in raw_paths]
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {"fetched_at": stamp, "nodes": nodes, "paths": paths}


def save_snapshot(snapshot, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_snapshot(path):
    """The saved snapshot, or None if there is none (or it cannot be read)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("nodes"), list) and isinstance(data.get("paths"), list)
    return data if ok else None
