# File Reference Manual

Quick reference for maintaining and extending the SWARCO Infrastructure Health test suite.

---

## config/endpoints.yaml

**What it does:** Defines every endpoint to test. This is the only file you need to edit for day-to-day maintenance.

**Structure of one entry:**
```yaml
- name: My Endpoint          # Display name shown in the report
  path: MyEndpointSuffix     # URL suffix — appended to BASE_URL + SWARCO
  expected_status: 200       # Expected HTTP response code
  max_response_ms: 5000      # Response time limit in milliseconds
  checks:                    # List of test functions to run (see checks.py)
    - valid_xml
    - my_custom_check
```

**To add a new endpoint:**
1. Add an entry under the right group (or create a new group with `- name:` and `endpoints:`)
2. Add at minimum `valid_xml` under checks
3. Commit and push — the next run picks it up automatically

**Available checks:** `valid_xml`, `vms_controller_status`, `predefined_paths_count`, `bt_paths_speed_and_traveltime`, `sensor_speed_status`

**Note:** HTTP status code and response time are always checked automatically regardless of the `checks:` list.

---

## runner/checks.py

**What it does:** Contains one Python function per check type. Each function receives the raw XML response text and returns a result dict:
```python
{"passed": True/False, "detail": "Human-readable explanation"}
```

**The Registry:** At the bottom of the file is a dictionary that maps the string name you write in `endpoints.yaml` to the actual Python function:
```python
REGISTRY = {
    "valid_xml": valid_xml,
    "sensor_speed_status": sensor_speed_status,
    ...
}
```
This is how `run_tests.py` knows which function to call when it reads `checks: [valid_xml]` from the YAML.

**To add a new check:**
1. Write a new function that takes `response_text: str` and returns `{"passed": bool, "detail": str}`
2. Add it to the `REGISTRY` dict at the bottom with a string key
3. Use that string key in `endpoints.yaml` under `checks:`

**Existing checks:**
- `valid_xml` — confirms the response parses as valid XML
- `vms_controller_status` — counts working / not-working / no-status VMS controllers
- `predefined_paths_count` — confirms at least 1 predefined BT path exists
- `bt_paths_speed_and_traveltime` — checks all BT paths have speed > 0 and travel time > 0
- `sensor_speed_status` — categorises traffic sensors as working / no traffic / malfunctioning / no data, reports avg flow rate

---

## runner/run_tests.py

**What it does:** The main entry point. Orchestrates the entire test run:
1. Reads `config/endpoints.yaml`
2. For each endpoint: makes the HTTP GET request, measures response time, checks HTTP status, runs each check from `checks.py`
3. Writes every result to `results/history.db`
4. Calls `report.py` to generate the HTML dashboard
5. Sends an email summary (currently disabled — see below)
6. Exits with code 1 if any tests failed (makes GitHub Actions show a red X)

**To re-enable email:** Find this line and uncomment it:
```python
# send_email(run_id, run_at, totals, all_results, report_path)
```

**To change the HTTP timeout:** Find `max_ms / 1000 + 10` — the `+ 10` adds 10 seconds of extra headroom beyond the `max_response_ms` defined in the YAML.

**You rarely need to edit this file** unless you want to change how tests are executed fundamentally.

---

## runner/report.py

**What it does:** Reads the SQLite database and generates the entire `reports/latest.html` dashboard. Called automatically at the end of every run.

**Key function:** `generate_report()` — builds the HTML string and writes it to `reports/latest.html`. Everything you see in the browser comes from here.

**Helper functions at the top** parse the failure reason strings stored in the DB into structured data for display:
- `parse_vms_detail()` — extracts working/not-working controller counts and IDs
- `parse_bt_detail()` — extracts path counts from BT failures
- `parse_sensor_detail()` — extracts sensor category counts
- `build_sensor_stability()` — queries the DB for all runs and counts pass/fail per endpoint to produce the stability badges

**To change the report layout or styling:** Edit the HTML/CSS inside `generate_report()`. The dashboard is plain HTML with inline styles — no framework needed.

---

## runner/db.py

**What it does:** All SQLite database operations. Creates the DB schema on first run, provides functions to insert and query data.

**Two tables:**
- `runs` — one row per workflow run (run_id, timestamp, pass/fail/error counts)
- `test_results` — one row per endpoint per run (all test details including failure reason)

**You will rarely need to edit this file.** The only reason to touch it is if you want to store additional data (e.g. add a new column). If you do, add the column to the `CREATE TABLE` statement and update the relevant `insert_*` function.

**To reset history:** Delete `results/history.db` from the repo. The next run creates a fresh database automatically.

**To inspect data manually:** Open `results/history.db` with [DB Browser for SQLite](https://sqlitebrowser.org/) — a free GUI tool for browsing and querying SQLite files.

---

## .github/workflows/daily_tests.yml

**What it does:** Tells GitHub Actions when and how to run the test suite.

**Schedule:** Two cron entries — change these to adjust the run times. GitHub cron is always UTC:
```yaml
- cron: "0 3 * * *"   # 06:00 Cyprus (UTC+3)
- cron: "0 9 * * *"   # 12:00 Cyprus (UTC+3)
```
Use [crontab.guru](https://crontab.guru) to build cron expressions.

**`workflow_dispatch:`** — enables the manual "Run workflow" button in the GitHub Actions tab. Keep this.

**Secrets:** The `env:` block passes your GitHub Secrets as environment variables to the Python script. If you rename a secret in GitHub Settings, update the matching line here too.

**Write permissions:** The workflow commits `history.db` and `latest.html` back to the repo after each run. This requires **Read and write permissions** under Settings → Actions → General → Workflow permissions.

---

## requirements.txt

Lists the two Python packages the script needs:
- `httpx` — makes the HTTP requests to your endpoints
- `pyyaml` — reads `endpoints.yaml`

If you ever add new Python functionality that needs an external library, add it here and it will be installed automatically on the next run.
