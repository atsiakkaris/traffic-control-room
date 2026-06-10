# SWARCO Infrastructure Health — API Test Suite

Automated twice-daily API tests for SWARCO DATEX II endpoints, running on GitHub Actions with SQLite history and a live HTML dashboard.

---

## Project Structure

```
├── config/
│   └── endpoints.yaml          ← Define / add endpoints here
├── runner/
│   ├── run_tests.py            ← Entry point
│   ├── checks.py               ← XML assertion logic per endpoint type
│   ├── db.py                   ← SQLite helpers
│   └── report.py               ← HTML dashboard generator
├── results/
│   └── history.db              ← SQLite DB (auto-committed after each run)
├── reports/
│   └── latest.html             ← Generated dashboard (auto-committed after each run)
├── .github/workflows/
│   └── daily_tests.yml         ← GitHub Actions schedule (runs at 06:00 and 12:00 Cyprus time)
└── requirements.txt
```

---

## How It Works

1. GitHub Actions triggers at **06:00 and 12:00 Cyprus time (03:00 and 09:00 UTC)**
2. All endpoints in `endpoints.yaml` are tested — HTTP status, response time, and XML assertions
3. Results are written to `results/history.db`
4. `reports/latest.html` is regenerated with the full dashboard
5. Both files are committed back to the repo automatically
6. GitHub Pages redeploys — the live report URL reflects the latest run within ~1 minute
7. An email summary is sent to the configured recipient

---

## Endpoints & Checks

| Group | Endpoint | Checks |
|---|---|---|
| VMS | VMS Inventory | Valid XML |
| VMS | VMS Live Data | Valid XML, working/not-working/no-status controller counts |
| Bluetooth | BT Inventory | Valid XML |
| Bluetooth | BT Paths Inventory | Valid XML, predefined path count > 0 |
| Bluetooth | BT Paths Live (FCD) | Valid XML, speed & travel time present for all paths |
| Traffic Detection | TD Inventory | Valid XML |
| Traffic Detection | TD Live | Valid XML, sensor speed categorisation (working / no traffic / malfunctioning / no data) + avg flow rate |

---

## GitHub Secrets Required

Go to **Settings → Secrets and variables → Actions → Repository secrets**:

| Secret | Value |
|---|---|
| `BASE_URL` | Base URL, e.g. `https://datex.example.com/` |
| `SWARCO` | Path segment, e.g. `swarco/api/Data/` |
| `GMAIL_USER` | Gmail address used to send notifications |
| `GMAIL_APP_PW` | Gmail App Password (16-char, not your regular password) |
| `NOTIFY_EMAIL` | Recipient address for the daily email summary |

### Creating a Gmail App Password
1. Go to [myaccount.google.com](https://myaccount.google.com) and search **App passwords**
2. Create one named `API Tests` — copy the 16-character code
3. Paste it as the `GMAIL_APP_PW` secret

---

## Adding New Endpoints

Open `config/endpoints.yaml` and add an entry under the appropriate group (or create a new group):

```yaml
- name: My New Endpoint
  path: MyNewEndpointSuffix
  expected_status: 200
  max_response_ms: 5000
  checks:
    - valid_xml
```

Available checks: `valid_xml`, `vms_controller_status`, `predefined_paths_count`, `bt_paths_speed_and_traveltime`, `sensor_speed_status`.

Commit and push via GitHub Desktop — the next scheduled run picks it up automatically.

---

## Resetting History

To clear all historical data and start fresh, delete `results/history.db` via GitHub Desktop or directly on GitHub. The next run creates a new empty database.

---

## Viewing Results

- **Live dashboard**: your GitHub Pages URL at `https://YOUR_USERNAME.github.io/YOUR_REPO/reports/latest.html` (updates after every run)
- **Email**: summary table sent after each run to `NOTIFY_EMAIL`
- **Raw data**: `results/history.db` is a standard SQLite file — open with [DB Browser for SQLite](https://sqlitebrowser.org/) for ad-hoc queries
- **Artifacts**: each run uploads `latest.html` as a downloadable artifact under Actions → the run → Artifacts

---

## Schedule

Runs twice daily. Edit `.github/workflows/daily_tests.yml` to change times:

```yaml
- cron: "0 3 * * *"   # 06:00 Cyprus (UTC+3)
- cron: "0 9 * * *"   # 12:00 Cyprus (UTC+3)
```

Use [crontab.guru](https://crontab.guru) to build a custom schedule. Note: GitHub cron is always UTC.

---

## GitHub Actions Permissions

Go to **Settings → Actions → General → Workflow permissions** and set **Read and write permissions** — required so the bot can commit `history.db` and `latest.html` back to the repo after each run.