# Running the demo

This is a **self-contained demo build**. It runs entirely on `localhost` with fake,
seeded data — no external database, no login/SSO, no Slack, and no ping agent. Every
device, location, status, event, and log entry is invented sample data whose only
purpose is to show off the app's features.

## Run it

```bash
pip install -r requirements.txt
DEMO_DATA=1 python run.py
```

Then open **http://localhost:5000**.

On first launch a disclaimer popup confirms it's a demo. The app auto-creates a local
SQLite database (`data/monitoring.db`) and seeds it with the demo fleet + history.

## What `DEMO_DATA=1` does

- **Seeds fake data** on first boot (only when the database is empty) — two locations,
  switches + APs, current/past outages, an unknown device, monitoring & notification
  pauses, a frequent-outage device, events (lightning, maintenance, an ongoing power
  outage) with a linked pause, a decommissioned device, notes, app-downtime, a
  scheduled pause, and a user-activity trail.
- **Shows the "demo version" disclaimer** popup on load.
- **Hides the "agent went quiet" banner** (there's no agent — the data is static).

Everything else works normally against the seeded data: the topology map, sidebar,
filters, logs, User Activity, events, Deleted Devices, per-device panels, Intermapper
links, notes, and so on. Because there's no agent, statuses don't change on their own —
the seeded snapshot is what you see.

## Re-seeding

The seed runs only when the database is empty. To start over with a fresh set of demo
data, delete the database and relaunch:

```bash
rm -f data/monitoring.db
DEMO_DATA=1 python run.py
```

## Notes

- Don't set `DEMO_MODE=1` for this build — that's a *different* mode (it hides/abstracts
  identifiers for screen-recording a real deployment). Here the data is already fake, so
  you want it shown in full: use `DEMO_DATA=1` only.
- No `DATABASE_URL`, `APP_PASSWORD`, `OKTA_*`, `SLACK_*`, or `AGENT_TOKEN` are needed.
- Everyone is an admin in this open, no-login build, so all edit controls are available.
