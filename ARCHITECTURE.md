# Architecture — how `main` actually works

This is a from-the-ground-up walkthrough of the codebase on `main`, for someone
picking up the project cold. It complements `README.md` (which documents the
*feature set*) by explaining the *mechanics* — what calls what, what's stored
where, and why.

---

## 1. The big picture

Every 6 hours, a GitHub Actions workflow runs `runner/run_tests.py`, which:

1. Hits every SWARCO DATEX II API endpoint listed in `config/endpoints.yaml`.
2. Records pass/fail + parsed sensor detail into `results/history.db` (SQLite).
3. Calls `runner/report.py` to regenerate `reports/latest.html` from the DB.
4. Commits both the DB and the HTML back to the repo, so GitHub Pages serves
   the latest dashboard and the DB carries full history forward.

A second workflow runs `runner/digest.py` every Monday, reading the same DB to
email a weekly summary.

Separately, and *not* part of the automated run, `runner/qa.py` /
`runner/update_projects.py` let a human match sensors to an external
ownership spreadsheet and persist that
mapping into the DB, so the dashboard can show who owns each sensor without
ever reading the spreadsheet directly.

```
                 ┌─────────────────────┐
  every 6h  ───▶ │   run_tests.py      │
  (GH Actions)   │  - hits SWARCO API  │
                 │  - writes DB        │
                 │  - calls report.py  │
                 └─────────┬───────────┘
                           │
                           ▼
                 results/history.db  ◀──────── update_projects.py
                    (SQLite, committed)         (manual, offline,
                           │                     reads QA Locations.xlsx)
                           ▼
                 reports/latest.html
                 (committed → GitHub Pages)

  every Monday ──▶ digest.py  ── reads DB ──▶ emails weekly summary
```

---

## 2. Configuration files (the "no code change" layer)

Two YAML files drive almost everything so that adding a sensor group or
renaming a panel doesn't require touching Python.

### `config/endpoints.yaml`

Defines every API endpoint to test, grouped under `groups: → name`. Each
endpoint entry can declare:

- `checks` — list of check function names (see `runner/tests.py`) run against
  the response body.
- `health_check` — which check produces a per-sensor health percentage (used
  by the dashboard's group cards and trend chart).
- `sensor_group` — store this endpoint's per-sensor results under a different
  group name than its parent (e.g. `Bluetooth Paths Live (FCD)` lives under
  the `Bluetooth` group in the config, but its sensor rows are tagged
  `"Bluetooth Paths"` so they get their own map layer/trend line).
- `coords_extract` — which coordinate-extraction function to run against the
  *inventory* endpoint's response (`measurement_site`, `vms`, or `bt_paths`).

### `config/ui_labels.yaml`

Cosmetic layer: panel titles, per-group display name/color/icon/map-layer-key,
and the staleness-warning threshold. `runner/report.py` reads this into
`GROUP_META` at import time and drives colors, icons, and column headers off
it — nothing about a group's visual identity is hardcoded in Python.

### `config/projects.csv`

Maps a project/contractor name to an accountability level:
`supported` (failures are actionable) or `out_of_support` (contract has
ended, failures are expected and shown separately, not chased).

---

## 3. The test run (`runner/run_tests.py`)

`run_all()` is the entry point:

1. Reads `BASE_URL`/`SWARCO` from environment (GitHub Secrets in CI, `.env`
   locally via `run.bat`).
2. Loops every group → every endpoint in `endpoints.yaml`.
3. For each endpoint, `run_single()`:
   - GETs the URL, with one retry on timeout/error.
   - Checks HTTP status and response time against configured limits.
   - Runs each named check function from `runner/tests.py`'s `REGISTRY` against
     the raw response text.
   - A check can return `sensors` (a `{sensor_id: status}` dict) and, in
     `LIVE_MODE`, `measurements` (raw per-sensor data) — these get persisted
     per-sensor, not just as one pass/fail per endpoint.
4. Every result is written via `db.insert_result()` (one row per endpoint per
   run in `test_results`) and, for endpoints with sensor detail,
   `db.insert_sensor_result()` (one row per sensor per run in `sensor_results`).
5. If the endpoint was an *inventory* feed (`coords_extract` set) and it
   passed, `_process_coords()` extracts lat/lon via `runner/geo.py` and
   upserts them into `sensor_coords` / `bt_path_coords`, then **retires**
   (marks `active=0`) any previously-known sensor that's no longer in the
   feed. This retire-on-absence step only fires on a *passing* inventory
   fetch — a failed/empty fetch must never be mistaken for "all sensors
   removed."
6. `db.insert_run()` writes the run-level summary row.
7. `report.generate_report()` is called to regenerate the HTML dashboard from
   whatever is now in the DB.
8. Process exits non-zero if anything failed/errored — this fails the GitHub
   Actions job (visible in the Actions tab) without blocking the commit step
   (`if: always()` in the workflow).

### Check functions (`runner/tests.py`)

Each check parses SWARCO's DATEX II XML and returns
`{"passed": bool, "detail": str, "sensors": {...}, "measurements": {...}}`.
The two that matter most for per-sensor health:

- `vms_controller_status` — walks `vmsControllerStatus` elements, classifies
  each as `working` / `not_working` / `no_status` based on the
  `workingStatus` field.
- `sensor_speed_status` — walks `siteMeasurements` (Traffic Detection),
  classifies each as `working` / `no_traffic` (speed=0) / `malfunctioning`
  (speed=-1) / `no_measurement` (missing).

Both return a `sensors_map` used by `run_tests.py` to populate `sensor_results`
— the authoritative record every health number is computed from. They also bake
a human-readable summary string (e.g. `"Working: 4 | Not working: 32 — IDs: 12,
14, ... | No status: 7 — 17, 21..."`) into `check_summary`, but that string is
for **display only** (the group-card detail breakdowns). No percentage is ever
derived from it — see §6.

`REGISTRY` at the bottom of the file maps check names (as used in
`endpoints.yaml`) to these functions.

---

## 4. Coordinate extraction (`runner/geo.py`)

Three extractor functions pull lat/lon out of DATEX II inventory XML:

- `extract_measurement_site_coords` — Traffic Detection loops and Bluetooth
  sites (`measurementSiteRecord` → `pointCoordinates`).
- `extract_vms_coords` — VMS controllers.
- `extract_bt_path_coords` — predefined Bluetooth paths, which are polylines
  (multiple coordinate pairs, GML geometry) rather than single points.

These feed `sensor_coords` (points, used for the map markers and QA matching)
and `bt_path_coords` (polylines, used to draw path lines on the map).

---

## 5. The database (`runner/db.py`)

SQLite file at `results/history.db`, committed to git after every run (this
is intentional — the DB *is* the deployment artifact that carries state
between CI runs, since GitHub Actions runners are stateless).

`get_connection()` is called by every script that touches the DB (not just
`run_tests.py`) and runs the full `CREATE TABLE IF NOT EXISTS` schema plus
`_migrate()` on every connection — so any script, run in any order, always
sees an up-to-date schema. Migrations are additive and idempotent
(`ALTER TABLE ... ADD COLUMN` guarded by `_has_column` checks) — never destructive.

| Table | Grain | Purpose |
|---|---|---|
| `runs` | one row per run | timestamp + pass/fail/error totals |
| `test_results` | one row per endpoint per run | status, HTTP code, timing, `check_summary` text |
| `sensor_results` | one row per sensor per run | `status` (e.g. `working`/`not_working`), optional `data` JSON (LIVE_MODE only) |
| `sensor_coords` | one row per sensor (latest) | lat/lon/name/site_code, `active` flag, `last_seen` |
| `bt_path_coords` | one row per BT path (latest) | polyline coordinates as JSON, `active` flag |
| `sensor_projects` | one row per sensor | `project`, `source` (`matched`/`colocated`), `commissioning` (`active`/`not_electrified`/`decommissioned`) |

Key distinction: `sensor_results` is the **authoritative per-sensor, per-run
history** — every stability calculation that needs to be *correct* (not just
a fast approximation) should read from here, not from `test_results.check_summary`
text (see the pitfall in §6).

`db.py` exposes fetch helpers used throughout `report.py` and `digest.py`:
`fetch_sensor_stability()` (full per-sensor history), `fetch_sensor_projects()`
(ownership + commissioning), `fetch_sensor_coords()` / `fetch_bt_path_coords()`
(for the map), `fetch_sensor_health_history()` (per-run health % text, used
for the trend chart), and others.

---

## 6. The dashboard (`runner/report.py`)

`generate_report()` is the single entry point. It:

1. Loads config (`GROUP_META` from `ui_labels.yaml`, `HEALTH_ENDPOINTS`/
   `SENSOR_CHECKS` derived from `endpoints.yaml`).
2. Pulls everything it needs from the DB in one pass (runs, latest results,
   sensor stability history, sensor coords/paths, sensor→project mapping).
3. Builds every panel as an HTML string and assembles them into one page —
   there is no client-side data fetching; the page is fully static once
   generated, aside from small inline `<script>` blocks for interactivity
   (map, search/sort/filter on the stability table, chart zoom/pan).

### Everything numeric comes from `sensor_results`

Every health number on the dashboard is derived from the per-sensor, per-run
rows in `sensor_results` — never from re-parsing the human-readable
`check_summary` text that `tests.py` wrote. Two shapes of the same source:

- **Group-level, per run** — `db.fetch_sensor_status_counts()` aggregates
  `{run_id: {group: {status: count}}}`; `_pct_from_counts()` turns that into a
  percentage (`good / total`, `GOOD_STATUSES = {"working", "ok"}`). Feeds the
  System Overview badges and the Health Trend chart.
- **Per-sensor history** — `db.fetch_sensor_stability()` returns each sensor's
  full status history. Feeds the stability tier, the sparkline, and the current
  fault age.

Commissioning-excluded sensors (awaiting power / decommissioned) must not count
against a group's health, and they are removed **by `(group, sensor_id)`**
before counting — `fetch_sensor_status_counts(excluded=...)`. Excluding by a
*count* rather than by ID was a real bug: because the number of genuinely-active
VMS fluctuated near the excluded count, the denominator kept collapsing to equal
`working`, silently forcing every run to 100% and erasing a real outage.

`check_summary` survives only for **display**: the group-card detail breakdowns
(`parse_vms_detail` / `parse_bt_detail` / `parse_sensor_detail`) and
`_humanize_failure`. Nothing that produces a percentage reads it. Reintroducing
a regex over that string to compute a number would be a regression — it is an
implicit, untested contract with `tests.py`'s message wording.

### Panels, top to bottom

- **System Overview** — one card per group. `group_status_card()` computes
  `min_health_pct` across that group's checks (from the latest run's status
  counts) and picks a status word: `Operational` (≥90%), `Deteriorated` (≥80%,
  i.e. `HEALTH_WARNING_PCT`), or `Feed issue`/`Degraded` if the endpoint itself
  failed regardless of sensor %. `_live_total()` subtracts commissioning-
  excluded counts from the "X/Y working" display (clamped to never show
  working > total — a display-only safety clamp, not used for the percentage).
- **Sensor Map** — Leaflet.js, built from `_build_map_sensor_list()` (points)
  and `bt_path_coords` (polylines). Marker color encodes status; commissioning-
  excluded sensors render grey regardless of last status. Historical playback
  scrubs through the last 30 runs, sorted by the raw ISO timestamp (sorting the
  formatted `dd/mm/yy` label instead put 01/07 before 30/06). Client-side
  controls (all in the map's inline `<script>`): a **contract filter** (isolates
  one contract's markers — each marker carries `_contract`; paths belong to no
  contract so any contract selection hides them), a **hide-not-live** toggle
  (`_notLive`), a **Streets/Satellite** base-layer swap, and a **full-screen**
  button. Full screen targets a wrapper (`#mapFsWrap`) around *both* the toolbars
  and the map, laid out flex-column, so the controls stay usable at full size —
  not just the map canvas. Each point pop-up carries a **View history →** link
  (`jumpToStability()`) that opens the Sensor Stability panel and that sensor's
  trend row.
- **Sensors Health Trend** — one line per group, `_build_chart_data()` +
  `_build_health_by_run()`, from the per-run status counts above.
- **Sensors by contract** — `build_contract_summary_html()` over
  `stability.contract_census()`: every contract (plus a *No maintenance plan*
  bucket) with Total / Working / Faults / Not-live counts, so a fully-healthy
  contract stays visible — the old "Attention needed" panel only showed
  contracts that had a live fault, hiding e.g. a contractor whose whole fleet is
  awaiting power. **Faults** uses `stability.windowed_fail()` — a *persistent*
  problem, failed ≥80% of the last 20 runs (`ATTENTION_FAIL_RATIO` /
  `ATTENTION_WINDOW_RUNS`) — not merely `history[-1]` bad, so a one-cycle blip
  doesn't count. Rows with faults expand to the failing sensors (display name,
  recent fail count, and current fault age from `current_state()`). Out-of-support
  contracts show faults neutrally (expected, not actionable); commissioning-
  excluded sensors count as *Not live*, never faults. The same `contract_census()`
  feeds the weekly digest (§9), so the two never disagree.
- **Sensor Stability** — per-sensor table driven by `fetch_sensor_stability()`,
  one row per sensor with a **Current state** cell (`current_state()`: Working /
  Down Nd / Never worked), a 20-run sparkline, the **lifetime** stability tier
  (`tier_for_counts()`), and the project owner.
- **Run History** — last 30 runs, one row each, per-group % + overall API
  response status.

---

## 6a. The BT paths review tool (`runner/bt_paths_map.py`)

A separate, standalone generator — not part of the daily test run — for
manually auditing the ~500 legacy Bluetooth travel-time paths (built up over
10 years, many overlapping or duplicated). It reads `bt_path_coords` and the
Bluetooth `sensor_coords` straight from the DB and renders a self-contained
Leaflet map with no other groups and no health data.

- **Duplicate detection** (`find_duplicate_groups`) — paths sharing the same
  name are compared by status-history match percentage; a near-100% match is
  auto-collapsed to one entry (the rest noted in the console output), a
  partial match is left for manual review (flagged in the UI, not collapsed).
  `--show-duplicates` disables collapsing entirely, for eyeballing every
  registration before deciding what to retire from the API.
- **Overlap detection** — point-to-polyline distance checks flag paths whose
  lines run suspiciously close together; clicking a line or endpoint marker
  cycles through the overlapping candidates in place, bolding whichever one is
  currently selected.
- **Reviewer findings persist client-side only** — flagging a path OK/problem
  and any note is kept in the browser's `localStorage` (key `btPathFlags`),
  never sent anywhere. This means findings are per-browser/per-machine and
  are lost on a fresh profile — the **CSV export/import** buttons exist
  specifically so a reviewer can back up their findings or hand them to
  someone else, merging by path ID without wiping local-only entries. Flags
  for paths no longer present in the API (retired/removed) are silently
  pruned on load rather than lingering as orphaned entries.
- **Cycling scope** — Prev/Next normally step through all paths alphabetically;
  opening the flagged-paths list forces cycling to be scoped to just that list
  (otherwise "next" is nearly useless — the next alphabetical path can be
  anywhere on the map).

**Sharing with colleagues:** `python runner/bt_paths_map.py --publish` (via
`publish_bt_map.bat`) additionally writes the generated page to
`docs/bt-paths-map.html`, which is tracked in git and served by GitHub Pages
alongside the main dashboard (Pages source is `main` / `/ (root)`, so nothing
under `docs/` needs a separate Pages config). `index.html` at the repo root is
a small tabbed landing page — plain iframes, no build step — so one URL
(`https://atsiakkaris.github.io/traffic-control-room/`) can switch between the
live dashboard and this tool instead of needing two separate links.

---

## 7. Ownership & commissioning (`runner/qa.py`, `update_projects.py`)

The API can tell you a sensor exists and whether it's currently working — it
can't tell you who owns it or whether it's even supposed to be working yet
(e.g. installed but not yet connected to power). That information only
exists in an external spreadsheet.

**Matching (`qa.py`):**

1. `load_reference()` reads a sheet (`path.xlsx::SheetName` syntax), parsing
   each row's name, lat/lon, `Project`, and `Status` (→ `commissioning`:
   `active` / `not_electrified` / `decommissioned` based on keyword matching
   against `_NOT_ELECTRIFIED` / `_INACTIVE_VALUES`).
2. `match_sensors()` builds every (reference row, API sensor) pair within
   `max_dist`, sorts all candidate pairs by distance, and assigns **greedily
   shortest-first** — a global nearest-neighbor matching, not naive per-row
   nearest-match, so ambiguous cases resolve to the overall best pairing.
   Names are never compared; matching is purely geographic.

   The radius is set per group in `update_projects.GROUPS`, and the
   `qa_*.bat` launchers must pass the same value or the QA report will
   disagree with what the DB holds:

   | Group | `max_dist` | Why |
   |---|---|---|
   | Traffic Detection | 300m | Dense urban loops — a wider radius reaches a different junction |
   | Bluetooth | 300m | As above |
   | VMS | 500m | Signs are sparse (median nearest-neighbour 1.45km) and their reference coordinates are approximate. At 300m, "VMS A1" missed the API sign of that exact name by 93m |

   `COORD_MATCH_MAX_M` (500m) is only the fallback when no `max_dist` is given.

   Claimed rows are tracked by **row position, not by name**. Two rows can
   legitimately share a name — two distinct points on Georgiou Griva Digeni
   Avenue. Keying on the name let the first match swallow the second row: it
   could never match, *and* the `ref_only` pass skipped it, so it disappeared
   from the results entirely. Every reference row now surfaces exactly once,
   as either a `match` or a `ref_only`.
3. `annotate_accountability()` gives every matched API sensor its reference
   row's `project` + `commissioning`, then **propagates to co-located
   twins**: many road installations have two physical sensors pointed in
   opposite directions (e.g. `TCC`/`ACC`, `Eastbound`/`Westbound`) that the
   spreadsheet lists as a single row. Any *unmatched* API sensor within
   `COLOCATION_M` (15m) of an already-matched one inherits that sensor's
   project — same installation, same contract.

   `match_sensors()` reads the same `COLOCATION_M` when it labels a sensor
   `colocated`. It once used a local 10m, so a sensor 12m from its twin
   inherited the project but was still reported as `api_only` in the QA view.
   One constant, both paths.

   **Known gap:** a sensor the spreadsheet has no row for at all cannot be
   matched, and surfaces as "Unassigned." This is a data task, not a code one
   — add the row and re-run `update_projects.py`. Two separate junctions that
   share a street name are *not* an instance of this: each needs its own row,
   and once both rows exist the matcher pairs them correctly (see step 2).

   Unassigned sensors record *why* in `sensor_projects.source`, so the
   dashboard can tell the two cases apart in a tooltip rather than showing one
   undifferentiated grey dash:

   | `source` | Meaning |
   |---|---|
   | `matched` | Paired directly with a reference row |
   | `colocated` | Inherited from a co-located matched twin |
   | `unmatched_no_ref:<m>` | Nearest row is `<m>` metres away, beyond threshold — the row does not exist |
   | `unmatched_ref_taken:<m>` | A row sits `<m>` metres away, inside threshold, but a closer sensor claimed it |
   | `unmatched_no_coords` | The API reports no coordinates for this sensor |

4. The result is persisted, not just displayed: `update_projects.py` writes `{sensor_id: {project, source,
   commissioning}}` into `sensor_projects` via `db.upsert_sensor_projects()`.
   Because `report.py` only ever reads the DB, the dashboard never depends on
   the spreadsheet being present at report-generation time (important, since
   CI doesn't have it — it's gitignored and lives on a separate cloud drive).

**Workflow to update ownership:**
```
1. Edit QA Locations.xlsx locally (the data owner's copy)
2. python runner/update_projects.py (or update_projects.bat)
   — matches offline against coordinates already in the DB, no API call
3. git commit results/history.db && git push
4. → next automated report shows the updated ownership
```

---

## 7a. Sensor names (`runner/labels.py`)

Single source of truth for how a sensor is shown to a human, shared by
`report.py` and `digest.py` so the same sensor never appears under two names.

`sensor_display_name(group, sensor_id, name, site_code)`:

| Group | Label | Why |
|---|---|---|
| Traffic Detection | `1040 (100)`, `1010 (Gr. Dhigeni Ave. (TCC))` | 22 of 104 loops have a NULL `name` but *all* carry a `site_code`; a bare id names no road |
| VMS | `A1 Highway Limassol-Nicosia (Alambra) (8)` | VMS have names, never site codes |
| Bluetooth Paths | `1004->1008` | falls back to the bare id — a few paths are unnamed in the feed |

Feed names arrive with embedded newlines (`"Gr. Dhigeni Ave.\n (TCC)"`), which
previously landed inside a `data-display` HTML attribute. The helper collapses
whitespace. `with_id(label, id)` appends the raw id for quoting to a contractor,
matching on digit boundaries so sensor `23` isn't judged "already present" by
site code `1023`.

---

## 8. Stability tiers (`runner/stability.py`)

Single source of truth for the six-tier badge system, shared by both
`report.py` and `digest.py` so a threshold change can't drift between them:

| Tier | Threshold (lifetime, raw ratio) |
|---|---|
| Always on | ≥99% |
| Healthy | ≥90% |
| Intermittent | ≥70% |
| Unstable | ≥40% |
| Critical | >0% — has worked at least once |
| Always off | `good_runs == 0` — never once reported |

The digest reuses these tiers but computes them over a **single week**, so it
relabels the zero tier as **"No good runs"** (`_WEEK_TIER_LABEL`): "Always off"
is a lifetime claim and would overstate a week's data.

`GOOD_STATUSES = {"working", "ok"}` — anything else (`not_working`,
`malfunctioning`, `no_status`, `no_traffic`, `no_measurement`, `failing`) counts
as bad for tiering purposes, regardless of which group it came from.

### Three metrics, deliberately separate

The dashboard answers three different questions and must never conflate them:

| | Question | Where it lives |
|---|---|---|
| **Stability tier** | "Can I trust this sensor?" | `tier_for_counts()` over the sensor's **whole lifetime** |
| **Current state / fault age** | "Is it down *now*, and for how long?" | `current_state()` (in `stability.py`; `report._current_state` is a back-compat alias) |
| **Persistent fault** | "Is this a standing problem a contractor owes a response on?" | `windowed_fail()` over the **last 20 runs** (failed ≥80%) |

The tier is a lifetime average and is *intentionally* slow to move — a sensor
repaired yesterday still reads badly. That is correct for a trust rating and
wrong for a maintenance list, so the **Sensors-by-contract "Faults" count is keyed
off the persistent-fault window**, not the tier: a contractor is held to sensors
that are consistently failing right now, with the lifetime tier riding along as
context ("new fault, or a repeat offender?").

`tier_for_counts(good, total)` gets two things right that a bare percentage
cannot:

* **"Always off" means `good == 0`** — never once reported. That is a defensible
  claim to put to a contractor, unlike a percentage that merely rounds to zero.
  One good run ever ⇒ `Critical`, not `Always off`.
* **The ratio is compared unrounded.** Rounding first created a cliff at 0.5%:
  2 good runs in 500 (0.40%) landed in `Always off` while 3 in 500 (0.60%)
  landed in `Critical`.

`tier_for(pct)` remains for `digest.py`, which only ever holds a percentage
(computed per week, so its rounding cannot reach a false zero).

Below `TIER_MIN_RUNS` (5) recorded runs the badge shows a neutral "Collecting
data" rather than a falsely precise tier.

> **Why the 20-run window drives the contract "Faults" count but not the tier.**
> A rolling window is deliberately kept *out* of the lifetime **stability tier**:
> it is cadence-dependent (20 runs ≈ 5 days at 6-hourly but ≈ 1.7 days at
> 2-hourly), so letting it drive the trust badge would mean changing the cron
> frequency silently changed what every badge meant. The contract **"fault"** is a
> different question — "is this a standing problem *right now*?" — where a recent
> window is exactly what you want, and the cadence caveat is acceptable because the
> number is read for action, not as a lifetime claim. `windowed_fail()` also
> requires `TIER_MIN_RUNS` recorded runs before it will call a fault, so a
> just-installed sensor isn't branded a contractor's problem on two data points.
> Net: lifetime tier = no window; current fault age = exact days; contract fault =
> last 20 runs.

Covered by `tests/test_stability.py`.

---

## 9. Weekly digest (`runner/digest.py`)

Independent of `report.py` — reads the same DB, builds its own HTML email.

`build_digest()`:
1. `fetch_sensor_health_by_day()` — per-sensor daily good/total for the last
   14 days (so this week can be compared to the previous week).
2. `fetch_active_sensors()` — sensors currently `active=1` in `sensor_coords`.
3. `fetch_excluded_commissioning()` — sensors currently
   `not_electrified`/`decommissioned` per `sensor_projects`; subtracted from
   the active set so they don't drag down tier counts or week-over-week %,
   mirroring the dashboard's exclusion logic.
4. Computes `this_pct`/`prev_pct` per sensor, then buckets into
   `always_off` (0% this week), `persistent` (<70% both weeks), `degraded`
   (dropped >15pt), `recovered` (rose >15pt).
5. `fetch_retired_this_week()` — sensors that flipped `active=0` in the last
   7 days (i.e. dropped from the API feed entirely), with names resolved from
   `sensor_coords`/`bt_path_coords` rather than shown as bare IDs.
6. `fetch_contract_census()` — the **Sensors by contract** section, computed by
   the *same* `stability.contract_census()` the dashboard panel uses (§6), so the
   email and the live dashboard can't disagree about a contract's totals or which
   sensors are failing. Rendered as a per-contract counts table plus an
   expandable failing-sensor list under each contract.

Sent via Gmail SMTP (`GMAIL_USER`/`GMAIL_APP_PW` secrets) to `NOTIFY_EMAIL`,
triggered by the `weekly_digest.yml` workflow (itself fired externally by
cron-job.org, not GitHub's own `schedule:` trigger — see below).

---

## 10. Automation (`.github/workflows/`)

Both workflows are `workflow_dispatch`-only (no `on: schedule:` block) —
they're triggered externally by **cron-job.org** hitting the GitHub Actions
API on a schedule. This was a deliberate choice after GitHub's native cron
scheduler proved unreliable for this project's cadence; cron-job.org pings
`daily_tests.yml` roughly every 6 hours and `weekly_digest.yml` every Monday
07:30 EEST.

`daily_tests.yml`: checkout → install deps → run `run_tests.py` → commit
`results/history.db` + `reports/latest.html` back to the repo (as the
`github-actions[bot]` identity) → push → upload the HTML as a build artifact.
The commit step runs `if: always()`, so even a failing test run still
publishes whatever the DB/report ended up looking like.

`weekly_digest.yml`: checkout → install deps → run `digest.py`. No commit
step — it only sends an email.

---

## 11. Extending the system

Both documented in `README.md`'s "Adding Endpoints and Groups" section, but
in short: **adding an endpoint** to an existing group is a pure
`endpoints.yaml` edit. **Adding a whole new sensor group** touches three
files — `ui_labels.yaml` (visual metadata), `endpoints.yaml` (what to test),
and `tests.py` (a new check function registered in `REGISTRY`, only if the
group needs custom per-sensor status parsing). Nothing in `report.py`,
`digest.py`, or the map JS needs to change — they're all driven off
`GROUP_META` and the DB's `group_name` column.
