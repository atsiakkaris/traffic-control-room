# ITS Infrastructure Health Monitor

Automated health monitoring for Cyprus SWARCO DATEX II traffic infrastructure. Tests run twice daily via GitHub Actions, results are stored in SQLite, and a live HTML dashboard is published to GitHub Pages after every run.

---

## Live Dashboard

**[View the latest report →](https://atsiakkaris.github.io/traffic-control-room/reports/latest.html)**

Updates automatically within ~1 minute of each scheduled run.

---

## What It Monitors

| Group | Endpoint | What is checked |
|---|---|---|
| **Traffic Detection** | TD Inventory | Valid XML |
| **Traffic Detection** | TD Live | Sensor speed status — working / no traffic / malfunctioning / no data; average flow rate |
| **Bluetooth** | BT Inventory | Valid XML; total device count |
| **Bluetooth** | BT Paths Inventory | Valid XML; predefined path count |
| **Bluetooth** | BT Paths Live (FCD) | Speed and travel time present for all 513 paths |
| **VMS** | VMS Inventory | Valid XML |
| **VMS** | VMS Live Data | Working / not-working / no-status controller counts |

All endpoints also check: HTTP 200 status, response time within limit, and feed freshness (data ≤ 15 min old where applicable).

---

## Dashboard Panels

**Infrastructure Groups** — one card per group showing current pass/fail with a detailed breakdown of each check and collapsible lists of failing sensor/controller IDs.

**Sensor Map** — interactive Leaflet.js map of Cyprus with:
- Colour-coded markers for Traffic Detection sensors, Bluetooth sites, and VMS controllers
- All 513 BT paths drawn as polylines (green = OK, red = issue, grey = no data)
- Click any marker or path to see live measurements (speed, flow rate, travel time) in a fixed info panel
- Toggle layers on/off; filter to issues only; collapsible legend

**Sensor Stability** — per-sensor history table with sparklines (last 40 runs), stability badge, and last known issue. Filter by group.

**Pass/Fail Trend** — stacked bar chart across the last 30 runs.

**Run History** — pass/fail counts and pass rate for the last 20 runs.

---

## Schedule

Runs twice daily:

| Cron (UTC) | Cyprus time |
|---|---|
| `0 3 * * *` | 06:00 EET |
| `0 19 * * *` | 22:00 EET |

Edit `.github/workflows/daily_tests.yml` to change. Use [crontab.guru](https://crontab.guru) to build a custom schedule.

---

## Project Structure

```
├── config/
│   └── endpoints.yaml          ← Endpoint definitions and checks
├── runner/
│   ├── run_tests.py            ← Entry point
│   ├── tests.py                ← XML assertion logic per check type
│   ├── db.py                   ← SQLite helpers (schema, queries, migrations)
│   ├── geo.py                  ← Coordinate extraction from DATEX II inventory feeds
│   └── report.py               ← HTML dashboard generator
├── results/
│   └── history.db              ← SQLite DB (auto-committed after each run)
├── reports/
│   └── latest.html             ← Generated dashboard (auto-committed after each run)
├── .github/workflows/
│   └── daily_tests.yml         ← GitHub Actions schedule
├── run.ps1                     ← Local run script (loads .env automatically)
└── requirements.txt
```

---


## GitHub Setup

### Secrets required

Go to **Settings → Secrets and variables → Actions → Repository secrets**:

| Secret | Description |
|---|---|
| `BASE_URL` | Base URL of the DATEX II host |
| `SWARCO` | API path segment (e.g. `swarco3/api/Data/`) |
| `GMAIL_USER` | Gmail address for outgoing notification emails |
| `GMAIL_APP_PW` | Gmail App Password (16-char — not your account password) |
| `NOTIFY_EMAIL` | Recipient address for the daily email summary |

### Workflow permissions

Go to **Settings → Actions → General → Workflow permissions** and enable **Read and write permissions** — required for the bot to commit `history.db` and `latest.html` back to the repo.

### GitHub Pages

Go to **Settings → Pages**, set source to **Deploy from a branch**, branch `main`, folder `/ (root)`.

---

## Adding Endpoints

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

Available checks: `valid_xml`, `feed_freshness`, `vms_controller_status`, `predefined_paths_count`, `bt_paths_speed_and_traveltime`, `bt_site_count`, `sensor_speed_status`.

---

## Database

Results are stored in `results/history.db` — a standard SQLite file. Open with [DB Browser for SQLite](https://sqlitebrowser.org/) for ad-hoc queries.

Key tables:

| Table | Contents |
|---|---|
| `runs` | One row per run — timestamp, total/passed/failed/errored counts |
| `test_results` | One row per endpoint per run — status, HTTP code, response time, failure reason, check summary |
| `sensor_results` | One row per sensor/path per run — status and (in LIVE_MODE) measurement data as JSON |
| `sensor_coords` | Latest lat/lon for each sensor, populated from inventory feeds |
| `bt_path_coords` | GML coordinates for all 513 BT paths, used to draw polylines on the map |

To reset history, delete `results/history.db`. The next run creates a fresh database.
