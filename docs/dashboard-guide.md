# Dashboard guide

What each part of the dashboard shows, how health is rated, and where sensor ownership comes from. Back to the [README](../README.md).

## Dashboard panels

**Snapshot line** — the dashboard is a static page, so the header states when the monitor last ran and shows a live, ticking age (e.g. *Snapshot from 12:01 Cyprus time (3 min ago)*). It is a reminder that the page doesn't update itself, not a statement about the feed. Separately, a purple *Data may be outdated* banner appears when the last run is older than `staleness_threshold_hours` (9 h) — i.e. the monitor itself has stopped running.

**Live tab** (`docs/live.html`) - how the network looks right now and what needs attention first. It stores nothing: every 5 minutes the browser reads the public snapshot of the `cyprus-live-traffic` Cloudflare Worker (the same feed its map app uses) and joins it with the last stored run, which is embedded in the page when the dashboard is built.

- **Counters.** Two rows, *Sensors* and *Bluetooth paths*, each with **Down now**, **Just went down**, **Back up** and **Never worked**. Click a counter to list what is behind it.
- **What counts as down.** A path is silent when the feed has no speed reading for it. A Bluetooth *sensor* has no status of its own: it counts as silent when none of its paths reports right now (an inference, not a measurement). Sensors with not enough evidence are left out of the counts but drawn grey on the map.
- **Often silent.** A path that goes quiet on a regular pattern (every night, say) is listed separately and never counts as just gone down, so it cannot bury real faults.
- **Whole-feed outages.** A feed that is down as a whole (for example Traffic Detection) is one amber line, not hundreds of faults. Awaiting-power and decommissioned sensors are left out.
- **VMS.** Live once the Worker's snapshot carries signs (a `vms` list with each sign's status and update time). A sign with no status, or not updated for over an hour, counts as down. Until then VMS shows the last stored check and the page says so.
- **Live map.** Same base map as the dashboard (OpenFreeMap, with a satellite option). Paths are coloured by live state; Bluetooth sensors are dots and VMS signs squares. Layer buttons, full screen, reset and zoom; hover a path for its live speed and travel time (`mm:ss`). Hovering where paths overlap lists them all, and a click cycles through them.
- **Side panel.** Click a list row or a map feature: speed, travel time, typical and free-flow speed, since when, lifetime tier and last 20 checks (for a sensor, the paths that end at it), with *Show on map*, *Copy summary* and *Copy link*. Every row also has a pin that jumps to its place on the map.
- **How fresh is it?** The status bar shows *Updated HH:MM* (when your browser last fetched the snapshot) and *feed measured HH:MM* (the feed's own timestamp), in Cyprus time; a measurement over 20 minutes old turns the pill amber. The footer gives the age of the stored 6-hourly check behind the badges and warns once it is over 9 hours old, which means the monitor may have stopped.
- **If the feed cannot be reached** the page keeps the last data and says so. Dark by default, with a Light/Dark button that remembers the choice.
- **Code.** Logic in `runner/attention.py`, page in `runner/live_ui.py`, built with the dashboard and committed by the workflow.

**System Overview** — one card per group showing feed status and sensor health %, with a breakdown of each check. Each check includes a short description of what it tests. Hover the group badge (Operational / Deteriorated / Feed issue) to see colour thresholds and which test is driving the status. Hover individual endpoint dots to see a per-check pass/fail breakdown.

A purple **stuck** badge appears beside a check whose pass/fail picture has stopped changing, which a health percentage alone can't distinguish from ordinary fluctuation. It fires in two cases: a **total** outage (nothing passing for 2 or more runs in a row) and a **partial** one (the same subset of sensors passing for 4 or more runs while every other sensor never does — they have stopped reporting, not flickered). Hover it for the counts and when it began. Adding or removing sensors in the API does not reset the streak, and awaiting-power / decommissioned sensors are left out, as in every other statistic.

**Sensor Map** — interactive Leaflet.js map with:
- Colour-coded markers for Traffic Detection sensors, Bluetooth sites, and VMS controllers
- Marker clustering at country zoom (expands at zoom 13+); toggle clustering on/off with the **Cluster** button
- All predefined BT paths drawn as polylines (green = OK, red = issue, grey = no data), with directional arrows showing the direction of travel
- Toggle layers on/off; filter to issues only; collapsible legend
- **Filter by contract** — a dropdown isolates one contract's sensors on the map (or *No maintenance plan*); paths, which belong to no contract, hide while a contract is selected
- **Hide not-live** — a toggle drops the grey awaiting-power / decommissioned markers so only equipment expected to work is shown
- **Streets / Satellite** base-map toggle. Streets is [OpenFreeMap](https://openfreemap.org) (always the light style) — the same base map as the BT paths map and the QA reports; satellite is Esri World Imagery
- **Full screen** button — expands the map *and its controls* to fill the display, so filtering, clustering and playback all stay usable
- **View on map** button on each group card isolates that group and flies to its bounds
- **Historical playback** bar — scrub or step through the last 30 runs to see how sensor statuses changed over time

The map pop-up for each sensor shows its **project owner**, a **View history →** link that jumps to that sensor's row in the Sensor Stability panel and opens its trend, and — for sensors awaiting power or decommissioned — a neutral "Awaiting power" / "Decommissioned" status with a grey marker.

**Sensor Health Trend** — one line per sensor group showing health % across the last 30 runs.

**Sensors by contract** — every maintenance contract and the sensors it covers, so a fully-healthy contract stays visible instead of only the ones with a current fault. Four columns per contract: **Total** owned, **Working**, **Faults**, and **Not live** (awaiting power / decommissioned). A **fault** here is a *persistent* problem — a sensor that failed **at least 80% of its last 20 runs**, not a one-cycle blip — so for a live contract it is what the contractor should be responding to; faults are shown neutral for an out-of-support contract, where a failure is expected. Click a contract row to expand the list of its failing sensors (name, recent fail count, and current outage age, longest first). Sensors matched to no contract appear under **No maintenance plan** — hovering one in the Sensor Stability panel explains **why** it has no owner: either the reference spreadsheet has no row for it (a data-entry task — add the row and re-run `update_projects.bat`), or a nearer sensor claimed the only nearby row (a possible mis-mapping). Awaiting-power and decommissioned sensors count under *Not live*, never as faults.

**Sensor Stability** — per-sensor table with a **Current state** cell (Working / Down *N*d / Never worked), a sparkline of the last 20 runs, the **lifetime** stability badge, the **project owner**, and timestamps for last working / last issue.
- Live search input to filter by sensor name or ID. Wrap the term in double quotes for an **exact match** — `"10"` finds sensor 10 only, not 1001 or 1040. Exact match tests each identifier shown on the row (site code, name words, and both endpoints of a Bluetooth path)
- Group dropdown filter
- Sort dropdown — Default, Worst first, or Best first
- 📍 icon on each row — click to fly the map to that sensor or BT path and open its popup
- Expandable per-sensor daily health % trend chart (last 7 / 30 days)
- Awaiting-power / decommissioned sensors show a neutral badge and are left out of the panel's health bar

**Run History** — per-run feed status and sensor health % for each sensor group across the last 30 runs.

> The dashboard uses a **two-column layout** (55/45): the left column holds System Overview, Sensor Map, Health Trend, and Run History; the right column holds the Sensors-by-contract and Sensor Stability panels and stays sticky while you scroll. On narrow screens (below 900px) the columns stack vertically.

## Health model in detail

The monitor uses a two-tier health model:

- **Feed health** (binary) — did the API respond with valid, fresh XML? If not, the group is marked as a feed issue regardless of sensor counts.
- **Sensor health %** — of the sensors/controllers/paths reported by the feed, what percentage are working? Shown as a percentage badge per group: green ≥ 90 %, amber ≥ 80 %, red < 80 %.

The Stability panel answers **two different questions with two different columns**, and they are deliberately kept apart:

- **Current state** — what the sensor is doing *right now*: `Working`, `Down 10d`, or `Never worked (28d)`. This is what the control room acts on.
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

Sensors marked in the reference sheet as **awaiting power** (not yet electrified) or **decommissioned** are not expected to be working, so they get a neutral badge instead of a health tier and are **excluded from all health statistics** (see [Sensor Ownership & Commissioning](#sensor-ownership-and-commissioning)).

This means a group is never falsely marked "failed" just because some sensors are malfunctioning — the feed being up/down is tracked separately from individual sensor health.

## Sensor ownership and commissioning

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
