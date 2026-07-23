#!/usr/bin/env python3
"""
update_projects.py — Refresh sensor -> project ownership in the local DB.

Run this after you have edited "QA Locations.xlsx" (or after new sensors have
been added to the API and recorded by a test run). It re-runs qa.py's
coordinate matching for every sensor group against the coordinates already in
results/history.db (no live API call, no network) and writes the result into
the sensor_projects table.

The dashboard (report.py) reads that table, so once you commit results/history.db
and push, the next automated report on GitHub shows the updated ownership —
including the "Attention needed, by project" rollup.

Usage:
    python runner/update_projects.py
    (or double-click update_projects.bat)

This only touches the sensor_projects table. It does not run the API tests and
does not generate any HTML.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from qa import (
    load_api_sensors,
    load_reference,
    match_sensors,
    load_project_accountability,
    annotate_accountability,
)
from db import upsert_sensor_projects

REPO_ROOT = Path(__file__).parent.parent
WORKBOOK = REPO_ROOT / "QA Locations.xlsx"

# One row per sensor group: (DB group name, sheet in the workbook, max match
# distance in metres or None for the default). Add a line here if a new group
# with a reference sheet is introduced.
GROUPS = [
    ("Traffic Detection", "Traffic Detection", 300),
    ("Bluetooth",         "Bluetooth",         300),
    # VMS sit far apart (median nearest-neighbour 1.45 km) and their reference
    # coordinates are approximate, so 300m strands signs that are unambiguously
    # the same installation — "VMS A1" was 393m from the API sign of that exact
    # name, with the runner-up 919m away. 500m closes that without introducing a
    # single re-assignment. Detection loops stay at 300m: they are dense enough
    # in Nicosia that a wider radius would pull in a different junction.
    ("VMS",               "VMS",               500),
]


def main():
    if not WORKBOOK.exists():
        raise SystemExit(
            f"ERROR: {WORKBOOK.name} not found at {WORKBOOK}.\n"
            f"       This file is kept out of git — copy it into the repo root "
            f"from your local/cloud store before running."
        )

    project_acct = load_project_accountability()
    print(f"Updating project assignments from {WORKBOOK.name} …\n")

    total_matched = total_api = 0
    rows = []
    for group, sheet, max_dist in GROUPS:
        api_sensors = load_api_sensors(group)
        if not api_sensors:
            rows.append((group, "no API sensors in DB — run the tests first"))
            continue

        ref_sensors, _not_electrified = load_reference([f"{WORKBOOK}::{sheet}"])
        matches = match_sensors(ref_sensors, api_sensors, max_dist=max_dist)
        annotate_accountability(api_sensors, matches, project_acct, max_dist=max_dist)

        # Persist every API sensor: matched ones carry their project, the rest
        # get project=None so a removed/edited assignment is cleared, not stale.
        to_persist = {s["id"]: {"project": s.get("project"), "source": s.get("project_source"),
                                "commissioning": s.get("commissioning", "active")}
                      for s in api_sensors}
        upsert_sensor_projects(group, to_persist)

        matched = sum(1 for v in to_persist.values() if v["project"])
        awaiting = sum(1 for v in to_persist.values() if v["commissioning"] == "not_electrified")
        total_matched += matched
        total_api += len(api_sensors)
        detail = f"{matched:>3} matched / {len(api_sensors):>3} API sensors"
        if awaiting:
            detail += f"  ({awaiting} awaiting power)"
        rows.append((group, detail))

    width = max(len(r[0]) for r in rows)
    for group, detail in rows:
        print(f"  {group.ljust(width)} :  {detail}")

    print(f"\nWrote sensor_projects -> {Path('results/history.db')}  "
          f"({total_matched}/{total_api} sensors assigned)")
    print("\nNext: commit results/history.db and push so the automated report picks it up.")


if __name__ == "__main__":
    main()
