# Control Room and BT Paths Review

Everything about the local review app (`control_room.bat`) and the BT paths review map. Back to the [README](../README.md).

## The BT paths review map

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

The copy at the link above (`docs/bt-paths-map.html`) **auto-refreshes every 6 hours** alongside the dashboard, so path
additions/deletions and the duplicate/overlap checks stay current on their own.

## The Control Room app

**Control Room (recommended for review work).** `control_room.bat` starts a small
app on your own machine (`127.0.0.1:8765`, nothing is exposed) and opens the same map
in your browser. Flags and notes are saved to `results/path_review.json` instead of
browser storage, and a **Review & delete…** button lists the flagged paths, runs a
dry run, and only after you type `YES` deletes them from the SWARCO API
(results are logged to `results/delete_log.csv`).

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
`results/control_room.log`. The window has its own icon and taskbar button (a road with two sensor dots,
`tools/control_room.ico`). For the same icon in Explorer and a button to pin, run `python tools/make_shortcut.py` once
(`--desktop` also puts it on the desktop): it makes a **Control Room** shortcut that opens the app window directly; pin it with
right-click > Pin to taskbar. Run it again after moving the folder or reinstalling Python.

**Reload app (top bar).** Restarts the Control Room with the latest code and the local database, for when files changed or you copied in a newer `history.db`; nothing is fetched from the live feed or OMNIA (that is *Refresh*). The window closes and reopens within a few seconds, in the same place and size (or maximized, or full screen) as it was; flags and notes are already saved.

**Who can delete.** The app is **read-only by default**: anyone can view the maps, compare
sensors and flag paths, but the *Review & delete…* button is absent (a *Read-only* badge
shows in the tab bar) and the server refuses delete requests. Deleting is switched on per PC
by adding `CONTROL_ROOM_ALLOW_DELETE=1` and `CONTROL_ROOM_DELETE_URL=<SWARCO delete address>` to
that PC's own `.env` (git-ignored, so a fresh clone never has either; the address is
deliberately not in the public code). Even then every delete needs the dry run and a typed `YES`.

## Sensor views

The same app also has a **Sensor locations** view (tabs for Traffic Detection / Bluetooth / VMS) that compares every API sensor with its
`QA Locations.xlsx` row and shows the distance between them. These pages only read
the database — they never change ownership data the way `update_projects.bat` does.

**Reading the sensor maps.** Above each map (Traffic Detection, Bluetooth, VMS) a summary line counts the dots - under 90% uptime, contract ended, retired or no history - and its buttons show only that group. Under it, the key lists every dot style with its count; click a style to show only that style (click it again to show all, ctrl+click adds or removes one). A dot's colour is lifetime uptime, except on the Bluetooth tab: Bluetooth sensors have no uptime of their own, so there the colour is the triangulation verdict (green working, orange works but some paths fail, red faulty, dashed grey not enough evidence). The key also explains the marks on top of the dots (gold halo = confirmed match, number = sensors at one spot, orange N = your note, big rings on Bluetooth = triangulation verdicts); a short distance line is marked by a dashed ring when you filter to it.

**Popups lead with a sentence.** Clicking a sensor on any sensor tab (and a Bluetooth sensor on the Paths tab) opens a popup whose
first line is one plain sentence in a coloured band: green *Working now*, orange *Working now, but it failed 6 of its last 20
runs* or *Failing in the latest check*, red *Down for 12 days (since 30 Oct): Speed = -1 (sensor fault)* or *Never worked since it
was first seen*, grey *Retired*, *Awaiting power*, *Decommissioned* or *No monitor history yet*. Lifetime uptime and the current
state now sit side by side (*Uptime (lifetime): 62% (Unstable) · Now: Down 12d*), because they answer different questions. A
sensor whose contract is active and that failed 80% of its last 20 runs also gets *A persistent fault for <project>*; one with
no contract on record says so; one whose contract ended says a failure is expected. Bluetooth sensors get their triangulation
verdict as the sentence. Rules: [how-the-checks-work.md](how-the-checks-work.md#9-the-popup-sentence).

**Notes on sensors.** Click a sensor on the Traffic Detection, Bluetooth or VMS tab (or a Bluetooth sensor on the Paths
tab) and write in the *Note* box of its popup; it saves by itself and the sensor gets an orange N badge on the map. Notes are kept in
`results/sensor_notes.json` on this PC only (git-ignored, so they never reach the public repo). Emptying the box removes the note.

**Which Bluetooth sensor is faulty? (Bluetooth tab → *Triangulation*).** A path only reports data when both
of its sensors work, so a path that works proves both ends are alive. A sensor with no working path, whose failing
paths lead to sensors that work elsewhere, is the likely culprit; a sensor with both working and failing paths is
only a *path problem*. The panel (bottom-left of the Bluetooth tab) lists the suspects, rings them on the map, and
each popup links to its paths on the Paths tab; it can export a CSV. **Show only** (next to *Export CSV*) hides every
other dot on the map and keeps just the sensors ticked in the panel; click it again (*Show all*) to bring the rest back. Runs during whole-feed outages are ignored,
and paths whose name doesn't match where they really end are left out rather than guessed.

## Site visits

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
[how-the-checks-work.md](how-the-checks-work.md#8-site-visits-what-goes-on-the-list).

## Routes

**Route times (Control Room, *Routes*).** Adds up the **live** travel times of Bluetooth paths. *A to B*: pick two existing nodes (type a
name, or click a node on the map) and get the fastest chain of paths between them, plus up to three alternatives, each with its total in
minutes, length, average speed and the paths used (drawn on the map with direction arrows). *Add paths*: type or click paths in order and
get the total; each path must start where the one before ends, and the tab says where two do not join. Only live readings are used: a
path with no reading is never guessed, it is left out of a fastest route and called out in a chain, so a total is never quietly short; a
warning shows when the live feed is more than 20 minutes old. The tab refreshes by itself every minute (while it is visible): the route or
chain on screen is re-timed in place, keeping the selected alternative and the map view, with a "Checked at hh:mm:ss, newest data from hh:mm" line (the feed only publishes a new reading every 5 minutes, a few minutes late, so a check can return the same numbers); if a refresh
fails the old numbers stay and the line says so. It works from the saved OMNIA data (Wiring tab, *Fetch from OMNIA*), so
paths with no start or end node there cannot be used (the tab lists them), and look-alike nodes with the same name within 100 m count as
one place.

*Gaps* (third mode) looks for **missing paths**: pairs of nodes within a chosen distance (3 km by default) whose shortest chain of paths
is at least N times longer than the straight line (3 by default), or does not exist. Lengths only, so a path with no live reading still
counts as joined. Each pair is judged both ways and listed by its worse direction, with the chain length, the number of paths, the road
node it passes through and the other direction's result; filter chips split *No chain* from *Detour*, and the list can be ordered worst
first or closest nodes first. Clicking a row draws the chain in amber with arrows and a dashed red line for the straight gap; *Save list
as CSV* exports it. It is a list of candidates to judge, not a verdict: a river, motorway or ring road can sit between two close nodes.
Nodes within 150 m of each other (setting *Same place*) count as **one place**: a new sensor put up beside an old one that is running out of
contract is a replacement, not a neighbour, so such a pair is never listed against each other, a chain may pass from one to the other, and a
row shows both names ("old / new").

## Live tab with contracts

**Live tab with contracts (Control Room, *Live*).** The Control Room has its own copy of the dashboard's Live tab (it reads the same public
live feed, every 5 minutes) with one addition the published page never has: each sensor's **contract (project)**. A VMS sign, a
Bluetooth sensor and, for a Bluetooth path, the sensors at its two ends show a *Contract* line in the side panel (with *(contract ended)*
for an out-of-support project and *no contract on record* where the spreadsheet has none), a small project line under each name in the
lists, and the contract in *Copy summary*. It is shown only when `QA Locations.xlsx` is on this PC (nothing in `.env` is needed; without
the file the tab says so and shows no contracts), and the project names come from the `sensor_projects` table that `update_projects.bat`
fills from the workbook: if you saved the workbook after the last update the tab warns you to run `update_projects.bat`. The published
`docs/live.html` is built without any of this, because the repo and the site are public and CI never has the workbook.

## Wiring and OMNIA edits

**Is a path wired to the right sensors? (Wiring tab).** In OMNIA every path has a start and an end *logical node*,
chosen by hand. The tab compares those with the API sensors (exact, case-sensitive names) and with where the path is
drawn, and lists wrong, swapped or missing nodes; it can export a CSV, and the Paths inspector and sensor popups carry
the same findings. Nodes are shown the way you think of them: the sensor's **site code and name** first and OMNIA's **node id small and grey** underneath
(a node no API sensor matches shows its name, "(no sensor)" and the node id). The node id is what OMNIA's edit call needs and the only way
to tell look-alike nodes apart, so it stays, in the edit boxes (which take node ids, not site codes: the two number ranges overlap, and the
text next to each box shows the site code and sensor of the node you typed), in the previews and in the logs; sorting and searching work on
site codes, and the CSV has two extra columns with the start and end site code. **Fetch from OMNIA** reads the node and path lists (read-only, needs the internal network, up to a
minute) and keeps `results/omnia_snapshot.json`, which is git-ignored. The OMNIA addresses come from the git-ignored `.env`.
Rules: [how-the-checks-work.md](how-the-checks-work.md).

**Changing many OMNIA paths at once (Wiring tab -> Bulk edit).** Tick paths (click a row to select only it, shift+click a range, ctrl+click adds or removes one, or tick a whole filter; up to 100 per batch, 50 for deleting; a counter at the bottom shows how many are selected; the first chip, **Show selected**, lists just the ticked paths so you can check them before editing, and keeps them in the list while you click around in it; the selection also survives switching filters), then set a
speed range, swap start and end node, set the nodes (or fill missing or wrong ones with the nearest node to the drawn end, clear matches only), and press *Preview changes*: OMNIA is read live and every path is
shown old value -> new value, nothing is sent. OMNIA needs all of
speed min, speed max, start and end node for every call, so a path with no nodes only changes once both nodes are given;
each path's current name is always sent back so nothing is renamed. Every change is logged with its old values in
`results/omnia_edit_log.csv`, and *Preview undo of last batch* puts the last batch back (paths edited by hand since are
left alone). Only whole numbers above 0 are accepted. **Rename is part of the same round:** give one path a
new name, or find/replace, a prefix or a suffix across the selected paths, or tick *name from its nodes* to build
`start->end` from the site codes of its two nodes' sensors, and *Preview changes* shows speeds, nodes and name together.
The name is built from the nodes the path will have after this change (typed, swapped or filled with the nearest node), so
a new path needs one round: speed range + *fill missing or wrong nodes with the nearest one* + *name from its nodes*. A path
that changes both is sent in one call; if the name cannot be built (its nodes lead to no known sensor) the preview says so
and only the speeds and nodes are changed. After sending, the names are read back from OMNIA to confirm they
changed, and *Preview undo of last rename* restores the old ones (`results/omnia_rename_log.csv`; the edit undo button
reverts only the speeds and nodes, each part has its own log).

**Reverse pairs (Wiring tab chips).** The drawn route decides whether a path has a reverse: another path drawn from this
path's end back to its start, with both ends within 30 m, or within 150 m when its nodes or its `A->B` name are swapped as
well (each direction of a road usually starts and ends on its own side of a junction). Swapped nodes or names never count on
their own, because they can be wrong. *Reverse pair to check by hand* lists the paths whose swapped nodes or name point at a
path that is drawn further off, or not in the API feed yet; each row says which path and how far apart the ends are.
*No reverse pair* lists the paths with no candidate at all; one-way streets have no reverse by design, so they are listed
too. With *Sort by Path ID, descending* the newest paths come first, so freshly made paths are easy to tick and send to
*Preview reverse path*, which follows the same rule.

**Preview reverse path, then create it (Wiring tab).** OMNIA stores a path as a chain of directed road arcs, and the
other direction of a road is a separate set of arcs (sometimes the same id + 500,000,000, often unrelated ids). Select a
path and press *Preview reverse path*: at each end of the path the tool asks OMNIA for the nearest arcs and keeps the one
running the opposite way that starts (or finishes) right at that end, then lets OMNIA's own router calculate the route
between those two arcs (it saves nothing). The reverse is accepted only if its length is within 10% of the forward path and
it starts and ends where the forward path ends and starts; a one-way street or a different route is reported instead.
Checked against a reverse made by hand in OMNIA (687 / 690): the tool found exactly the same arcs both ways. A path that
already has a reverse (by the rule above) is refused, and so is one whose possible reverse cannot be compared yet (for
example one created a moment ago that is not in the API feed); a possible reverse that the drawn routes rule out is shown
as a note only.
For the paths that pass, typing YES and *Create in OMNIA* does what OMNIA's own editor does on Save: asks OMNIA for a new path
id (never one an existing path has), builds the path from the two end arcs with the flipped name (`7052->7053` becomes
`7053->7052`), reads the new path's arcs back and stops if they differ from the preview, then copies the forward path's speed
range and its two nodes swapped. Needs `CONTROL_ROOM_ALLOW_EDIT=1`; every attempt is logged in `results/omnia_create_log.csv`,
and a wrong one is removed with *Delete selected paths*.

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
