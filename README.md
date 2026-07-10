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

The Stability panel answers **two different questions with two different columns**, and they are deliberately kept apart:

- **Current state** — what the sensor is doing *right now*: `Working`, `Down 10d`, or `Never worked (28d)`. This is what the control room acts on, and it drives the "Attention needed" list.
- **Stability (lifetime)** — the sensor's record across **every run ever taken**: how far it can be trusted. It is intentionally slow-moving, so a sensor repaired yesterday still shows a poor record.

A sensor can be down today with an excellent lifetime record (a new fault), or working today with a terrible one (a repeat offender that just came back). Both facts matter, so both are shown.

| Badge | Lifetime range | Meaning |
|---|---|---|
| Always on | 99–100% | Effectively every run was good |
| Healthy | 90–98% | Consistently up, rare misses |
| Intermittent | 70–89% | Mostly working but with regular gaps |
| Unstable | 40–69% | Unreliable — failing more often than not |
| Critical | under 40% | Mostly failing, **but has worked at least once** |
| Always off | never | Has **never** produced a single good reading |

**"Always off" means literally zero good runs** — not a percentage that rounds to zero. That distinction is deliberate: it is a defensible claim to put in front of a contractor ("this sensor has never worked"). A sensor that worked once and never again is `Critical`, not `Always off`. Tiering compares the raw ratio, so there is no rounding cliff.

A sensor with fewer than 5 recorded runs shows a neutral **Collecting data** badge rather than a falsely precise tier.

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

**Attention needed, by project** — lists every sensor that is **not reporting right now**, grouped by the project/contractor that owns it and ordered by **how long it has been down** (longest outage first), so the strongest case comes to the top. Each sensor also carries its lifetime badge as context — is this a new fault, or a repeat offender? A sensor repaired yesterday drops off the list; one that died this morning appears however good its record was. Out-of-support projects (where a failure is expected and non-actionable) are shown in a separate, clearly-marked section; sensors not matched to any project surface as *Unassigned — no owner known*. Hovering an unassigned sensor's project cell explains **why** it has no owner: either the reference spreadsheet has no row for it (a data-entry task — add the row and re-run `update_projects.bat`), or a nearer sensor claimed the only nearby row (a possible mis-mapping). Awaiting-power and decommissioned sensors are excluded (they aren't faults).

**Sensor Stability** — per-sensor table with a **Current state** cell (Working / Down *N*d / Never worked), a sparkline of the last 20 runs, the **lifetime** stability badge, the **project owner**, and timestamps for last working / last issue.
- Live search input to filter by sensor name or ID. Wrap the term in double quotes for an **exact match** — `"10"` finds sensor 10 only, not 1001 or 1040. Exact match tests each identifier shown on the row (site code, name words, and both endpoints of a Bluetooth path)
- Group dropdown filter
- Sort dropdown — Default, Worst first, or Best first
- 📍 icon on each row — click to fly the map to that sensor or BT path and open its popup
- Expandable per-sensor daily health % trend chart (last 7 / 30 days)
- Awaiting-power / decommissioned sensors show a neutral badge and are left out of the panel's health bar

**Run History** — per-run feed status and sensor health % for each sensor group across the last 30 runs.

> The dashboard uses a **two-column layout** (55/45): the left column holds System Overview, Sensor Map, Health Trend, and Run History; the right column holds the Attention-needed and Sensor Stability panels and stays sticky while you scroll. On narrow screens (below 900px) the columns stack vertically.

---

## Schedule

Tests run every 6 hours (triggered via cron-job.org). The workflow can also be triggered manually from the GitHub Actions tab.

A **weekly digest email** is sent every Monday at 07:30 Cyprus time (EEST). It summarises the past 7 days of sensor health, flagging sensors with **no good runs that week**, persistently unstable sensors, degraded/recovered sensors, and any sensors retired from the API feed. Sensors are named exactly as the dashboard names them (via `runner/labels.py`), with the raw ID alongside for quoting to a contractor. Because the digest scores a single week, its zero tier reads *"No good runs"* rather than the dashboard's lifetime *"Always off"*.

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
│   ├── stability.py            ← Shared health thresholds + the six stability tiers
│   ├── labels.py               ← Shared human-readable sensor names (report + digest)
│   ├── qa.py                   ← Reference-sheet ↔ API coordinate matching (ownership + commissioning)
│   ├── update_projects.py      ← Refresh sensor→project + commissioning in the DB after editing the workbook
│   └── digest.py               ← Weekly digest email builder and sender
├── results/
│   └── history.db              ← SQLite DB (auto-committed after each run)
├── reports/
│   └── latest.html             ← Generated dashboard (auto-committed after each run)
├── QA Locations.xlsx           ← Reference equipment inventory (gitignored — local/cloud only)
├── .github/workflows/
│   ├── daily_tests.yml         ← GitHub Actions workflow (runs pytest, then the API tests)
│   └── weekly_digest.yml       ← Weekly digest email (Mondays 07:30 EEST, triggered via cron-job.org)
├── tests/
│   ├── test_generate_report.py ← Dashboard generator: current state, fault age, search tokens
│   ├── test_stability.py       ← Tier boundaries and health percentages
│   ├── test_labels.py          ← Shared sensor-name rules
│   └── test_data_quality.py    ← Coordinate matching and the retire guard
├── run.bat                     ← Local run script (double-click, loads .env automatically)
├── report.bat                  ← Regenerate dashboard HTML from existing DB without hitting the API
├── update_projects.bat         ← Refresh ownership/commissioning in the DB from QA Locations.xlsx
├── qa_tdu.bat / qa_bt.bat / qa_vms.bat  ← Open the per-group QA matching report (needs QA Locations.xlsx)
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
