# ITS Infrastructure Health Monitor

Automated health monitoring for Cyprus traffic infrastructure. Tests run every 6 hours via GitHub Actions, results are stored in SQLite, and a live HTML dashboard is published to GitHub Pages after every run.

---

## Live Dashboard

**[Open the site →](https://atsiakkaris.github.io/traffic-control-room/)**

A tabbed landing page ([index.html](index.html)) switches between the **Dashboard**
(updates automatically within ~1 minute of each scheduled run) and the
**BT Paths Map** (see below).

---

## BT Paths Review Tool

A standalone tool (`runner/bt_paths_map.py`) for manually auditing the
legacy Bluetooth travel-time paths — many overlapping or duplicated after 10
years of additions. It shows just the BT paths and their anchor sensors on a
Leaflet map, with:

- **Duplicate detection** — same-name paths with matching status history are
  auto-collapsed to one; partial matches are flagged for manual review instead
  (`--show-duplicates` shows every registration uncollapsed)
- **Overlap detection** — click a line or endpoint marker to cycle through
  paths running suspiciously close together
- **Per-path flag/note** with local persistence, and CSV export/import so
  findings can be backed up or shared between reviewers (see caveat below)
- **Cycle flagged only** — step through just the flagged paths instead of all
  of them alphabetically

Run it locally with `bt_paths_map.bat` (opens `reports/bt_paths_map.html`,
gitignored). ⚠️ Flags/notes are saved in your **browser's local storage** —
they don't sync across machines or browsers and aren't backed up anywhere;
export to CSV periodically if you want to keep them.

**Control Room (recommended for review work).** `control_room.bat` starts a small
app on your own machine (`127.0.0.1:8765`, nothing is exposed) and opens the same map
in your browser. Flags and notes are saved to `results/path_review.json` instead of
browser storage, and a **Review & delete…** button lists the flagged paths, runs a
dry run, and only after you type `YES` deletes them from the SWARCO API
(results are logged to `results/delete_log.csv`).

The same app also has a **Sensor locations** view (tabs for Traffic Detection / Bluetooth / VMS) that compares every API sensor with its
`QA Locations.xlsx` row and shows the distance between them. These pages only read
the database — they never change ownership data the way `update_projects.bat` does.

The app has a tab bar at the top: **Paths** and the three sensor views. All tabs stay loaded,
so switching never reloads the map. Select a path and use *Sensors at start / at end* to
jump to the sensor view at that spot; there, *Back to path* selects it again on the Paths
tab and *Paths here* centres the map on what you were looking at. Pages are remembered
until the database changes, so opening is instant; the **Refresh** button in the tab bar
rebuilds the open tab from the latest data. Clicking an API sensor on the Paths map shows the same details
as the Bluetooth tab (health, project, spreadsheet match, triangulation verdict) and lists its paths; *select* opens one.
`control_room.bat` opens it in **its own window** (needs `pip install pywebview` once; without
it the page opens in your browser instead, with a console that must stay open). Close the
window or press **Quit** in the tab bar to stop it; errors, if any, go to
`results/control_room.log`.

**Reading the sensor maps.** Above each map (Traffic Detection, Bluetooth, VMS) a summary line counts the dots - under 90% uptime, contract ended, retired or no history - and its buttons show only that group. Under it, the key lists every dot style with its count; click a style to show only that style (click it again to show all, ctrl+click adds or removes one). A dot's colour is lifetime uptime, except on the Bluetooth tab: Bluetooth sensors have no uptime of their own, so there the colour is the triangulation verdict (green working, orange works but some paths fail, red faulty, dashed grey not enough evidence). The key also explains the marks on top of the dots (gold halo = confirmed match, number = sensors at one spot, orange N = your note, big rings on Bluetooth = triangulation verdicts); a short distance line is marked by a dashed ring when you filter to it.

**Popups lead with a sentence.** Clicking a sensor on any sensor tab (and a Bluetooth sensor on the Paths tab) opens a popup whose
first line is one plain sentence in a coloured band: green *Working now*, orange *Working now, but it failed 6 of its last 20
runs* or *Failing in the latest check*, red *Down for 12 days (since 30 Oct): Speed = -1 (sensor fault)* or *Never worked since it
was first seen*, grey *Retired*, *Awaiting power*, *Decommissioned* or *No monitor history yet*. Lifetime uptime and the current
state now sit side by side (*Uptime (lifetime): 62% (Unstable) · Now: Down 12d*), because they answer different questions. A
sensor whose contract is active and that failed 80% of its last 20 runs also gets *A persistent fault for <project>*; one with
no contract on record says so; one whose contract ended says a failure is expected. Bluetooth sensors get their triangulation
verdict as the sentence. Rules: [docs/how-the-checks-work.md](docs/how-the-checks-work.md#9-the-popup-sentence).

**Reload app (top bar).** Restarts the Control Room with the latest code and the local database, for when files changed or you copied in a newer `history.db`; nothing is fetched from the live feed or OMNIA (that is *Refresh*). The window closes and reopens within a few seconds; flags and notes are already saved.

**Notes on sensors.** Click a sensor on the Traffic Detection, Bluetooth or VMS tab (or a Bluetooth sensor on the Paths
tab) and write in the *Note* box of its popup; it saves by itself and the sensor gets an orange N badge on the map. Notes are kept in
`results/sensor_notes.json` on this PC only (git-ignored, so they never reach the public repo). Emptying the box removes the note.

**Planning on-site checks (Site visits tab).** Some doubts can only be settled on site: is the sensor still there, and is it
where the API or the spreadsheet says? The tab lists everything worth a visit (positions more than 100 m apart, a sensor in
the API with no spreadsheet row or the other way round, a pair of those within 1 km that is probably one device, retired
sensors, faulty Bluetooth sensors, sensors you wrote a note on), merges what is within 150 m into one stop, splits the stops by
area (Nicosia, Limassol, Larnaca, Paphos, Famagusta) and puts each area in driving order, cut into days (15 stops by default).
Tick what to include and press *Plan again*. *Save field sheet (Excel)* gives one sheet per day, stops in driving order, with
yellow columns to fill in on site (drop-down lists for *Found?* and *Right position*, GPS, label, remarks), *Navigate* links and
print-ready pages; *Save for Google My Maps (KML)* can be imported into Google My Maps so the days are on your phone; *Print*
prints the day lists. Click a day's heading to fold or unfold it (*Fold all days* / *Unfold all days* do all at once; folded days still print open). Each day also has *Open in Google Maps* links that drive through its stops. The order uses straight-line
distance and nothing is sent anywhere until you open a Google link.

**A general view of the issues, filtered without replanning (*All issues*).** The *All issues* view lists everything the checks
flag, before any plan filter, with an overview table of reasons by group (for example how many Bluetooth sensors have a position
that differs) at the top, right under the View buttons, and the map and the list below it. Tick or untick a reason or a group, or click a number to see just those; *include visited* and *tick everything*
and the search box narrow it further. The list and the map (one coloured dot per reason) change instantly; the plan, its filters
and its day numbers are not touched, and each row says which stop of the current plan it is on (or that it is not in the plan).
This is different from the tick boxes under *Plan*, which choose what goes into the days and only take effect with *Plan again*.

**Your notes and what you have checked (View buttons, top of the tab).** *Plan* is the days. *My notes*, *Visited / checked* and
*Notes + visits* switch the map and the list to just what you wrote or did, for every sensor, wherever it is in the plan and
whatever the filters say; nothing is replanned (the plan, its filters and its day numbers are untouched, and *Plan* brings it back
as it was). The list shows each sensor with your note (and its date), what was found on site and what it means for you, with *show
on map*, *navigate* and a link to the sensor's own tab; a search box filters the text. On the map an orange **N** is a note, a green
**✓** a visited sensor and a purple **N✓** both. In the plan itself a note is now a highlighted box on the card and an orange N on
the stop's map marker. The view is remembered until you press *Plan again*. Notes are the ones you write in a sensor's popup
on the sensor tabs; write or edit them there.

**Why a sensor is recommended.** Nothing on the list is a verdict. Every card says *Why it is on the list* with the numbers behind
it (for example "the API puts it at ..., the spreadsheet row at ...: 233 m apart, we flag 100 m or more"), *The monitor says* (the
same sentence as the popup status line) and *To check* on site. Under the map, *Why are these sensors recommended? How the list is
made* explains the reasons with their thresholds and what each one means, how the list becomes stops and days, and what is
deliberately **not** on the list; it is written from the same constants the code uses and prints with the days. Each stop also shows
its straight-line km from the previous stop (or the start). The same reasoning is in the Excel field sheet (a *Why it is on the list*
column and sections on the Read me sheet), in the Google My Maps file and on the phone page, so you can show a colleague any of them.

**Where each day starts.** Every day begins at your office, so each day's stops are ordered from it, the drive from it is counted
and the Google Maps links start there. It is set in the git-ignored `.env` (the repo is public, so the address is not in the
code): `CONTROL_ROOM_VISIT_START=Name shown|lat,lon`. Without it a day starts at the centre of its area. Coming back is not counted.

**On your phone.** *Save for phone (web page)* writes one self-contained HTML file with the days as big cards: a *Navigate* button
per position (opens Google Maps), *Found?* and *Which position is right?* buttons, a GPS box with *Use my location*, label and
remarks, a "next stop" bar and a done counter. Send the file to your phone (e-mail, cloud drive, USB) and open it in the phone's
browser; it needs no internet except for Google Maps itself. Answers are kept in the phone's browser (the same stops keep the same
answers if you save the plan again). *Share / save results* makes a CSV with the same columns as the field sheet; send it back to
this PC and use *Import findings...*. The Control Room only listens on this PC, so the page is a file, not a link.

**After the visit.** *Import findings...* reads the filled-in Excel file (or the phone's results CSV) back (a preview first; nothing changes until you press
Apply). Items with a Yes or No answer count as *visited* and leave the plan (*Show visited* brings them back), and every finding
is translated into what you have to do: correct the spreadsheet, ask SWARCO to correct the API position, add a missing row,
installed but not reported by the API, not found on site, and so on. These appear in *What the visits found* and can be saved
as a corrections CSV. The import can also add a dated line to each sensor's note. Single items can be marked visited by hand
(*Mark as visited* in the popup or the list; *Not visited after all* takes it back without losing imported findings). Findings
are kept in `results/site_visits.json` (git-ignored, like the notes). Rules:
[docs/how-the-checks-work.md](docs/how-the-checks-work.md#8-site-visits-what-goes-on-the-list).

**Which Bluetooth sensor is faulty? (Bluetooth tab → *Triangulation*).** A path only reports data when both
of its sensors work, so a path that works proves both ends are alive. A sensor with no working path, whose failing
paths lead to sensors that work elsewhere, is the likely culprit; a sensor with both working and failing paths is
only a *path problem*. The panel (bottom-left of the Bluetooth tab) lists the suspects, rings them on the map, and
each popup links to its paths on the Paths tab; it can export a CSV. **Show only** (next to *Export CSV*) hides every
other dot on the map and keeps just the sensors ticked in the panel; click it again (*Show all*) to bring the rest back. Runs during whole-feed outages are ignored,
and paths whose name doesn't match where they really end are left out rather than guessed.

**Is a path wired to the right sensors? (Wiring tab).** In OMNIA every path has a start and an end *logical node*,
chosen by hand. The tab compares those with the API sensors (exact, case-sensitive names) and with where the path is
drawn, and lists wrong, swapped or missing nodes; it can export a CSV, and the Paths inspector and sensor popups carry
the same findings. Nodes are shown the way you think of them: the sensor's **site code and name** first and OMNIA's **node id small and grey** underneath
(a node no API sensor matches shows its name, "(no sensor)" and the node id). The node id is what OMNIA's edit call needs and the only way
to tell look-alike nodes apart, so it stays, in the edit boxes (which take node ids, not site codes: the two number ranges overlap, and the
text next to each box shows the site code and sensor of the node you typed), in the previews and in the logs; sorting and searching work on
site codes, and the CSV has two extra columns with the start and end site code. **Fetch from OMNIA** reads the node and path lists (read-only, needs the internal network, up to a
minute) and keeps `results/omnia_snapshot.json`, which is git-ignored. The OMNIA addresses come from the git-ignored `.env`.
Rules: [docs/how-the-checks-work.md](docs/how-the-checks-work.md).

**Changing many OMNIA paths at once (Wiring tab -> Bulk edit).** Tick paths (click a row to select only it, shift+click a range, ctrl+click adds or removes one, or tick a whole filter; up to 100 per batch, 50 for deleting; a counter at the bottom shows how many are selected), then set a
speed range, swap start and end node, set the nodes (or fill missing or wrong ones with the nearest node to the drawn end, clear matches only), and press *Preview changes*: OMNIA is read live and every path is
shown old value -> new value, nothing is sent. OMNIA needs all of
speed min, speed max, start and end node for every call, so a path with no nodes only changes once both nodes are given;
each path's current name is always sent back so nothing is renamed. Every change is logged with its old values in
`results/omnia_edit_log.csv`, and *Preview undo of last batch* puts the last batch back (paths edited by hand since are
left alone). Only whole numbers above 0 are accepted. **Rename** works the same way (preview, then apply): give one path a
new name, or find/replace, a prefix or a suffix across the selected paths, or tick *name from its nodes* to rebuild
`start->end` from the site codes of its two nodes' sensors; after sending, the names are read back from OMNIA to confirm they
changed, and *Preview undo of last rename* restores the old ones (`results/omnia_rename_log.csv`).

**A path was re-created under a new id? (history merge).** OMNIA cannot turn a path into another geometry and never gives a
deleted id out again, so a re-drawn path gets a new id and starts with an empty history. `python tools/merge_path_history.py 250=661`
(OLD=NEW, several pairs allowed) shows a dry run; with `--apply` it first backs up `results/history.db`, then re-keys the old
history onto the new id so the path keeps one continuous history. Pairs are found by comparing the road segments of the old and new
path (old paths keep theirs in OMNIA after a delete).

**Map of OMNIA nodes versus API sensors (stand-alone).** `tools\omnia_nodes_map.bat` (or
`python runner/omnia_nodes_map.py [--fetch]`) writes `reports/omnia_nodes_map.html`, a separate page that opens in any
browser: every OMNIA logical node (coloured by how it matches a sensor) next to the sensor positions from the API, with
dashed lines where a node and its sensor are apart, a search box and filters. It uses the saved OMNIA snapshot (`--fetch`
reads OMNIA first) and is git-ignored because it holds OMNIA data.

**Who can delete.** The app is **read-only by default**: anyone can view the maps, compare
sensors and flag paths, but the *Review & delete…* button is absent (a *Read-only* badge
shows in the tab bar) and the server refuses delete requests. Deleting is switched on per PC
by adding `CONTROL_ROOM_ALLOW_DELETE=1` and `CONTROL_ROOM_DELETE_URL=<SWARCO delete address>` to
that PC's own `.env` (git-ignored, so a fresh clone never has either; the address is
deliberately not in the public code). Even then every delete needs the dry run and a typed `YES`.

The copy at the link above (`docs/bt-paths-map.html`) **auto-refreshes every 6 hours** alongside the dashboard, so path
additions/deletions and the duplicate/overlap checks stay current on their own.

---

## What It Monitors

| Group | Endpoint | What is checked |
|---|---|---|
| **Traffic Detection** | [TD Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/TrafficMeasurementSiteTablePublication) | Valid XML |
| **Traffic Detection** | [TD Live](https://www.traffic4cyprus.org.cy/swarco3/api/Data/MeasuredDataPublication) | Feed freshness; sensor speed status (working / no traffic / malfunctioning / no data); average flow rate |
| **Bluetooth** | [BT Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/BTMeasurementSiteTablePublication) | Valid XML; total device count |
| **Bluetooth** | [BT Paths Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationPublication) | Valid XML; predefined path count |
| **Bluetooth** | [BT Paths Live (FCD)](https://www.traffic4cyprus.org.cy/swarco3/api/Data/PredefinedLocationDataPublication) | Feed freshness; speed and travel time per path; per-path measurement freshness (catches a frozen/stalled live feed even when speed/travel-time values still look valid) |
| **VMS** | [VMS Inventory](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsTablePublication) | Valid XML |
| **VMS** | [VMS Live Data](https://www.traffic4cyprus.org.cy/swarco3/api/Data/VmsPublication) | Feed freshness; working / not-working / no-status controller counts; per-controller status freshness (catches a controller stuck re-reporting an old status) |

All live endpoints also check: HTTP 200 status, response time within limit, and feed freshness (data ≤ 5 min old) — that last one only confirms the API responded just now, not that the values inside are new. For BT Paths and VMS specifically, each record's own timestamp (`measurement_timestamp` / `statusUpdateTime`) is checked separately (Bluetooth paths ≤ 30 min, VMS controllers ≤ 1 h) since the two can disagree.

---

## Health Model

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

**Snapshot line** — the dashboard is a static page, so the header states when the monitor last ran and shows a live, ticking age (e.g. *Snapshot from 12:01 Cyprus time (3 min ago)*). It is a reminder that the page doesn't update itself, not a statement about the feed. Separately, a purple *Data may be outdated* banner appears when the last run is older than `staleness_threshold_hours` (9 h) — i.e. the monitor itself has stopped running.

**Live tab** (the third tab, `docs/live.html`) — how the network looks right now and what needs attention first. It stores nothing: every 5 minutes the browser reads the public snapshot of the `cyprus-live-traffic` Cloudflare Worker (the same feed its map app uses) and joins it with the history of the last stored run, which is embedded in the page when the dashboard is built. Two rows of four counters, *Sensors* and *Bluetooth paths*, each with **Down now**, **New since last check**, **Back up** and **Never worked**. A path is silent when the live feed has no speed reading for it. A Bluetooth *sensor* has no status of its own: it counts as silent when none of its paths reports right now, which is an inference from how its paths behave (triangulation), not a measurement; sensors with not enough evidence are left out of the counts but still drawn on the map in grey. A path that goes quiet on a regular pattern (e.g. every night) is listed separately as *often silent* and is never counted as new, so it cannot bury real faults. A feed that is down as a whole (e.g. Traffic Detection) is one amber line, not hundreds of faults; awaiting-power and decommissioned sensors are left out. **VMS** is live once the Worker's snapshot carries signs (a `vms` list with each sign's status and update time; a sign with no status, or one not updated for over an hour, counts as down, as in the monitor). Until then VMS is shown as of the last stored check and the page says so. Below the counters is a **live map** with the same base map as the dashboard (OpenFreeMap, with a satellite option): paths coloured by live state, Bluetooth sensors and VMS signs as dots and squares, with layer buttons and full screen, reset and zoom controls; hover a path to see its live speed and travel time. Click a counter to list what is behind it and a row (or a map feature) to open the side panel (speed, travel time, typical and free-flow speed, since when, lifetime tier, last 20 checks; for a sensor, the paths that end at it) with *Show on map*, *Copy summary* and *Copy link*. Every list row also has a 📍 pin that jumps straight to its place on the map. If the live feed cannot be reached the page keeps the last data and says so. **How fresh is it?** The status bar shows *Updated HH:MM* (when your browser last fetched the snapshot) and *feed measured HH:MM* (the feed's own timestamp), both in Cyprus time; a time that is not from today gets its date in front (*16/11 22:05*), and a measurement over 20 minutes old turns the pill amber. The footer gives the age of the stored 6-hourly check behind the badges (*… check of 17/11/26 06:02 (3 h ago)*) and warns once it is over 9 hours old, which means the monitor may have stopped. Dark by default like the dashboard, with a Light/Dark button in the top bar that remembers the choice. Logic: `runner/attention.py`; page: `runner/live_ui.py`; built with the dashboard and committed by the workflow.

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

---

## Schedule

Tests run every 6 hours (triggered via cron-job.org). The workflow can also be triggered manually from the GitHub Actions tab.

A **weekly digest email** is sent every Monday at 07:30 Cyprus time (EEST). It summarises the past 7 days of sensor health, flagging sensors with **no good runs that week**, persistently unstable sensors, degraded/recovered sensors, and any sensors retired from the API feed. It also carries the same **Sensors by contract** census as the dashboard — each contract's Total / Working / Faults / Not-live counts, with the failing sensors expandable under each — so maintenance and response can be tracked per contractor. Sensors are named exactly as the dashboard names them (via `runner/labels.py`), with the raw ID alongside for quoting to a contractor. Because the digest scores a single week, its zero tier reads *"No good runs"* rather than the dashboard's lifetime *"Always off"*.

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
│   ├── attention.py            ← Live tab logic: down / new / recovered / never worked, feed outages, stored context
│   ├── live_ui.py              ← Live tab page (HTML, CSS, JS; polls the Worker every 5 min)
│   ├── stability.py            ← Shared health thresholds + the six stability tiers
│   ├── labels.py               ← Shared human-readable sensor names (report + digest)
│   ├── qa.py                   ← Reference-sheet ↔ API coordinate matching (ownership + commissioning)
│   ├── update_projects.py      ← Refresh sensor→project + commissioning in the DB after editing the workbook
│   ├── sensor_status.py        ← The one-sentence status that leads every sensor popup (current state, fault age, lifetime uptime)
│   ├── site_visits.py          ← Site visits tab: what to check on site, stops, driving order, days, CSV/KML exports
│   ├── digest.py               ← Weekly digest email builder and sender
│   └── bt_paths_map.py         ← Standalone BT path review map (duplicate/overlap detection, flag/note, CSV export)
├── results/
│   └── history.db              ← SQLite DB (auto-committed after each run)
├── reports/
│   ├── latest.html             ← Generated dashboard (auto-committed after each run)
│   └── bt_paths_map.html       ← BT paths review map (gitignored — regenerate with bt_paths_map.bat)
├── docs/
│   └── bt-paths-map.html       ← BT paths map served by GitHub Pages (auto-refreshed every 6h)
├── index.html                  ← Tabbed landing page (GitHub Pages root) switching Dashboard / BT Paths Map
├── QA Locations.xlsx           ← Reference equipment inventory (gitignored — local/cloud only)
├── .github/workflows/
│   ├── daily_tests.yml         ← GitHub Actions workflow (runs pytest, the API tests, then refreshes the published BT paths map)
│   └── weekly_digest.yml       ← Weekly digest email (Mondays 07:30 EEST, triggered via cron-job.org)
├── tests/
│   ├── test_generate_report.py ← Dashboard generator: current state, fault age, search tokens
│   ├── test_stability.py       ← Tier boundaries and health percentages
│   ├── test_attention.py       ← Live tab: counts, feed outages, Bluetooth sensors, stored context, script safety
│   ├── test_labels.py          ← Shared sensor-name rules
│   ├── test_digest_commissioning.py ← Digest leaves out awaiting-power / decommissioned sensors
│   └── test_data_quality.py    ← Coordinate matching and the retire guard
├── run.bat                     ← Local run script (double-click, loads .env automatically)
├── report.bat                  ← Regenerate dashboard HTML from existing DB without hitting the API
├── update_projects.bat         ← Refresh ownership/commissioning in the DB from QA Locations.xlsx
├── tools/                      ← control_room.py (the app), db_peek.py (read-only DB browser),
│                                 bt_direction_check.bat, bt_paths_map_live.bat
├── bt_paths_map.bat             ← Open the BT paths review map
├── control_room.bat             ← Local review app: map + saved flags + confirmed delete
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
| `feed_state` | Latest representative `measurement_timestamp` per live feed, used to work out how long a feed's data has been frozen |
| `bt_path_last_ts` | Each BT path's last reported `measurement_timestamp`, carried forward across runs so a path that stops sending a timestamp still counts as stale |
