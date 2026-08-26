# AP Monitor

A Flask app that monitors uptime for Location A's wireless network. It
ICMP-pings a list of switches and access points, records results, posts Slack
alerts when a device changes state, and serves a live D3.js topology map you can
drag to reposition, check on demand, and add devices to from the UI.

The system is split in two:

- **This web app** runs on Heroku — the dashboard, the API, Slack alerting, and
  the database. Heroku can't reach the the private device addresses, so it
  doesn't ping anything itself.
- **A Raspberry Pi agent** ([`agent/`](agent/)) runs on the device network, does the
  actual pinging, and POSTs results back up to the web app. See
  [`agent/README.md`](agent/README.md).

## Running locally

```bash
pip install -r requirements.txt
python run.py
```

This starts the web server on `http://localhost:5000`. With no `DATABASE_URL`
set (see below), it uses a local SQLite file at `data/monitoring.db`, created
automatically — zero setup, fully offline.

Off the device network nothing can be pinged, so every device reads `down`/`unknown`;
that's expected. To run the in-process pinger when you *are* on the device network
(instead of relying on the remote agent), set `ENABLE_LOCAL_PING=1`.

## Persistence: SQLite locally, Postgres in production

All durable state — ping history, the canonical device/switch list, map
positions, and per-device notes — lives in a database. The app picks its backend
from the `DATABASE_URL` environment variable:

| `DATABASE_URL` | Backend | Use |
|---|---|---|
| **unset** | SQLite (`data/monitoring.db`) | local dev — no setup, works offline |
| **set** | Postgres via psycopg3 | production (Heroku + Neon) |

Heroku's ephemeral filesystem wipes the dyno's disk on every restart (≈daily, plus
on each deploy), which is why production needs an external database — otherwise
added devices, dragged positions, and notes vanish on the next restart.

The device list is **seeded once** from [`config/devices.yaml`](config/devices.yaml)
into a `nodes` table the first time the app boots against an empty database.
After that, the database is the source of truth and the UI's "Add Device" flow
writes straight to it. Editing `devices.yaml` later has **no effect** on a
populated database — to re-import it you must reseed an empty `nodes` table.

### Setting up Neon (free Postgres) for production

Neon's free tier is permanent, needs no credit card, and gives 0.5 GB storage
(millions of ping rows) plus 100 compute-hrs/mo.

1. **Create the project** at [neon.tech](https://neon.tech). Pick the region
   closest to your Heroku app's region (e.g. Heroku `us` → Neon AWS `us-east`)
   to keep query latency low. The defaults are otherwise fine.

2. **Copy the pooled connection string.** In the project's *Connection Details*,
   turn **Connection pooling** on — the host will contain `-pooler`
   (e.g. `ep-xxx-pooler.us-east-2.aws.neon.tech`). Copy the full
   `postgresql://…?sslmode=require` string. The pooler (PgBouncer) tolerates the
   app's short-lived connections; use it rather than the "direct" string for the
   web app.

3. **Point Heroku at it** (quote the value so your shell doesn't mangle `?`/`&`):
   ```bash
   heroku config:set DATABASE_URL='postgresql://USER:PASSWORD@ep-xxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require'
   ```
   No migration command is needed — the schema is created idempotently at boot
   and the `nodes` table seeds itself from `devices.yaml` on first request.

4. **Deploy and verify.** Push, then `heroku logs --tail`; on the first boot
   against the empty database you should see
   `Seeded nodes table from devices.yaml (…)`. To confirm the durability fix
   that motivated all this: add a device via the UI → `heroku restart` → confirm
   it's still listed.

**Neon notes:** the legacy `postgres://` scheme is normalized to `postgresql://`
automatically, so paste whatever Neon gives you. Free-tier compute auto-suspends
after ~5 min idle and cold-starts in 1–2s on the next connection (the connection
pool reconnects through it transparently); the *project* is never paused.

## Multiple locations (sites)

The app supports multiple physical locations, each with its own device list, map,
activity log, and pinging agent. Locations are defined in `settings.yaml` under
`sites:` (first entry is the default view); the header has a location dropdown to
switch between them. Devices belong to a site (`nodes.site`); existing devices are
all **Location A** (the default).

Each site's agent authenticates with its own token:
- **Location A** (default): `AGENT_TOKEN` (unchanged).
- **Other sites**: `AGENT_TOKEN_<KEY>` where `<KEY>` is the site key upper-cased
  with dashes → underscores, e.g. `AGENT_TOKEN_LOCATION_B`.

One agent covers **all** locations: click **Download agent** (hamburger menu) for a
ready-to-run bundle (`ping_agent.py` + a `.env` pre-filled with `SERVER_URL` and
`SITE=all`), fill in the **master `AGENT_TOKEN`**, and install it on a Windows Server
that can reach every location's network (see [`agent/README.md`](agent/README.md)).
The agent maps each device's report to its location by IP.

> Device IPs and names must be unique **across all locations** (they key the
> per-device data tables).

## Environment variables (Heroku config vars)

| Variable | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | prod | Neon Postgres connection string (pooled, `sslmode=require`). Unset → local SQLite. |
| `SECRET_KEY` | prod | Flask session signing key. Without it, logins drop on every restart (an ephemeral key is generated and a warning logged). |
| `OKTA_ISSUER` | prod (SSO) | Okta OIDC issuer URL (e.g. `https://sso.example.com` or a custom auth-server URL). Enables SSO when set with the client vars below. |
| `OKTA_CLIENT_ID` / `OKTA_CLIENT_SECRET` | prod (SSO) | OIDC app credentials from Okta. |
| `OKTA_REDIRECT_URI` | prod (SSO) | The **sign-in redirect** URL registered in Okta, e.g. `https://your-app.herokuapp.com/auth/callback`. |
| `OKTA_LOGOUT_REDIRECT_URI` | prod (SSO) | The **sign-out redirect** URL registered in Okta (where users land after logout). Defaults to the app's own `/logged-out` confirmation page — whichever URL is used **must be added to the Okta app's "Sign-out redirect URIs"**. |
| `OKTA_SCOPES` | optional | OIDC scopes (default `openid email profile`). |
| `ADMIN_GROUP` | optional (SSO) | directory group whose members get **admin (edit) rights**; everyone else who logs in is **view-only**. Default `Network Monitor Admins`. Requires a **`groups` claim** configured on the Okta app (the org default authorization server doesn't release groups) — add a Groups claim to the app's ID token filtered to this group. Password-mode logins are always admin. |
| `SESSION_COOKIE_SECURE` | optional | `1`/`0`; defaults to `1` when SSO is enabled. Set `0` only for local HTTP testing. |
| `APP_PASSWORD` | prod | Shared-password gate (fallback used only when Okta isn't configured). Unset + no Okta → the app is open (dev convenience). |
| `ADMIN_PASSWORD` | prod | Extra password required to **delete a location** (and all its devices) from the UI. Unset → location deletion is disabled entirely. |
| `AGENT_TOKEN` | prod | Shared secret the Pi agent sends as `X-Agent-Token`. Must match the agent's `.env`. Unset → agent endpoints are unauthenticated (warned). |
| `SLACK_WEBHOOK_URL` | optional | Overrides the webhook in `settings.yaml` so the secret needn't live in committed config. |
| `APP_BASE_URL` | optional | Public app URL (e.g. `https://your-app.herokuapp.com`). When set, each device listed in a Slack outage alert links to its **view page** (`?device=<Device ID>&site=<key>`, keyed by the stable Device ID so it survives IP changes) instead of the bare hostname. Unset → alerts show the plain hostname/IP. |
| `POLLING_INTERVAL_MINUTES` | optional | Overrides the polling cadence in `settings.yaml`. |
| `ENABLE_LOCAL_PING` | optional | Set to `1` to run the in-process pinger (only useful when the app itself is on the device network). Off by default; production relies on the remote agent. |

## Configuration files

- [`config/settings.yaml`](config/settings.yaml) — polling cadence, Slack config,
  server host/port. The Slack `webhook_url` is a **placeholder** (treated as
  unconfigured); set the real webhook via the `SLACK_WEBHOOK_URL` config var — never
  commit a live one.
- [`config/devices.yaml`](config/devices.yaml) — the seed list of switches and
  access points. Canonical only until the database is first seeded (see above).

## Project layout

```
app/            Flask app factory, routes, database layer, monitor/notifier
agent/          Raspberry Pi ping agent (runs on the device network — see its README)
config/         settings.yaml + devices.yaml (seed)
static/         vanilla JS + D3 topology map, CSS
templates/      index.html (dashboard), login.html
run.py          local entrypoint (gunicorn is used in production — see Procfile)
```

## License

Released under the [MIT License](LICENSE) — Copyright (c) 2026 Alumni
Association of the University of Michigan. Author: Christina Adams.
