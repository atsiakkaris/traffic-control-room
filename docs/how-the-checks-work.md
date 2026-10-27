# How the Bluetooth checks decide what is "good", "failing" or "faulty"

This explains the rules the monitor and the Control Room use for Bluetooth (BT) paths and sensors, in plain
language, so they can be explained to colleagues. Every number below is a setting in the code (named in
brackets), not a guess made on the day.

There are three layers, each built on the one before (plus a separate configuration check, section 6):

1. **One reading** of one path (the monitor, every 6 hours) -> `ok`, `failing`, `stale` or `missing`.
2. **The path's history** (last 20 readings) -> *good*, *no data*, *unrealistic*, *stale*, *intermittent* or *unknown*.
3. **The sensors at the ends of paths** -> *faulty*, *works but some paths fail*, *working*, and so on (triangulation).

---

## 1. One reading of a path

A BT **path** (for example `1015->1009`) is a stretch of road between two BT sensors. It only reports a speed
and travel time when **both** of its sensors saw the same device. The monitor asks the live feed about every
path every 6 hours and records one status per path (`runner/tests.py`):

| Status | When it is recorded |
|---|---|
| **ok** | The speed is above 0, the travel time is above 0, and the measurement is no older than 30 minutes (`DEFAULT_STALE_HOURS`). |
| **failing** | The speed or the travel time is `-1`, 0, or absent. `-1` is the feed's "malfunctioning" value. |
| **stale** | The values are positive, but the measurement timestamp is more than 30 minutes old: the path reported once and then went quiet. |
| **missing** | The path is in our inventory but the live feed did not list it at all in this run. |

If a path is both without data and old, it is recorded as **failing** (no data comes first).

**Unrealistic speed.** A positive speed under 5 km/h or over 140 km/h (`MIN_SPEED_KMH`, `MAX_SPEED_KMH`) is
still recorded as `ok` (the sensors did detect something), but with a small marker saying the speed is
unrealistic. `-1` is *not* unrealistic: it is "no data".

---

## 2. The path's history: how a path is classified

A single bad reading means little (a path can have no traffic at 3 a.m.). So a path is judged over its **last
20 readings** (`WINDOW_RUNS`). With fewer than **5** readings (`MIN_RUNS`) it is **unknown**: not enough
history to say anything. The "consistently bad" line is **80 %** of the window, i.e. 16 of 20 (`BAD_RATIO`).

The checks are applied in this order and the first one that matches wins (`runner/bt_data_quality.py`):

| Category | Rule |
|---|---|
| **No data** | `failing` or `missing` in at least 80 % of the window. |
| **Unrealistic** | Positive but unrealistic speed in at least 80 % of the window. |
| **Stale** | `stale` in at least 80 % of the window. |
| **Unknown** | Fewer than 5 readings recorded. |
| **Intermittent** | Anything other than `ok` in at least 20 % of the window, but not consistently. |
| **Good** | Anything other than `ok` in less than 20 % of the window (at most 3 of 20). |

Two extra notes shown on the Paths page:
- **Bad since** is when the current unbroken bad streak began.
- **Recovering** appears when a path still looks bad because the window remembers the problem, but its newest
  **4** readings (a full day) were all good (`RECOVERING_RUNS`). It is only a note; the category does not change.

**Example, path 643** (2026-10-22): 17 of the last 20 readings were `failing`, i.e. 85 %. That is above 80 %, so the
category is **No data**, "bad since 21/10 18:02", not recovering. It is a borderline case: with 2 fewer
failing readings (15 of 20, 75 %) it would be *intermittent*, even though the feed sometimes shows a speed for it (for example 47 km/h
at 09:52, between two monitor readings).

---

## 3. Which sensors are at the two ends of a path

To blame a sensor we first need to know which sensors a path joins (`runner/bt_sensor_suspects.py`).

- **Named paths** like `1015->1009`: the numbers are the sensors' *site codes*. The sensor with that code
  must really be within **250 m** of the path's start or end point. If it is not, but a different sensor
  clearly is, the name is wrong and the path is set aside as a **name mismatch** instead of being guessed.
- **Other names** (for example `643`): the sensor nearest to the path end counts, but only if it is within
  **100 m** and the next-nearest sensor is at least **30 m** further away.
- A path whose end cannot be tied to one clear sensor, or whose two ends are the same sensor, is left out of
  the sensor verdicts.

---

## 4. Which readings are used for sensors

- **Outages are ignored.** If fewer than **half** of all paths were `ok` in a monitor run, the whole feed was down
  and says nothing about single sensors, so that run is dropped (`OUTAGE_OK_SHARE`). The newest **20 healthy
  runs** from the last 150 readings are used (`LOOKBACK_RUNS`).
- A path's state is then worked out from those runs with the same rules as section 2:

| Path state | Comes from | Meaning for its sensors |
|---|---|---|
| **good** | Good | Both sensors are alive. |
| **odd** | Unrealistic speed | Both sensors are alive (they detect devices), the speed is just wrong. |
| **bad** | No data, or Stale | Something is wrong at one end or on the path. |
| **mixed** | Intermittent | Unclear. It proves nothing either way. |
| **unknown** | Unknown | Too little history. Ignored. |

---

## 5. The sensor verdict

The idea: a path works only if **both** its sensors work, so a working path *proves* both ends are alive. A
sensor that has no working path at all, while its paths to *proven-alive* sensors fail, is the suspect.

> **Example.** A->B and B->C fail; D->A and J->C work. A and C are proven alive (their other paths work), so
> the failing paths point at their common end, B.

| Verdict | Rule |
|---|---|
| **Faulty (high confidence)** | No path through it works, and at least **2** of its failing paths lead to sensors that are proven alive by other paths. |
| **Faulty (likely)** | No path through it works, but the proof is thinner: fewer than 2 failing paths to proven-alive sensors, or both ends of the failing path are unproven (so the blame is shared). |
| **Works, but some paths fail** | It has working **and** failing paths. The sensor is alive; look at the failing *paths* (distance, direction, configuration) instead. |
| **Probably fine** | Its failing paths all end at a sensor judged *faulty (high confidence)*. |
| **Working** | It has working paths and none failing. |
| **Not enough evidence** | No usable path, or only unclear ones (mixed or unknown). |

**Limits to keep in mind when explaining it:**
- It is a hint for where to look first, not proof. A failing path can also be caused by the path itself.
- A path that is only *borderline* bad (like 643 at 85 %) counts as bad. Several such paths can push a
  sensor towards "faulty" on thin evidence, so read the reason text in the popup.
- Paths shorter on history than 5 healthy readings, and name-mismatched or unresolved paths, are excluded.
- A new sensor or path has few readings and may show as "Not enough evidence" for a while.

---

## 6. The wiring check: is a path wired to the sensors it is drawn between?

Everything above judges *behaviour*. This part checks the *configuration*. When a path is created in OMNIA, someone
picks a start and an end **logical node** (OMNIA's copy of a BT sensor location). The path only gets data from the
sensors behind those two nodes, so a wrong or missing node means a path that is drawn between A and B but reads from
C, or from nothing. The **Wiring** tab compares three things (`runner/bt_wiring.py`, data from `runner/omnia_fetch.py`):

- the **configured** start/end nodes of every path (read from OMNIA, read-only, saved as a snapshot),
- the **drawn** path ends (the coordinates in the API feed), and
- the **API sensors**.

**Step 1 - is each node a real sensor?** The node's name must equal the API sensor's name **exactly, including
capitals** (some sensors differ only in case). Several sensors share a name, so the one **at the node's location**
(within 15 m) is meant.

| Node status | Rule |
|---|---|
| Name and location match | A sensor has exactly this name and is within 15 m of the node. |
| Name matches, but it is somewhere else | Exact name, but the nearest such sensor is more than 15 m away: the sensor or the node was moved. |
| A sensor is here, with a different name | No sensor has this name, but one sits within 15 m: renamed on one side. |
| Differs only by capitals or spaces | Never matched automatically; the look-alike is shown. |
| No sensor | Nothing matches by name or place. |

**Step 2 - is each path wired to what it is drawn between?** For each end the distance from the drawn end to the
configured node is measured (the real data: median 40 m, 80% within 100 m).

| Finding | Rule |
|---|---|
| Start or end node not set | The node is empty, or its id is not a known node. |
| A node has no matching sensor | Step 1 found no sensor for it. |
| Wired to a different sensor than it is drawn to | The drawn end is more than 150 m from its node **and** a different sensor is clearly there. |
| Start and end nodes are the wrong way round | The nodes fit the drawn ends only when swapped. |
| Start and end node are the same | Both ends use one node. |
| Drawn end is far from its node | More than 500 m away and no other sensor explains it. |
| Drawn end is a little off its node | 150 to 500 m: only "check" (road snapping is normal up to about 100 m). |
| Path name disagrees with its nodes | A name like `1001->1002` whose numbers differ from the site codes of the nodes' sensors. |
| Ends next to a second sensor | The node's sensor has another sensor within 60 m, so the wiring cannot tell from where the path ends which of the two it uses: check by hand. |
| Speed range missing or not min < max | Only for paths that have nodes. |
| Drawn path not found in OMNIA / OMNIA path not in the API feed | The path exists on one side only. |

**Missing nodes: the nearest node is suggested.** For a path with no start or end node, the finding also names the
node closest to the drawn end, with the distance: up to 150 m and a node that stands for a sensor is "likely", 150 to
500 m is "check by hand", and beyond 500 m nothing is suggested. It is also "check by hand" when a second node is just
as close (within 150 m of the end and within 60 m of the first node), or when the path's own `A->B` name gives another
site code than the nearest node's sensor. Only the clear suggestions can be applied: in Bulk edit, tick "fill missing
nodes with the nearest one". It fills only missing or unknown nodes, never replaces a node that is set, never uses the
same node for both ends, and still needs a speed range if the path has none.

The page also lists the nodes that need attention and the API sensors no node stands for. OMNIA is only read; the
**Fetch from OMNIA** button needs the internal network and keeps a snapshot, so the check also works offline.

## 7. Where to see each result

| Where | What it shows |
|---|---|
| **Paths tab, inspector** | The path's category, percentage, "bad since" and the last stored values (section 2). |
| **Paths tab, click a sensor** | The sensor's verdict, reason, and its paths with their states (sections 3-5). |
| **Bluetooth sensors tab, Triangulation panel** | All suspects, rings on the map, and a CSV export. |
| **Wiring tab** | Every path with a wiring finding, the nodes that need attention, and a CSV export (section 6). |
| **Paths tab, inspector and sensor popups** | A "Wiring" line for the selected path; the OMNIA node and path count in a sensor popup. |

The settings are listed at the top of `runner/bt_data_quality.py` (path categories) and
`runner/bt_sensor_suspects.py` (sensor verdicts).
