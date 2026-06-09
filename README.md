# SWARCO API Test Suite

Automated daily API tests for SWARCO DATEX II endpoints, running on GitHub Actions with SQLite history and HTML reports delivered by email.

---

## Project Structure

```
├── config/
│   └── endpoints.yaml          ← Define / add endpoints here
├── runner/
│   ├── run_tests.py            ← Entry point
│   ├── checks.py               ← XML assertion logic
│   ├── db.py                   ← SQLite helpers
│   └── report.py               ← HTML report generator
├── results/
│   └── history.db              ← SQLite DB (auto-committed by CI)
├── reports/
│   └── latest.html             ← Generated report (auto-committed by CI)
├── .github/workflows/
│   └── daily_tests.yml         ← GitHub Actions schedule
└── requirements.txt
```

---

## Setup (one-time)

### 1. Create a GitHub repository

Push this folder as a new repo:
```bash
git init
git add .
git commit -m "Initial commit"
gh repo create swarco-api-tests --private --source=. --push
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name    | Value |
|----------------|-------|
| `BASE_URL`     | Your base URL, e.g. `https://datex.example.com/` |
| `SWARCO`       | The path segment, e.g. `swarco/` |
| `GMAIL_USER`   | Your Gmail address, e.g. `you@gmail.com` |
| `GMAIL_APP_PW` | A Gmail **App Password** (see below) |
| `NOTIFY_EMAIL` | Where to send the daily report |

### 3. Create a Gmail App Password

1. Go to [myaccount.google.com/security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already on
3. Search for **App passwords** → select **Mail** → **Other (custom name)** → type `API Tests`
4. Copy the 16-character password — paste it as the `GMAIL_APP_PW` secret

### 4. Enable GitHub Actions write permission

Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions** (needed to commit `history.db` back to the repo after each run).

### 5. Run manually to test

Go to **Actions → Daily API Tests → Run workflow**.

---

## Adding new endpoints

Open `config/endpoints.yaml` and add an entry under the appropriate group:

```yaml
- name: My New Endpoint
  path: MyNewEndpointSuffix
  expected_status: 200
  max_response_ms: 5000
  checks:
    - valid_xml
```

Available checks: `valid_xml`, `vms_controller_status`, `predefined_paths_count`, `bt_paths_speed_and_traveltime`, `sensor_speed_status`.

Commit and push — the next run will include it automatically.

---

## Viewing results

- **Email**: a summary table is sent every day after the run.
- **HTML report**: committed to `reports/latest.html` — viewable as a GitHub Pages site, or download it from the Actions artifacts tab.
- **Raw data**: `results/history.db` is a standard SQLite file — open with [DB Browser for SQLite](https://sqlitebrowser.org/) for ad-hoc queries.

---

## Schedule

Edit `.github/workflows/daily_tests.yml` to change the time:
```yaml
- cron: "0 7 * * *"   # 07:00 UTC every day
```
Use [crontab.guru](https://crontab.guru) to build your preferred schedule.
