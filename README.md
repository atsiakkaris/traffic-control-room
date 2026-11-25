# ITS Infrastructure Health Monitor

Automated health monitoring for Cyprus traffic infrastructure. Every 6 hours GitHub Actions tests the public traffic APIs, stores the results in SQLite and publishes a dashboard to GitHub Pages.

**[Open the site →](https://atsiakkaris.github.io/traffic-control-room/)**

---

## The three tabs

The site ([index.html](index.html)) has three tabs:

| Tab | What it shows | Updates |
|---|---|---|
| **Dashboard** | Health of every sensor group, map, trends, stability, contracts | After each 6-hourly run |
| **BT Paths Map** | Bluetooth travel-time paths on a map, with duplicate and overlap checks | After each 6-hourly run |
| **Live** | What is down right now, from the public live feed | Every 5 minutes, in your browser |

Panel-by-panel details: [docs/dashboard-guide.md](docs/dashboard-guide.md).

---

## What it checks

| Group | Endpoint | What is checked |
|---|---|---|
| **Traffic Detection** | [TD Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/TrafficMeasurementSiteTablePublication) | Valid XML |
| | [TD Live](https://www.traffic4cyprus.org.cy/swarco3/api/Data/MeasuredDataPublication) | Feed freshness; per-sensor speed status; average flow |
| **Bluetooth** | [BT Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/BTMeasurementSiteTablePublication) | Valid XML; device count |
| | [BT Paths Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationPublication) | Valid XML; path count |
| | [BT Paths Live](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationDataPublication) | Feed freshness; speed and travel time per path; per-path timestamp (catches a stalled feed) |
| **VMS** | [VMS Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsTablePublication) | Valid XML |
| | [VMS Live](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsPublication) | Feed freshness; controller status counts; per-controller timestamp |

Every live endpoint also checks HTTP 200 and response time. "Feed freshness" (data at most 5 minutes old) only proves the API answered just now, so BT paths (30 min) and VMS controllers (1 h) also have their own timestamp check.

---

## How health is rated

- **Feed health:** did the API answer with valid, fresh data? If not, the group shows a feed issue whatever the sensors do.
- **Sensor health %:** of the sensors the feed reports, how many work? Green 90% or more, amber 80% or more, red below.

Each sensor has two separate answers:

- **Current state:** what it is doing now (`Working`, `Down 10d`, `Never worked`).
- **Stability (lifetime):** its record over every run ever taken. It moves slowly, so a sensor repaired yesterday still shows a poor record.

| Badge | Lifetime uptime | Meaning |
|---|---|---|
| Always on | 99–100% | Effectively every run was good |
| Healthy | 90–98% | Rare misses |
| Intermittent | 70–89% | Regular gaps |
| Unstable | 40–69% | Fails more often than not |
| Critical | under 40% | Mostly failing, but has worked at least once |
| Always off | never | Has never produced a good reading |

Sensors with fewer than 5 runs show *Collecting data*. Sensors marked *awaiting power* or *decommissioned* in the reference sheet get a neutral badge and are left out of all statistics. More: [docs/dashboard-guide.md](docs/dashboard-guide.md#health-model-in-detail).

---

## Everyday tasks

Double-click the `.bat` file in the repo root. Secrets are read from your local `.env`.

| To do this | Run |
|---|---|
| Run the checks now and build the dashboard | `run.bat` |
| Rebuild the dashboard from the existing database (no API calls) | `report.bat` |
| Update sensor owners and commissioning after editing `QA Locations.xlsx` | `update_projects.bat`, then commit `results/history.db` |
| Open the Control Room review app | `control_room.bat` |
| Open the BT paths map on its own | `bt_paths_map.bat` |
| Preview the weekly digest (does not send an email) | `digest.bat` |
| Run the tests | `python -m pytest` |

---

## Control Room

A local app (`control_room.bat`, nothing is exposed outside your PC) for reviewing the network:

- **Paths:** the BT paths map, with flags and notes saved on your PC.
- **Sensor views:** Traffic Detection, Bluetooth and VMS compared with the spreadsheet, plus triangulation of faulty Bluetooth sensors.
- **Site visits:** plans on-site checks by area and day, with Excel, phone and Google My Maps exports.
- **Wiring:** checks OMNIA path nodes, with bulk edit and reverse-path creation (off unless enabled).
- **Routes:** adds up live travel times between two nodes and looks for missing paths.
- **Live:** the Live tab with each sensor's contract.

Deleting paths and editing OMNIA are off by default. Everything is explained in [docs/control-room.md](docs/control-room.md).

---

## Schedule and weekly digest

- **Checks:** every 6 hours, triggered by cron-job.org. You can also start the workflow from the GitHub Actions tab.
- **Weekly digest email:** Mondays at 07:30 Cyprus time. It covers the past 7 days: sensors with no good runs, unstable ones, recoveries, retired sensors and the *Sensors by contract* census.

---

## Project structure

```
├── config/            Endpoints, project accountability, UI labels
├── runner/            The checks, database, dashboard, Live tab, digest and map builders
├── tools/             Control Room app and small helpers
├── tests/             pytest suite
├── docs/              Published pages (live.html, bt-paths-map.html) and guides
├── reports/           Generated dashboard (latest.html)
├── results/           history.db, the SQLite store
├── .github/workflows/ daily_tests.yml, weekly_digest.yml
├── index.html         Tabbed landing page (GitHub Pages root)
└── *.bat              Local shortcuts (see Everyday tasks)
```

`QA Locations.xlsx` (the reference inventory) is kept out of git. How the code fits together: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Database

`results/history.db` is a SQLite file; open it with [DB Browser for SQLite](https://sqlitebrowser.org/).

| Table | Contents |
|---|---|
| `runs` | One row per run |
| `test_results` | One row per endpoint per run |
| `sensor_results` | One row per sensor or path per run |
| `sensor_coords` | Latest position of each sensor |
| `bt_path_coords` | Drawn route of each BT path |
| `sensor_projects` | Project owner and commissioning state per sensor |
| `feed_state` | Latest measurement time per live feed |
| `bt_path_last_ts` | Last timestamp each BT path reported |

---

## More documentation

- [docs/dashboard-guide.md](docs/dashboard-guide.md): what each dashboard panel shows, health model, sensor ownership
- [docs/control-room.md](docs/control-room.md): the Control Room app and BT paths review
- [docs/how-the-checks-work.md](docs/how-the-checks-work.md): the rules behind the Bluetooth checks
- [ARCHITECTURE.md](ARCHITECTURE.md): how the code works, and how to add an endpoint or a sensor group
