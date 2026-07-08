# ITS Infrastructure Health Monitor

Automated health monitoring for Cyprus traffic infrastructure. Tests run every 6 hours via GitHub Actions, results are stored in SQLite, and a live HTML dashboard is published to GitHub Pages after every run.

---

## Live Dashboard

**[View the latest report here→](https://atsiakkaris.github.io/traffic-control-room/reports/latest.html)**

It updates automatically within ~1 minute of each scheduled run.

---

## What It Monitors

| Group | Endpoint | What is checked |
|---|---|---|
| **Traffic Detection** | [TD Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/TrafficMeasurementSiteTablePublication) | Valid XML |
| **Traffic Detection** | [TD Live](https://www.traffic4cyprus.org.cy/swarco3/api/Data/MeasuredDataPublication) | Feed freshness; sensor speed status (working / no traffic / malfunctioning / no data); average flow rate |
| **Bluetooth** | [BT Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/BTMeasurementSiteTablePublication) | Valid XML; total device count |
| **Bluetooth** | [BT Paths Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationPublication) | Valid XML; predefined path count |
| **Bluetooth** | [BT Paths Live (FCD)](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationDataPublication) | Feed freshness; speed and travel time per path |
| **VMS** | [VMS Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsTablePublication) | Valid XML |
| **VMS** | [VMS Live Data](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsPublication) | Feed freshness; working / not-working / no-status controller counts |

All live endpoints also check: HTTP 200 status, response time within limit, and feed freshness (data ≤ 5 min old).

---

## Health Model

The monitor uses a two-tier health model:

- **Feed health** (binary) — did the API respond with valid, fresh XML? If not, the group is marked as a feed issue regardless of sensor counts.
- **Sensor health %** — of the sensors/controllers/paths reported by the feed, what percentage are working? Shown as a percentage badge per group: green ≥ 90 %, amber ≥ 80 %, red < 80 %.

Each sensor in the Stability panel gets one of six badges based on its historical health %:

| Badge | Range | Meaning |
|---|---|---|
| Always on | 99–100% | Effectively every recorded run was good |
| Healthy | 90–98% | Consistently up, rare misses |
| Intermittent | 70–89% | Mostly working but with regular gaps |
| Unstable | 40–69% | Unreliable — failing more often than not |
| Critical | 1–39% | Almost always failing |
| Always off | 0% | No good runs recorded |

Sensors marked in the reference sheet as **awaiting power** (not yet electrified) or **decommissioned** are not expected to be working, so they get a neutral badge instead of a health tier and are **excluded from all health statistics** (see [Sensor Ownership & Commissioning](#sensor-ownership--commissioning)).

This means a group is never falsely marked "failed" just because some sensors are malfunctioning — the feed being up/down is tracked separately from individual sensor health.

---

## Sensor Ownership & Commissioning

The dashboard can show **who owns each sensor** (so a failure has someone to contact) and account for sensors that **aren't expected to be working yet**. This information comes from an external reference workbook, not from the API — the API is what we're validating, so ownership can't be derived from it.

**Source of truth: `QA Locations.xlsx`** (kept out of git — it holds the authority's equipment inventory). One sheet per group (Traffic Detection / Bluetooth / VMS), each row carrying a `Project` and a `Status` column. `config/projects.csv` maps each project to an accountability level (`supported` / `out_of_support`).

**How it reaches the dashboard.** `runner/qa.py` matches each API sensor to a reference row by geographic proximity and records the project + commissioning state. Because the dashboard reads the DB (never the spreadsheet), this is persisted to the `sensor_projects` table via a single command:

```
1. Edit QA Locations.xlsx locally
2. Run update_projects.bat        (matches against DB coords, offline — no API call)
3. git commit results/history.db && push
4. → the next automated report shows the updated ownership
```

**Commissioning states** (from the reference `Status` column) — all excluded from health statistics so they never drag the numbers down:

| Status in spreadsheet | Dashboard state | Treatment |
|---|---|---|
| `active` (or blank) | normal | Counted normally |
| `pending power connection`, `not electrified`, … | **Awaiting power** | Neutral badge, grey marker, excluded from stats; shown as e.g. *VMS: 4/4 working · 38 awaiting power* |
| `inactive`, `decommissioned`, `retired`, … | **Decommissioned** | Neutral badge, grey marker, excluded from stats |

Set a row back to `active` and re-run `update_projects.bat` to return the sensor to normal counting. Bluetooth *paths* are combinations of sensors (not owned equipment), so they carry no project.

---

## Dashboard Panels

**System Overview** — one card per group showing feed status and sensor health %, with a breakdown of each check. Each check includes a short description of what it tests. Hover the group badge (Operational / Deteriorated / Feed issue) to see colour thresholds and which test is driving the status. Hover individual endpoint dots to see a per-check pass/fail breakdown.

**Sensor Map** — interactive Leaflet.js map with:
- Colour-coded markers for Traffic Detection sensors, Bluetooth sites, and VMS controllers
- Marker clustering at country zoom (expands at zoom 13+); toggle clustering on/off with the **Cluster** button
- All predefined BT paths drawn as polylines (green = OK, red = issue, grey = no data), with directional arrows showing the direction of travel
- Toggle layers on/off; filter to issues only; collapsible legend
- **View on map** button on each group card isolates that group and flies to its bounds
- **Historical playback** bar — scrub or step through the last 30 runs to see how sensor statuses changed over time

The map pop-up for each sensor shows its **project owner** and, for sensors awaiting power or decommissioned, a neutral "Awaiting power" / "Decommissioned" status with a grey marker.

**Sensor Health Trend** — one line per sensor group showing health % across the last 30 runs.

**Attention needed, by project** — groups every sensor below 70 % uptime by the project/contractor that owns it, worst-count first, so a failure has an owner to contact. Out-of-support projects (where a failure is expected and non-actionable) are shown in a separate, clearly-marked section; sensors not matched to any project surface as *Unassigned — no owner known*. Awaiting-power and decommissioned sensors are excluded (they aren't faults).

**Sensor Stability** — per-sensor history table with sparklines (last 20 runs), stability badge, **project owner**, and timestamps for first seen / last working / last issue.
- Live search input to filter by sensor name or ID
- Group dropdown filter
- Sort dropdown — Default, Worst first, or Best first
- 📍 icon on each row — click to fly the map to that sensor or BT path and open its popup
- Expandable per-sensor daily health % trend chart (last 7 / 30 days)
- Awaiting-power / decommissioned sensors show a neutral badge and are left out of the panel's health bar

**Run History** — per-run feed status and sensor health % for each sensor group across the last 30 runs.

> The dashboard uses a **two-column layout** (60/40): the left column holds System Overview, Sensor Map, Health Trend, and Run History; the right column holds the Sensor Stability panel and stays sticky while you scroll. On narrow screens (below 900px) the columns stack vertically.

---

## Schedule

Tests run every 6 hours (triggered via cron-job.org). The workflow can also be triggered manually from the GitHub Actions tab.

A **weekly digest email** is sent every Monday at 07:30 Cyprus time (EEST). It summarises the past 7 days of sensor health, flagging always-off sensors, persistently unstable sensors, degraded/recovered sensors, and any sensors retired from the API feed.

---

## Project Structure

```
├── config/
│   ├── endpoints.yaml          ← Endpoint definitions and checks
│   ├── projects.csv            ← Project → accountability (supported / out_of_support)
│   └── ui_labels.yaml          ← UI label overrides and staleness threshold (rename panels, columns, groups without touching code)
├── runner/
│   ├── run_tests.py            ← Entry point
│   ├── tests.py                ← XML assertion logic per check type
│   ├── db.py                   ← SQLite helpers (schema, queries, migrations)
│   ├── geo.py                  ← Coordinate extraction from DATEX II inventory feeds
│   ├── report.py               ← HTML dashboard generator
│   ├── qa.py                   ← Reference-sheet ↔ API coordinate matching (ownership + commissioning)
│   ├── update_projects.py      ← Refresh sensor→project + commissioning in the DB after editing the workbook
│   └── digest.py               ← Weekly digest email builder and sender
├── results/
│   └── history.db              ← SQLite DB (auto-committed after each run)
├── reports/
│   └── latest.html             ← Generated dashboard (auto-committed after each run)
├── QA Locations.xlsx           ← Reference equipment inventory (gitignored — local/cloud only)
├── .github/workflows/
│   ├── daily_tests.yml         ← GitHub Actions workflow (triggered frequently)
│   └── weekly_digest.yml       ← Weekly digest email (Mondays 07:30 EEST, triggered via cron-job.org)
├── tests/
│   └── test_generate_report.py ← Smoke tests for the HTML report generator
├── run.bat                     ← Local run script (double-click, loads .env automatically)
├── report.bat                  ← Regenerate dashboard HTML from existing DB without hitting the API
├── update_projects.bat         ← Refresh ownership/commissioning in the DB from QA Locations.xlsx
└── requirements.txt
```

---



## Adding Endpoints and Groups

### Adding an endpoint to an existing group

Edit `config/endpoints.yaml` and add an entry under the relevant group:

```yaml
- name: My New Endpoint
  path: MyEndpointPath
  expected_status: 200
  max_response_ms: 5000
  checks:
    - valid_xml
    - feed_freshness
```

For live endpoints that track per-sensor health, add:
```yaml
  health_check: my_check_name   # check function that produces the health %
```

If sensor results should be stored under a different group name than the parent group (e.g. Bluetooth Paths within the Bluetooth group), add:
```yaml
  sensor_group: "Other Group Name"
```

Available checks: `valid_xml`, `feed_freshness`, `vms_controller_status`, `predefined_paths_count`, `bt_paths_speed_and_traveltime`, `bt_site_count`, `sensor_speed_status`.

### Adding a new sensor group

The report, trend chart, history table, and map layer toggles are all data-driven — adding a new group requires only config changes:

1. **`config/ui_labels.yaml`** — add a block under `groups:`:
```yaml
  Radars:
    display:       "Radars"
    color:         "#8b0000"
    layer_key:     "radar"
    map_label:     "Radars"
    history_label: "Radars"
    icon:          "ti-antenna"
    icon_size:     24
```

2. **`config/endpoints.yaml`** — add a new group with its endpoints (including `health_check` on live endpoints).

3. **`runner/tests.py`** — add the check function for the new group's live data and register it in `REGISTRY`.

That's it — the next run picks up the new group everywhere automatically.

---

## Database

Results are stored in `results/history.db` — a standard SQLite file. Open with [DB Browser for SQLite](https://sqlitebrowser.org/) for ad-hoc queries.

Key tables:

| Table | Contents |
|---|---|
| `runs` | One row per run — timestamp, total/passed/failed/errored counts |
| `test_results` | One row per endpoint per run — status, HTTP code, response time, failure reason, check summary |
| `sensor_results` | One row per sensor/path per run — status and (in LIVE_MODE) measurement data as JSON |
| `sensor_coords` | Latest lat/lon for each sensor, populated from inventory feeds; `last_seen` updated on every run |
| `bt_path_coords` | GML coordinates for all predefined BT paths, used to draw polylines on the map |
| `sensor_projects` | Per-sensor project owner + commissioning state (`active` / `not_electrified` / `decommissioned`), written by `update_projects.py` |
