import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ── Dialect selection ────────────────────────────────────────────────
# DATABASE_URL set (Heroku/Neon) → Postgres via psycopg3 + a small pool.
# Unset → local SQLite, exactly as before (zero-setup offline dev).
# The two dialects differ in only a handful of places (placeholder token,
# upsert syntax, autoincrement), handled by the q() helper and small
# `if _PG` branches — not worth a full ORM for a 6-table schema.
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    # Heroku/Neon sometimes hand out the legacy scheme; psycopg wants this one.
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]
_PG = bool(DATABASE_URL)

DB_PATH = os.environ.get(
    'DATABASE_PATH',
    os.path.join(os.path.dirname(__file__), '..', 'data', 'monitoring.db'),
)

_pool = None


def _get_pool():
    """Lazily build the Postgres connection pool. Imported here (not at module
    top) so an offline developer without psycopg installed is unaffected when
    DATABASE_URL is unset. gunicorn runs 1 worker × 4 threads, so a small pool
    covers concurrent requests; Neon's idle-suspend cold starts are handled by
    psycopg_pool's reconnect."""
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool
        from psycopg.rows import dict_row
        # min_size=0 so the pool holds NO idle connection: when the app goes quiet
        # the pool won't keep reconnecting, letting Neon's serverless compute
        # auto-suspend (and stay suspended) to conserve compute-hours. The next
        # query opens a connection on demand (Neon cold-starts in ~1-2s). max_idle
        # closes lingering idle connections promptly for the same reason.
        _pool = ConnectionPool(
            DATABASE_URL, min_size=0, max_size=5, max_idle=60.0,
            kwargs={'row_factory': dict_row}, open=True,
        )
    return _pool


def get_conn():
    """Return a connection usable as `with get_conn() as conn:`. Both dialects
    commit on clean block exit; the Postgres one also returns to the pool."""
    if _PG:
        return _get_pool().connection()
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql: str) -> str:
    """Translate the SQLite '?' placeholder to Postgres '%s' when on PG. SQL in
    this module never contains a literal '?' or '%' otherwise, so a plain swap
    is safe."""
    return sql.replace('?', '%s') if _PG else sql


def _norm(row) -> dict:
    """Normalize a result row to a plain dict, converting any datetime values to
    naive-UTC ISO strings. Postgres returns real datetimes for time columns;
    the frontend expects naive ISO strings it can suffix with 'Z' (see map.js).
    SQLite already stores/returns strings, so this is a no-op there."""
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.replace(tzinfo=None).isoformat()
    return d


# ── Schema ───────────────────────────────────────────────────────────
# One statement per list entry so the same loop initializes both dialects
# (psycopg has no executescript). Types differ only where they must.

def _schema() -> list[str]:
    if _PG:
        serial_pk = "id BIGSERIAL PRIMARY KEY"
        ts_default = "TIMESTAMP NOT NULL DEFAULT (now() AT TIME ZONE 'utc')"
        ts = "TIMESTAMP"
    else:
        serial_pk = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        ts_default = "DATETIME DEFAULT CURRENT_TIMESTAMP"
        ts = "DATETIME"
    return [
        f"""CREATE TABLE IF NOT EXISTS ping_history (
                {serial_pk},
                device_ip TEXT NOT NULL,
                node_id INTEGER,
                timestamp {ts_default},
                status TEXT NOT NULL,
                response_ms REAL,
                reported_by TEXT
            )""",
        f"""CREATE TABLE IF NOT EXISTS device_states (
                device_ip TEXT PRIMARY KEY,
                node_id INTEGER,
                current_status TEXT DEFAULT 'unknown',
                last_check {ts},
                last_change {ts},
                alert_active INTEGER DEFAULT 0,
                reminded INTEGER DEFAULT 0,
                down_strikes INTEGER DEFAULT 0,
                last_sample {ts},
                reported_by TEXT
            )""",
        """CREATE TABLE IF NOT EXISTS device_positions (
                device_ip TEXT PRIMARY KEY,
                node_id INTEGER,
                x REAL NOT NULL,
                y REAL NOT NULL
            )""",
        # A SEPARATE saved map layout for mobile (its own Edit Map). Same shape as
        # device_positions; kept in its own table so a device can have both a desktop
        # and a mobile position. A device with no row here falls back to its desktop
        # position, so the mobile map starts identical to the desktop one.
        """CREATE TABLE IF NOT EXISTS device_positions_mobile (
                device_ip TEXT PRIMARY KEY,
                node_id INTEGER,
                x REAL NOT NULL,
                y REAL NOT NULL
            )""",
        # GEOGRAPHIC position, for locations with an aerial basemap: where the
        # device physically is. Deliberately NOT split desktop/mobile like the two
        # tables above — a building is in one place — and stored as lat/lng rather
        # than image pixels so positions survive replacing or re-cropping the
        # imagery (the frontend projects them to pixels at render time).
        """CREATE TABLE IF NOT EXISTS device_positions_geo (
                device_ip TEXT PRIMARY KEY,
                node_id INTEGER,
                lat REAL NOT NULL,
                lng REAL NOT NULL
            )""",
        """CREATE TABLE IF NOT EXISTS app_flags (
                key TEXT PRIMARY KEY,
                value TEXT
            )""",
        # Locations. Seeded once from settings.yaml, then managed via the UI.
        # id gives a stable display order (first = default site).
        f"""CREATE TABLE IF NOT EXISTS sites (
                {serial_pk},
                key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at {ts_default},
                pending_delete_at {ts}
            )""",
        # Per-device/switch notes, keyed by IP like states/positions/history.
        f"""CREATE TABLE IF NOT EXISTS device_notes (
                device_ip TEXT PRIMARY KEY,
                node_id INTEGER,
                note TEXT NOT NULL DEFAULT '',
                updated_at {ts}
            )""",
        # Canonical device/switch list (replaces the YAML extra file). Seeded
        # once from devices.yaml, then managed via the UI. x/y carry the
        # YAML layout baseline; device_positions overrides for dragged nodes.
        # kind is 'switch', 'ap', or 'other'. No CHECK constraint — the kind set
        # is validated in the app layer so adding new kinds needs no migration.
        f"""CREATE TABLE IF NOT EXISTS nodes (
                ip TEXT PRIMARY KEY,
                node_id INTEGER,
                name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                site TEXT NOT NULL DEFAULT 'location-a',
                location TEXT,
                hostname TEXT,
                intermapper_url TEXT,
                switch TEXT,
                uplink TEXT,
                x REAL,
                y REAL,
                enabled INTEGER NOT NULL DEFAULT 1,
                notify INTEGER NOT NULL DEFAULT 1,
                created_at {ts_default},
                decommissioned_at {ts}
            )""",
        # Periods a device's monitoring/notifications were paused. A row is opened
        # (resumed_at NULL) on pause and closed on resume — mirrors how outages are
        # stored. `kind` = 'monitoring' (agent stops pinging) or 'notifications'
        # (still pinged, just no Slack).
        f"""CREATE TABLE IF NOT EXISTS pause_periods (
                {serial_pk},
                device_ip TEXT NOT NULL,
                node_id INTEGER,
                paused_at {ts},
                resumed_at {ts},
                kind TEXT NOT NULL DEFAULT 'monitoring'
            )""",
        # Scheduled monitoring pauses: a named future window that pauses a set of
        # devices (JSON list of IPs) between start_at and end_at. The always-on
        # watchdog thread flips their `enabled` flag at the boundaries.
        f"""CREATE TABLE IF NOT EXISTS scheduled_pauses (
                {serial_pk},
                site TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                start_at {ts} NOT NULL,
                end_at {ts} NOT NULL,
                device_ips TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                mode TEXT NOT NULL DEFAULT 'monitoring',
                category TEXT,
                description TEXT,
                event_group_id TEXT,
                created_at {ts_default}
            )""",
        # App-downtime intervals: one row per detected monitoring gap (the site's
        # agent/app went quiet). Written only when a gap is detected — NOT per
        # cycle — so it grows with real outages, not with time. The "last report"
        # watermark that gaps are measured against lives in app_flags
        # (last_report:<site>). An open interval (end_ts NULL) is never stored;
        # an ongoing outage is synthesized at read time from a stale watermark.
        f"""CREATE TABLE IF NOT EXISTS app_downtime (
                {serial_pk},
                site TEXT NOT NULL,
                start_ts {ts},
                end_ts {ts}
            )""",
        "CREATE INDEX IF NOT EXISTS idx_ping_ip_time ON ping_history(device_ip, timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_pause_ip ON pause_periods(device_ip)",
        "CREATE INDEX IF NOT EXISTS idx_app_downtime_site ON app_downtime(site, start_ts)",
        # Audit trail: one row per successful, user-initiated mutating request.
        # Written by the after_request hook in routes.py — never per-ping/agent
        # traffic, so it grows only with human actions (no auto-pruning yet).
        f"""CREATE TABLE IF NOT EXISTS user_activity (
                {serial_pk},
                ts {ts_default},
                actor TEXT NOT NULL,
                method TEXT NOT NULL,
                endpoint TEXT,
                path TEXT,
                summary TEXT NOT NULL,
                status INTEGER,
                site TEXT
            )""",
        "CREATE INDEX IF NOT EXISTS idx_user_activity_ts ON user_activity(ts)",
        # Maintenance windows: an admin-marked [start_at, end_at) range for one
        # device whose overlapping portion of an outage is re-labeled "maintenance"
        # (shown blue, and excluded from frequent-outage counting when it fully
        # covers an outage). Purely an overlay on the derived outages — nothing in
        # ping_history changes.
        # "Events" overlay (formerly maintenance-only): one row per device. `category`
        # is the event type (maintenance | lightning | power_outage | other); `note`
        # holds the free-text description; `event_group_id` ties together the per-device
        # rows created by one "Mark an Event" action so they edit/delete/display as one.
        # Existing rows default to category='maintenance', event_group_id NULL.
        f"""CREATE TABLE IF NOT EXISTS maintenance_windows (
                {serial_pk},
                device_ip TEXT NOT NULL,
                node_id INTEGER,
                start_at {ts} NOT NULL,
                end_at {ts} NOT NULL,
                note TEXT,
                created_by TEXT,
                category TEXT NOT NULL DEFAULT 'maintenance',
                event_group_id TEXT,
                created_at {ts_default}
            )""",
        "CREATE INDEX IF NOT EXISTS idx_maintenance_ip ON maintenance_windows(device_ip)",
        # idx_maintenance_group is created by _migrate_add_event_fields (after the
        # column is guaranteed to exist on pre-existing tables).
    ]


def init_db():
    with get_conn() as conn:
        for stmt in _schema():
            conn.execute(stmt)
        _migrate_add_enabled(conn)        # must run before the rebuild below
        _migrate_allow_other_kind(conn)
        _migrate_add_reminded(conn)
        _migrate_add_site(conn)
        _migrate_add_debounce(conn)
        _migrate_collapse_ping_history(conn)
        _migrate_drop_heartbeats(conn)
        _migrate_add_site_pending_delete(conn)
        _migrate_add_user_activity_site(conn)
        _migrate_backfill_user_activity_site(conn)
        _migrate_add_notify(conn)
        _migrate_add_pause_kind(conn)
        _migrate_add_scheduled_pause_mode(conn)
        _migrate_add_reported_by(conn)
        _migrate_add_event_fields(conn)
        _migrate_add_node_hostname(conn)
        _migrate_add_node_decommissioned(conn)
        _migrate_add_node_id(conn)
        _migrate_add_intermapper_url(conn)


def _migrate_add_site_pending_delete(conn):
    """Add sites.pending_delete_at (when a scheduled soft-delete becomes permanent;
    NULL = not pending). Idempotent."""
    ts = 'TIMESTAMP' if _PG else 'DATETIME'
    if _PG:
        conn.execute(f"ALTER TABLE sites ADD COLUMN IF NOT EXISTS pending_delete_at {ts}")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(sites)").fetchall()]
        if 'pending_delete_at' not in cols:
            conn.execute(f"ALTER TABLE sites ADD COLUMN pending_delete_at {ts}")


def _migrate_add_notify(conn):
    """Add nodes.notify (1 = Slack alerts on). Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS notify INTEGER NOT NULL DEFAULT 1")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'notify' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN notify INTEGER NOT NULL DEFAULT 1")


def _migrate_add_node_hostname(conn):
    """Add nodes.hostname — the DNS name the agent pings (falls back to IP when NULL/
    empty). The IP stays the identity/routing key; hostname is only the ping target.
    Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS hostname TEXT")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'hostname' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN hostname TEXT")


def _migrate_add_node_decommissioned(conn):
    """Add nodes.decommissioned_at — when a device was removed from the map / list /
    ping targets. NULL = active; a timestamp = decommissioned (row kept so its logs
    stay in Device Logs, but excluded from the live fleet). Idempotent."""
    ts = 'TIMESTAMP' if _PG else 'DATETIME'
    if _PG:
        conn.execute(f"ALTER TABLE nodes ADD COLUMN IF NOT EXISTS decommissioned_at {ts}")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'decommissioned_at' not in cols:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN decommissioned_at {ts}")


_PERDEVICE_TABLES = ('ping_history', 'device_states', 'device_positions',
                     'device_positions_mobile', 'device_positions_geo',
                     'device_notes', 'pause_periods', 'maintenance_windows')


def _ensure_column(conn, table, col, decl):
    """Add `col` to `table` if missing (idempotent, both dialects). Names are hard-coded
    constants, never user input."""
    if _PG:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {decl}")
    else:
        cols = [r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate_add_intermapper_url(conn):
    """Add nodes.intermapper_url — the device's page on U-M's Intermapper monitoring
    site, shown as a link on the device view. Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS intermapper_url TEXT")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'intermapper_url' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN intermapper_url TEXT")


def _migrate_add_node_id(conn):
    """Phase 3a of the IP → Device ID migration: give every device a STABLE surrogate
    `node_id` and add a backfilled `node_id` to each per-device table. Purely additive
    and reversible — `device_ip` stays the authoritative key here; a later phase flips
    reads/caches to node_id. Idempotent (guards on column presence, backfills only NULLs)."""
    # 1. nodes.node_id — sequential ids for existing rows, then a UNIQUE index.
    _ensure_column(conn, 'nodes', 'node_id', 'INTEGER')
    nxt = conn.execute("SELECT COALESCE(MAX(node_id), 0) AS m FROM nodes").fetchone()['m'] or 0
    for r in conn.execute("SELECT ip FROM nodes WHERE node_id IS NULL ORDER BY created_at, ip").fetchall():
        nxt += 1
        conn.execute(q("UPDATE nodes SET node_id = ? WHERE ip = ?"), (nxt, r['ip']))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_node_id ON nodes(node_id)")
    # 2. node_id on each per-device table, backfilled from the current ip↔node mapping.
    #    Rows whose device_ip no longer maps to any node (old hard-deletes) stay NULL.
    for table in _PERDEVICE_TABLES:
        _ensure_column(conn, table, 'node_id', 'INTEGER')
        conn.execute(
            f"""UPDATE {table} SET node_id =
                    (SELECT n.node_id FROM nodes n WHERE n.ip = {table}.device_ip)
                WHERE node_id IS NULL""")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_node_id ON {table}(node_id)")


def _migrate_add_pause_kind(conn):
    """Add pause_periods.kind ('monitoring' | 'notifications'). Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE pause_periods ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'monitoring'")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(pause_periods)").fetchall()]
        if 'kind' not in cols:
            conn.execute("ALTER TABLE pause_periods ADD COLUMN kind TEXT NOT NULL DEFAULT 'monitoring'")


def _migrate_add_scheduled_pause_mode(conn):
    """Add scheduled_pauses.mode ('monitoring' | 'notifications'). Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE scheduled_pauses ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'monitoring'")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(scheduled_pauses)").fetchall()]
        if 'mode' not in cols:
            conn.execute("ALTER TABLE scheduled_pauses ADD COLUMN mode TEXT NOT NULL DEFAULT 'monitoring'")


def _migrate_add_reported_by(conn):
    """Add reported_by (name of the agent whose report recorded a transition) to
    ping_history and device_states. Nullable — old rows and locally-recorded ones stay
    NULL. Idempotent. Purely informational; nothing keys off it."""
    for table in ('ping_history', 'device_states'):
        if _PG:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS reported_by TEXT")
        else:
            cols = [r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if 'reported_by' not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN reported_by TEXT")


def _migrate_add_event_fields(conn):
    """Generalize maintenance windows into "events": add category/event_group_id to
    maintenance_windows and category/description/event_group_id to scheduled_pauses.
    Existing maintenance_windows rows default to category='maintenance'. Additive and
    idempotent (no rename/PK surgery — see the device_states lesson)."""
    adds = {
        'maintenance_windows': [
            ("category", "TEXT NOT NULL DEFAULT 'maintenance'"),
            ("event_group_id", "TEXT"),
        ],
        'scheduled_pauses': [
            ("category", "TEXT"),
            ("description", "TEXT"),
            ("event_group_id", "TEXT"),
        ],
    }
    for table, cols in adds.items():
        if _PG:
            for name, decl in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {decl}")
        else:
            existing = {r['name'] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_group ON maintenance_windows(event_group_id)")


def _migrate_add_user_activity_site(conn):
    """Add user_activity.site so the audit trail can be scoped per location.
    Backfilling legacy rows to the correct location is done separately (by device
    IP) in _migrate_backfill_user_activity_site. Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE user_activity ADD COLUMN IF NOT EXISTS site TEXT")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(user_activity)").fetchall()]
        if 'site' not in cols:
            conn.execute("ALTER TABLE user_activity ADD COLUMN site TEXT")


def _migrate_backfill_user_activity_site(conn):
    """One-time: attribute audit rows to the right location. Device-scoped rows
    (path /api/devices/<ip>/…) are set to that device's current site — this both
    fills pre-multi-site rows and *corrects* the earlier blanket backfill that
    wrongly dumped every legacy row onto the default location. Rows that can't be
    tied to a device (add-device, location, and scheduled-pause actions, whose
    path carries no IP) fall back to the default site. Flag-guarded → runs once."""
    done = conn.execute(
        q("SELECT value FROM app_flags WHERE key = ?"), ('ua_site_backfilled',)
    ).fetchone()
    if done and done['value'] == '1':
        return

    ip_site = {r['ip']: r['site'] for r in
               conn.execute("SELECT ip, site FROM nodes").fetchall()}
    ip_re = re.compile(r'/api/devices/(\d{1,3}(?:\.\d{1,3}){3})(?:/|$)')
    for row in conn.execute("SELECT id, path FROM user_activity").fetchall():
        m = ip_re.search(row['path'] or '')
        if m and m.group(1) in ip_site:
            conn.execute(q("UPDATE user_activity SET site = ? WHERE id = ?"),
                         (ip_site[m.group(1)], row['id']))
    # Anything still unattributed → the original default location.
    conn.execute("UPDATE user_activity SET site = 'location-a' WHERE site IS NULL")

    if _PG:
        conn.execute("""INSERT INTO app_flags (key, value) VALUES ('ua_site_backfilled', '1')
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    else:
        conn.execute("INSERT OR REPLACE INTO app_flags (key, value) VALUES ('ua_site_backfilled', '1')")


def _migrate_drop_heartbeats(conn):
    """Drop the short-lived dense `heartbeats` table — superseded by the
    app_downtime interval table + the last_report watermark (one row per real
    outage instead of one per report cycle). Idempotent."""
    conn.execute("DROP TABLE IF EXISTS heartbeats")


def _migrate_add_debounce(conn):
    """Add device_states.down_strikes / last_sample for the multi-agent-safe
    confirm-down + history-dedupe logic. Idempotent."""
    ts = 'TIMESTAMP' if _PG else 'DATETIME'
    adds = [('down_strikes', 'INTEGER DEFAULT 0'), ('last_sample', ts)]
    if _PG:
        for col, ddl in adds:
            conn.execute(f"ALTER TABLE device_states ADD COLUMN IF NOT EXISTS {col} {ddl}")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(device_states)").fetchall()]
        for col, ddl in adds:
            if col not in cols:
                conn.execute(f"ALTER TABLE device_states ADD COLUMN {col} {ddl}")


def _migrate_collapse_ping_history(conn):
    """One-time: collapse the legacy dense ping log (a row every ~45s) down to
    transition rows only — keep a row only where a device's status differs from
    the previous one chronologically. The read paths already derive intervals
    from transitions, so the logs look identical afterward; this just reclaims
    the space the old sampling used and matches the new transition-only write
    path. Guarded by an app_flags flag so the (potentially heavy) scan runs once."""
    done = conn.execute(
        q("SELECT value FROM app_flags WHERE key = ?"), ('ping_history_collapsed',)
    ).fetchone()
    if done and done['value'] == '1':
        return
    # Drop every row whose status equals the previous row's (same device), i.e.
    # the redundant "still up" / "still down" samples between real transitions.
    conn.execute("""
        DELETE FROM ping_history WHERE id IN (
            SELECT id FROM (
                SELECT id, status,
                       LAG(status) OVER (PARTITION BY device_ip ORDER BY timestamp, id) AS prev
                FROM ping_history
            ) t WHERE prev IS NOT NULL AND status = prev
        )""")
    if _PG:
        conn.execute("""INSERT INTO app_flags (key, value) VALUES ('ping_history_collapsed', '1')
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""")
    else:
        conn.execute("INSERT OR REPLACE INTO app_flags (key, value) VALUES ('ping_history_collapsed', '1')")


def _migrate_add_site(conn):
    """Add nodes.site (which location a device belongs to). Existing devices
    default to 'location-a' — the original single site. Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS site TEXT NOT NULL DEFAULT 'location-a'")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'site' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN site TEXT NOT NULL DEFAULT 'location-a'")


def _migrate_add_reminded(conn):
    """Add device_states.reminded (0/1: whether the one-time prolonged-outage
    reminder has been sent for the current down episode). Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE device_states ADD COLUMN IF NOT EXISTS reminded INTEGER DEFAULT 0")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(device_states)").fetchall()]
        if 'reminded' not in cols:
            conn.execute("ALTER TABLE device_states ADD COLUMN reminded INTEGER DEFAULT 0")


def _migrate_add_enabled(conn):
    """Add nodes.enabled (1 = monitored, 0 = paused) to older databases that
    predate the pause feature. Idempotent."""
    if _PG:
        conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS enabled INTEGER NOT NULL DEFAULT 1")
    else:
        cols = [r['name'] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        if 'enabled' not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")


def _migrate_allow_other_kind(conn):
    """Older databases created `nodes.kind` with a CHECK constraint limiting it to
    ('ap','switch'), which rejects the new 'other' kind. Remove that constraint
    (kind is validated in the app layer now). Idempotent."""
    if _PG:
        # Inline column CHECKs get the default name <table>_<column>_check.
        conn.execute("ALTER TABLE nodes DROP CONSTRAINT IF EXISTS nodes_kind_check")
    else:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone()
        sql = (row['sql'] if row else '') or ''
        if 'CHECK' in sql.upper():
            # SQLite can't drop a CHECK in place — rebuild the table without it.
            conn.execute("ALTER TABLE nodes RENAME TO _nodes_old")
            conn.execute(
                """CREATE TABLE nodes (
                       ip TEXT PRIMARY KEY,
                       name TEXT NOT NULL UNIQUE,
                       kind TEXT NOT NULL,
                       location TEXT,
                       switch TEXT,
                       uplink TEXT,
                       x REAL,
                       y REAL,
                       enabled INTEGER NOT NULL DEFAULT 1,
                       created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            conn.execute(
                """INSERT INTO nodes (ip, name, kind, location, switch, uplink, x, y, enabled, created_at)
                   SELECT ip, name, kind, location, switch, uplink, x, y, enabled, created_at FROM _nodes_old"""
            )
            conn.execute("DROP TABLE _nodes_old")


# ── Ping recording / state ───────────────────────────────────────────

# Event model: ping_history stores one row per confirmed STATE CHANGE (a
# transition), not one per ping. A device that stays up writes nothing, so
# storage scales with how often devices change state, not with time or device
# count. Site liveness (for "app downtime") is tracked as intervals, not samples:
# a last_report watermark in app_flags + an app_downtime row per real gap.

# A site's monitoring is considered "down" once no agent has reported for longer
# than this — several missed poll cycles (agent default cadence is ~120s). Gaps
# beyond it become app_downtime intervals.
APP_GAP_SECONDS = 600


# ── In-memory runtime state (so the DB isn't touched on every ping) ──
# device_states used to be written on EVERY ping (per device, per report) and
# read on every topology poll, keeping Neon's serverless compute awake 24/7. Now
# the RUNTIME source of truth is in memory, and the DB is written only on a
# confirmed STATE TRANSITION (a durable backing) and read once at startup. A
# steady fleet therefore touches the DB almost never, letting the compute
# auto-suspend. Same for the per-site last-report watermark and the manual
# "check now" flag.
#   ASSUMES ONE web process (gunicorn 1 worker × 4 threads share this memory).
#   Scaling to >1 dyno/worker would need a shared store — each process would keep
#   its own cache and they'd diverge. The RLock guards concurrent thread access.
_rt_lock = threading.RLock()
_states: dict[str, dict] = {}       # ip -> {current_status,last_check,last_change,alert_active,reminded,down_strikes}
_positions: dict[str, dict] = {}        # desktop layout: ip -> {x, y} (rarely changes)
_positions_mobile: dict[str, dict] = {} # mobile layout: ip -> {x, y} (separate Edit Map)
_positions_geo: dict[str, dict] = {}    # aerial basemap: ip -> {lat, lng} (one per device)
_last_report: dict[str, str] = {}   # site -> naive-UTC ISO watermark of the last agent report
_check_requested: set[str] = set()  # sites with a pending FULL manual-check request
# site -> set of device IPs for a TARGETED manual check (single-device "Check now").
# Mutually exclusive with a full request for the same site (full supersedes).
_check_requested_ips: dict[str, set[str]] = {}
# ip -> the status last DURABLY written to ping_history/device_states. Diverges from
# _states[ip]['current_status'] (the live map value) only while DB writes are failing;
# a device with _persisted_status != current_status has a transition still owed to the
# DB, retried on each subsequent record_ping until it lands.
_persisted_status: dict[str, str] = {}
# IP → Device ID migration: ip <-> stable surrogate node_id, loaded at startup and kept
# current on add/edit/delete. Phase 3a used these to dual-write node_id; Phase 3b keys the
# in-memory caches and DB reads by node_id (device_ip stays dual-written as a safety net).
# Both directions are maintained together so we can translate at the public (ip-based)
# accessor boundary without a DB hit. See _nid() / _ip_of().
_ip_to_nid: dict[str, int] = {}
_nid_to_ip: dict[int, str] = {}
# Diagnostics for the "DB can't be written" condition (surfaced by the watchdog):
# count of consecutive transition-write failures + the last error text.
_write_fail_count = 0
_write_last_error: str | None = None


def _nid(ip):
    """Stable surrogate node_id for a device IP (None if unknown). In-memory, no DB —
    the key for the caches + DB reads, and dual-written to device_ip on writes."""
    with _rt_lock:
        return _ip_to_nid.get(ip)


def _ip_of(nid):
    """Current IP for a node_id (None if unknown) — reverse of _nid, so the ip-based
    public accessors can translate a node_id-keyed cache back to IPs."""
    with _rt_lock:
        return _nid_to_ip.get(nid)


def _set_ip_nid(ip, nid):
    """Maintain both directions of the ip <-> node_id map together (call under _rt_lock)."""
    _ip_to_nid[ip] = nid
    _nid_to_ip[nid] = ip


def _forget_ip(ip):
    """Drop an IP from both directions of the map (call under _rt_lock)."""
    nid = _ip_to_nid.pop(ip, None)
    if nid is not None and _nid_to_ip.get(nid) == ip:
        _nid_to_ip.pop(nid, None)


def load_runtime_caches():
    """Populate the device-state cache from the DB once at startup so the map
    isn't blank after a restart. The watermark and check-flag start empty (a fresh
    report re-establishes the baseline; a pending check doesn't survive a restart).
    down_strikes resets to 0 (debounce restarts — a single blip may be re-counted,
    which is harmless)."""
    with _rt_lock:
        _states.clear()
        _positions.clear()
        _positions_mobile.clear()
        _positions_geo.clear()
        _last_report.clear()
        _check_requested.clear()
        _check_requested_ips.clear()
        _persisted_status.clear()
        _ip_to_nid.clear()
        _nid_to_ip.clear()
        try:
            with get_conn() as conn:
                for r in conn.execute("SELECT ip, node_id FROM nodes").fetchall():
                    if r['node_id'] is not None:
                        _set_ip_nid(r['ip'], r['node_id'])
                # The caches are keyed by the stable node_id (Phase 3b). A row's node_id
                # is used when present; otherwise resolve it from its device_ip (covers a
                # row written before the 3a backfill). Rows we still can't key are skipped.
                for r in conn.execute("SELECT * FROM device_states").fetchall():
                    d = _norm(r)
                    nid = d.get('node_id') if d.get('node_id') is not None else _ip_to_nid.get(d['device_ip'])
                    if nid is None:
                        continue
                    status = d.get('current_status') or 'unknown'
                    _states[nid] = {
                        'current_status': status,
                        'last_check': d.get('last_check'),
                        'last_change': d.get('last_change'),
                        'alert_active': d.get('alert_active') or 0,
                        'reminded': d.get('reminded') or 0,
                        'down_strikes': 0,
                        'reported_by': d.get('reported_by'),
                    }
                    # Whatever's in device_states IS what's durably persisted.
                    _persisted_status[nid] = status
                for cache, tbl, cols in (
                        (_positions, 'device_positions', 'x, y'),
                        (_positions_mobile, 'device_positions_mobile', 'x, y'),
                        (_positions_geo, 'device_positions_geo', 'lat, lng')):
                    for r in conn.execute(f"SELECT device_ip, node_id, {cols} FROM {tbl}").fetchall():
                        nid = r['node_id'] if r['node_id'] is not None else _ip_to_nid.get(r['device_ip'])
                        if nid is None:
                            continue
                        cache[nid] = ({'lat': r['lat'], 'lng': r['lng']}
                                      if tbl == 'device_positions_geo' else {'x': r['x'], 'y': r['y']})
        except Exception:
            log.exception("load_runtime_caches failed")


def _persist_transition(device_ip, st, response_ms):
    """Write-through a confirmed transition: append the ping_history marker and upsert
    device_states so the DB stays a correct durable backing. ONE attempt, in a single
    `get_conn()` transaction that commits on clean exit and rolls back on any
    exception (so a failure leaves nothing partial). Raises on failure; the caller
    keeps the transition queued and retries it on the next report — we do NOT retry in
    a loop here, because that would hold the runtime lock (blocking the topology poll)
    and, if the DB were rejecting every write, stall the whole report batch."""
    reported_by = st.get('reported_by')
    nid = _nid(device_ip)                    # dual-write the stable node_id (Phase 3a)
    with get_conn() as conn:
        conn.execute(
            q("INSERT INTO ping_history (device_ip, node_id, status, response_ms, reported_by) "
              "VALUES (?, ?, ?, ?, ?)"),
            (device_ip, nid, st['current_status'], response_ms, reported_by))
        vals = (device_ip, nid, st['current_status'], st['last_check'], st['last_change'],
                st['alert_active'], st['reminded'], st['down_strikes'], reported_by)
        # UPDATE-then-INSERT rather than ON CONFLICT (device_ip) so it works regardless
        # of the device_states table's unique-constraint shape. The production table
        # predates the `device_ip PRIMARY KEY` in the CREATE (CREATE TABLE IF NOT EXISTS
        # never adds it to an existing table), so `ON CONFLICT (device_ip)` raised
        # InvalidColumnReference and every transition write failed. Single web process +
        # the RLock in record_ping serialize writes, so UPDATE-then-INSERT can't race.
        # Same robust pattern set_position uses.
        cur = conn.execute(
            q("""UPDATE device_states SET node_id = ?, current_status = ?, last_check = ?, last_change = ?,
                     alert_active = ?, reminded = ?, down_strikes = ?, reported_by = ? WHERE device_ip = ?"""),
            (nid, st['current_status'], st['last_check'], st['last_change'],
             st['alert_active'], st['reminded'], st['down_strikes'], reported_by, device_ip))
        if not cur.rowcount:
            conn.execute(
                q("""INSERT INTO device_states (device_ip, node_id, current_status, last_check, last_change,
                        alert_active, reminded, down_strikes, reported_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                vals)


def db_write_health() -> tuple[int, str | None]:
    """(consecutive transition-write failures, last error text). 0 == healthy.
    Read by the watchdog to alert when the DB has become unwritable."""
    with _rt_lock:
        return _write_fail_count, _write_last_error


def record_ping(device_ip: str, status: str, response_ms: float | None,
                confirm_checks: int = 2, reported_by: str | None = None):
    """Record a ping observation and update the device's CONFIRMED status in the
    in-memory cache, which is the live source of truth for the map and Slack. The DB
    (ping_history marker + device_states) is written on a confirmed transition.

    The in-memory status is updated UNCONDITIONALLY — even if the DB write fails — so
    the map and alerts never freeze when the database can't be written (e.g. it's in
    read-only mode, or a Neon cold-start blips). A transition whose write fails stays
    QUEUED (`_persisted_status` still shows the old value) and is retried on the next
    report until it lands, so ping_history/device_states catch up without ever writing
    a duplicate marker. Persistent write failure is counted + logged loudly so the
    watchdog can alert — it is never silent.

    Multi-agent safe: a device flips to 'down' (and alerts) only after
    `confirm_checks` consecutive down observations; recovery to 'up' is immediate.
    Returns (changed, prev_status); changed reflects a confirmed transition."""
    global _write_fail_count, _write_last_error
    now = datetime.utcnow().isoformat()
    confirm_checks = max(1, int(confirm_checks))

    with _rt_lock:
        # Cache key is the stable node_id (Phase 3b); fall back to the IP for a stray
        # report from an IP not in the node list (never shown, but not dropped).
        nid = _ip_to_nid.get(device_ip)
        key = nid if nid is not None else device_ip
        cur = _states.get(key)
        prev = cur['current_status'] if cur else 'unknown'
        strikes = cur['down_strikes'] if cur else 0

        # Confirm-down / immediate-up debounce.
        if status == 'up':
            confirmed, strikes = 'up', 0
        elif prev == 'down':
            confirmed, strikes = 'down', 0        # already down; stay down
        else:                                     # raw down while up/unknown
            strikes += 1
            if strikes >= confirm_checks:
                confirmed, strikes = 'down', 0    # confirmed outage
            else:
                confirmed = prev                  # pending — not yet an outage

        changed = confirmed != prev
        st = {
            'current_status': confirmed,
            'last_check': now,
            'last_change': now if changed else (cur['last_change'] if cur else now),
            'alert_active': 1 if confirmed == 'down' else 0,
            'reminded': 0 if changed else (cur['reminded'] if cur else 0),
            'down_strikes': strikes,
            'reported_by': reported_by,   # which agent's report this observation came from
        }
        # ALWAYS update the live in-memory status first — the map/Slack must reflect
        # reality regardless of whether the DB write succeeds.
        _states[key] = st

        # Persist whenever the confirmed status differs from what's durably recorded.
        # In the healthy case that's exactly "on a transition". If a prior write
        # failed, _persisted_status is stale, so this also RETRIES that owed write on
        # the next report — and never writes a non-transition duplicate row.
        if confirmed != _persisted_status.get(key, 'unknown'):
            try:
                _persist_transition(device_ip, st, response_ms)
                _persisted_status[key] = confirmed
                if _write_fail_count:
                    log.warning("DB writes recovered after %d failed transition(s)", _write_fail_count)
                    _write_fail_count = 0
                    _write_last_error = None
            except Exception as e:      # noqa: BLE001
                _write_fail_count += 1
                _write_last_error = f"{type(e).__name__}: {e}"
                # Loud — this is a monitoring-affecting failure, never swallowed.
                log.error("record_ping: DB transition write FAILED for %s (%s→%s): %s "
                          "— map/alerts stay live, will retry next report (%d consecutive)",
                          device_ip, prev, confirmed, _write_last_error, _write_fail_count)

    return changed, prev


def _upsert_flag(conn, key, value):
    """Set an app_flags key using an existing connection (so it joins the caller's
    transaction). Mirrors set_flag, which opens its own connection."""
    if _PG:
        conn.execute(
            """INSERT INTO app_flags (key, value) VALUES (%s, %s)
               ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, value))
    else:
        conn.execute("INSERT OR REPLACE INTO app_flags (key, value) VALUES (?, ?)", (key, value))


def record_report(site: str):
    """Mark that an agent reported for `site` at 'now'. Instead of storing a row
    per cycle, we keep a single 'last report' watermark and, when the gap since
    the previous report exceeds APP_GAP_SECONDS (the monitoring pipeline went
    quiet), append ONE app_downtime interval [last_report, now]. So storage grows
    with real outages, not time, and it's naturally multi-agent-safe (any agent's
    report refreshes the watermark; a gap means nobody reported)."""
    now_dt = datetime.utcnow()
    with _rt_lock:
        last = _last_report.get(site)
        _last_report[site] = now_dt.isoformat()      # in-memory; no per-report DB write
    last_dt = _to_dt(last) if last else None
    # Only touch the DB when the pipeline actually went quiet — write ONE
    # app_downtime interval for the gap. Steady-state reports write nothing.
    if last_dt is not None and (now_dt - last_dt).total_seconds() > APP_GAP_SECONDS:
        # Best-effort: a failed write here (e.g. read-only DB) must not abort the
        # report — the Slack alerting downstream has to keep working during a DB
        # outage. The gap is still visible via the in-memory watermark.
        try:
            with get_conn() as conn:
                conn.execute(
                    q("INSERT INTO app_downtime (site, start_ts, end_ts) VALUES (?, ?, ?)"),
                    (site, last_dt.isoformat(), now_dt.isoformat()))
        except Exception as e:      # noqa: BLE001
            log.warning("record_report: app_downtime write failed for %s: %s", site, e)


def get_last_report(site: str) -> str | None:
    """Naive-UTC ISO of the last agent report for `site` (in-memory watermark), or
    None if none since startup. Used by the stale-monitoring banner + watchdog."""
    with _rt_lock:
        return _last_report.get(site)


# ── Manual "check now" flag (in-memory, so the agent's ~20s target poll that
# consumes it doesn't hit the DB every time) ─────────────────────────
def request_check(site: str, ips=None):
    """Request a manual check for `site`. `ips=None` → a FULL check (ping every device,
    the header "Check Now" button). `ips=[...]` → a TARGETED check of just those devices
    (the single-device panel's "Check now"). A full request supersedes any pending
    targeted set for the site; a targeted request is absorbed when a full one is already
    pending (the full sweep covers it)."""
    with _rt_lock:
        if ips:
            if site not in _check_requested:
                _check_requested_ips.setdefault(site, set()).update(ips)
        else:
            _check_requested.add(site)
            _check_requested_ips.pop(site, None)


def consume_check(site: str) -> tuple[bool, list[str]]:
    """Consume any pending manual check for `site`, clearing it. Returns
    `(full, ips)`: `full=True` means ping the whole site; otherwise `ips` is the list
    of specific device IPs to check (empty when nothing is pending). The two are
    mutually exclusive — a full check returns `ips=[]`."""
    with _rt_lock:
        full = site in _check_requested
        _check_requested.discard(site)
        ips = sorted(_check_requested_ips.pop(site, set()))
        return (full, [] if full else ips)


# Cached scheduled-pause state so the watchdog can skip its per-tick DB query. Two
# facts are cached: whether any 'scheduled'/'active' pause exists, and the EARLIEST
# boundary the sweep must next act on (a 'scheduled' pause's start_at, or an 'active'
# pause's end_at). Between boundaries the watchdog does zero DB queries — so even a
# multi-day active pause lets Neon's compute suspend. Refreshed at startup, at the end
# of each sweep, and whenever a pause is created/cancelled.
_open_pauses = False
_next_pause_boundary: str | None = None    # naive-UTC ISO of the next activate/resume


def refresh_open_pauses() -> bool:
    """Recompute + cache (1 query) the open-pauses flag and the next sweep boundary."""
    global _open_pauses, _next_pause_boundary
    with get_conn() as conn:
        rows = conn.execute(
            q("SELECT start_at, end_at, status FROM scheduled_pauses "
              "WHERE status IN ('scheduled', 'active')")).fetchall()
    boundary = None
    for r in rows:
        d = _norm(r)
        # A 'scheduled' pause is acted on at its start; an 'active' one at its end.
        t = d.get('start_at') if d.get('status') == 'scheduled' else d.get('end_at')
        if t and (boundary is None or t < boundary):
            boundary = t
    with _rt_lock:
        _open_pauses = len(rows) > 0
        _next_pause_boundary = boundary
    return _open_pauses


def has_open_pauses() -> bool:
    with _rt_lock:
        return _open_pauses


def pause_sweep_due() -> bool:
    """True only when the watchdog should actually run its scheduled-pause DB sweep:
    an open pause exists AND its next boundary (activate/resume time) has arrived.
    Between boundaries this returns False from memory alone — no DB query — so an
    active pause doesn't keep Neon awake for its whole duration."""
    with _rt_lock:
        b = _next_pause_boundary
        if not _open_pauses or b is None:
            return False
    try:
        return datetime.utcnow() >= datetime.fromisoformat(b)
    except (ValueError, TypeError):
        return True     # unparseable boundary — don't get stuck, let the sweep run


def seconds_until_next_pause_boundary() -> float | None:
    """Seconds from now until the next scheduled-pause boundary (activate/resume), or
    None if no pause is open. Negative if the boundary has already passed (a sweep is
    due). Read from the in-memory boundary cache — no DB query. Lets the watchdog pick
    a slow tick when nothing is near and a fast one when a boundary approaches."""
    with _rt_lock:
        b = _next_pause_boundary
        if not _open_pauses or b is None:
            return None
    try:
        return (datetime.fromisoformat(b) - datetime.utcnow()).total_seconds()
    except (ValueError, TypeError):
        return 0.0      # unparseable — treat as due now


# Cached "is any location soft-delete pending?" flag so the purge sweep (called
# from topology + agent report) doesn't query the DB every few minutes when there's
# nothing to purge — which would keep Neon's compute from ever suspending.
_pending_deletions = False


def refresh_pending_deletions() -> bool:
    global _pending_deletions
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT COUNT(*) AS n FROM sites WHERE pending_delete_at IS NOT NULL")
        ).fetchone()
    with _rt_lock:
        _pending_deletions = (row['n'] or 0) > 0
    return _pending_deletions


def has_pending_deletions() -> bool:
    with _rt_lock:
        return _pending_deletions


def get_overdue_down(threshold_hours: float) -> list[str]:
    """IPs currently down at least `threshold_hours` that haven't had the one-time
    prolonged-outage reminder sent yet. Served from the in-memory (node_id-keyed) cache,
    translated back to IPs for the caller."""
    cutoff = (datetime.utcnow() - timedelta(hours=threshold_hours)).isoformat()
    with _rt_lock:
        return [_nid_to_ip.get(k, k) for k, s in _states.items()
                if s['current_status'] == 'down' and not s['reminded']
                and (s['last_change'] or '') <= cutoff]


def mark_reminded(device_ips: list[str]):
    """Mark the prolonged-outage reminder as sent — in memory, and write-through to
    the DB so a restart doesn't re-remind for a still-open outage."""
    if not device_ips:
        return
    with _rt_lock:
        for ip in device_ips:
            k = _ip_to_nid.get(ip, ip)      # node_id key (or the IP itself for a stray)
            if k in _states:
                _states[k]['reminded'] = 1
    # Best-effort DB write — the in-memory flag already prevents re-reminding this
    # process; a failed write (read-only DB) must not abort the report.
    try:
        with get_conn() as conn:
            conn.execute(
                q(f"UPDATE device_states SET reminded = 1 WHERE device_ip IN ({','.join('?' * len(device_ips))})"),
                list(device_ips)
            )
    except Exception as e:      # noqa: BLE001
        log.warning("mark_reminded: DB write failed (flag kept in memory): %s", e)


def get_all_states(device_ips: list[str]) -> dict:
    """Current status/last_check/etc. for the given IPs, from the in-memory cache
    (no DB read — this is the 30s topology hot path). The cache is node_id-keyed;
    translate each requested IP through the map."""
    with _rt_lock:
        out = {}
        for ip in device_ips:
            k = _ip_to_nid.get(ip, ip)
            if k in _states:
                out[ip] = dict(_states[k])
        return out


def get_uptime_percent(device_ip: str, hours: int = 24) -> float:
    """Time-weighted uptime over the last `hours`: the fraction of *monitored*
    time the device was confirmed 'up', derived from its status transitions.
    Paused time is excluded from both the up-time and the denominator, so pausing
    a device doesn't drag its uptime down. None if never observed / nothing
    monitored in the window."""
    now_dt = datetime.utcnow()
    with get_conn() as conn:
        trans = _device_transitions(conn, device_ip, None)   # [(dt, status)] asc
        if not trans:
            return None
        # Don't count time before we ever observed the device as downtime — floor
        # the window at its first observation (so a freshly-added device that's up
        # reads ~100%, not near-0% until 24h of history accrues).
        since_dt = max(now_dt - timedelta(hours=hours), trans[0][0])
        pauses = _pause_periods(conn, device_ip, since_dt.isoformat())

    pause_ivals = _pause_intervals(pauses, now_dt)
    up_secs = 0.0
    n = len(trans)
    for i, (t, st) in enumerate(trans):
        seg_start = max(t, since_dt)
        seg_end = min(trans[i + 1][0] if i + 1 < n else now_dt, now_dt)
        if seg_end <= seg_start:
            continue
        if st == 'up':
            up_secs += (seg_end - seg_start).total_seconds() \
                - _overlap_seconds(seg_start, seg_end, pause_ivals)

    total = (now_dt - since_dt).total_seconds() - _overlap_seconds(since_dt, now_dt, pause_ivals)
    if total <= 0:
        return None
    return round(min(up_secs / total * 100, 100.0), 1)


def get_recent_history(device_ip: str, limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            q("""SELECT timestamp, status, response_ms FROM ping_history
                 WHERE device_ip = ? ORDER BY timestamp DESC LIMIT ?"""),
            (device_ip, limit)
        ).fetchall()
    return [_norm(r) for r in rows]


# ── Map positions ────────────────────────────────────────────────────

def _pos_cache(layout: str) -> dict:
    """The in-memory position cache for a layout ('mobile' → mobile, else desktop)."""
    return _positions_mobile if layout == 'mobile' else _positions


def get_position(device_ip: str, layout: str = 'desktop') -> dict | None:
    """User-dragged position (or None) for the given layout — from the (node_id-keyed)
    in-memory cache."""
    with _rt_lock:
        p = _pos_cache(layout).get(_ip_to_nid.get(device_ip, device_ip))
        return dict(p) if p else None


def get_positions(device_ips: list[str], layout: str = 'desktop') -> dict:
    """{ip: {x, y}} for the given IPs + layout — from the in-memory (node_id-keyed) cache
    (no DB read on the topology hot path). Positions change only on an Edit-Map save."""
    with _rt_lock:
        cache = _pos_cache(layout)
        out = {}
        for ip in device_ips:
            k = _ip_to_nid.get(ip, ip)
            if k in cache:
                out[ip] = dict(cache[k])
        return out


def set_position(device_ip: str, x: float, y: float, layout: str = 'desktop'):
    # Write-through: update the cache AND the durable DB row (Edit-Map saves only).
    # The mobile layout has its own table so a device can hold both positions.
    table = 'device_positions_mobile' if layout == 'mobile' else 'device_positions'
    with _rt_lock:
        _pos_cache(layout)[_ip_to_nid.get(device_ip, device_ip)] = {'x': x, 'y': y}
    # UPDATE-then-INSERT rather than ON CONFLICT so it works regardless of the
    # table's unique-constraint shape (robust to schema drift across migrations).
    nid = _nid(device_ip)
    with get_conn() as conn:
        cur = conn.execute(
            q(f"UPDATE {table} SET x = ?, y = ?, node_id = ? WHERE device_ip = ?"),
            (x, y, nid, device_ip)
        )
        if cur.rowcount == 0:
            conn.execute(
                q(f"INSERT INTO {table} (device_ip, node_id, x, y) VALUES (?, ?, ?, ?)"),
                (device_ip, nid, x, y)
            )


# ── Geographic positions (aerial basemap) ────────────────────────────
# Same in-memory write-through shape as the x/y positions above, so the topology
# hot path stays DB-free. Stored as lat/lng: the browser projects them onto the
# basemap's pixel grid, which keeps them valid if the imagery is replaced.

def get_geo_positions(device_ips: list[str]) -> dict:
    """{ip: {lat, lng}} for the given IPs — from the in-memory (node_id-keyed) cache
    (no DB read; this is on the 30s topology path)."""
    with _rt_lock:
        out = {}
        for ip in device_ips:
            k = _ip_to_nid.get(ip, ip)
            if k in _positions_geo:
                out[ip] = dict(_positions_geo[k])
        return out


def has_geo_position(device_ip: str) -> bool:
    with _rt_lock:
        return _ip_to_nid.get(device_ip, device_ip) in _positions_geo


def set_geo_position(device_ip: str, lat: float, lng: float):
    """Write-through: update the cache AND the durable row (Edit-Map saves and the
    one-time seed only). UPDATE-then-INSERT for the same schema-drift robustness as
    set_position."""
    with _rt_lock:
        _positions_geo[_ip_to_nid.get(device_ip, device_ip)] = {'lat': lat, 'lng': lng}
    nid = _nid(device_ip)
    with get_conn() as conn:
        cur = conn.execute(
            q("UPDATE device_positions_geo SET lat = ?, lng = ?, node_id = ? WHERE device_ip = ?"),
            (lat, lng, nid, device_ip)
        )
        if cur.rowcount == 0:
            conn.execute(
                q("INSERT INTO device_positions_geo (device_ip, node_id, lat, lng) VALUES (?, ?, ?, ?)"),
                (device_ip, nid, lat, lng)
            )


def get_alert_active(device_ip: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT alert_active FROM device_states WHERE device_ip = ?"), (device_ip,)
        ).fetchone()
    return bool(row['alert_active']) if row else False


# ── Per-device notes ─────────────────────────────────────────────────

def get_note(device_ip: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT note FROM device_notes WHERE device_ip = ?"), (device_ip,)
        ).fetchone()
    return row['note'] if row else ''


def set_note(device_ip: str, note: str):
    now = datetime.utcnow().isoformat()
    nid = _nid(device_ip)
    with get_conn() as conn:
        cur = conn.execute(
            q("UPDATE device_notes SET note = ?, updated_at = ?, node_id = ? WHERE device_ip = ?"),
            (note, now, nid, device_ip)
        )
        if cur.rowcount == 0:
            conn.execute(
                q("INSERT INTO device_notes (device_ip, node_id, note, updated_at) VALUES (?, ?, ?, ?)"),
                (device_ip, nid, note, now)
            )


# ── Node registry (devices + switches; was devices.yaml/extra) ───────

def _node_entry(row: dict) -> dict:
    """Shape a nodes row like a devices.yaml entry (drop internal columns,
    omit empties) so the merge/response code consumes it unchanged."""
    out = {'name': row['name'], 'ip': row['ip'], 'kind': row['kind'],
           'site': row.get('site') or 'location-a',
           'enabled': bool(row.get('enabled', 1)),
           'notify': bool(row.get('notify', 1))}
    if row.get('location'):
        out['location'] = row['location']
    if row.get('hostname'):
        out['hostname'] = row['hostname']
    if row.get('intermapper_url'):
        out['intermapper_url'] = row['intermapper_url']
    if row.get('switch'):
        out['switch'] = row['switch']
    if row.get('uplink'):
        out['uplink'] = row['uplink']
    if row.get('x') is not None and row.get('y') is not None:
        out['x'] = row['x']
        out['y'] = row['y']
    return out


def get_nodes() -> dict:
    """Return {'devices': [...], 'switches': [...]} from the nodes table,
    matching the structure the app config merge expects. Decommissioned devices
    (soft-deleted — see decommission_node) are excluded so they drop off the map,
    the device list, and the agent's ping targets while their logs are retained."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM nodes WHERE decommissioned_at IS NULL").fetchall()
    devices, switches = [], []
    for r in rows:
        r = dict(r)
        (switches if r['kind'] == 'switch' else devices).append(_node_entry(r))
    return {'devices': devices, 'switches': switches}


def get_decommissioned_nodes(site: str | None = None) -> list[dict]:
    """Soft-deleted nodes (whole rows), optionally scoped to a site. Used by the log
    endpoints so a removed device's history still renders with its name/kind."""
    with get_conn() as conn:
        if site is None:
            rows = conn.execute("SELECT * FROM nodes WHERE decommissioned_at IS NOT NULL").fetchall()
        else:
            rows = conn.execute(
                q("SELECT * FROM nodes WHERE decommissioned_at IS NOT NULL AND site = ?"),
                (site,)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        e = _node_entry(r)
        e['decommissioned_at'] = _norm(r).get('decommissioned_at')
        out.append(e)
    return out


def nodes_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()
    return row['n']


def add_node(ip: str, name: str, kind: str, site: str = 'location-a',
             location: str | None = None, switch: str | None = None,
             uplink: str | None = None, x: float | None = None, y: float | None = None,
             hostname: str | None = None, intermapper_url: str | None = None):
    """Insert a device ('ap') or switch ('switch') at a given site. PK on ip +
    UNIQUE on name give a DB-level backstop to the app's own duplicate checks.
    `hostname` is the optional DNS name the agent pings (falls back to IP);
    `intermapper_url` links the device to its page on U-M's Intermapper."""
    old_ip = None
    with get_conn() as conn:
        # Adding a device whose IP or name is held by a DECOMMISSIONED node revives that
        # row (clears decommissioned_at, overwrites its fields) instead of hitting the PK/
        # UNIQUE constraint — so re-adding a replaced device at the same IP resumes its
        # existing history. Active duplicates are still rejected by the caller's own check.
        # A revived row keeps its existing node_id; a brand-new row gets the next one.
        row = conn.execute(
            q("""SELECT ip, node_id FROM nodes
                 WHERE (ip = ? OR name = ?) AND decommissioned_at IS NOT NULL"""),
            (ip, name)).fetchone()
        if row:
            nid = row['node_id']
            old_ip = row['ip']
            conn.execute(
                q("""UPDATE nodes SET ip = ?, name = ?, kind = ?, site = ?, location = ?,
                         hostname = ?, intermapper_url = ?, switch = ?, uplink = ?, x = ?, y = ?,
                         decommissioned_at = NULL
                     WHERE ip = ?"""),
                (ip, name, kind, site, location, hostname or None, intermapper_url or None,
                 switch, uplink, x, y, old_ip))
        else:
            nid = (conn.execute("SELECT COALESCE(MAX(node_id), 0) AS m FROM nodes").fetchone()['m'] or 0) + 1
            conn.execute(
                q("""INSERT INTO nodes (ip, node_id, name, kind, site, location, hostname,
                         intermapper_url, switch, uplink, x, y)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
                (ip, nid, name, kind, site, location, hostname or None, intermapper_url or None,
                 switch, uplink, x, y)
            )
    with _rt_lock:                          # keep the ip <-> node_id map current
        if old_ip and old_ip != ip:
            _forget_ip(old_ip)
        if nid is not None:
            _set_ip_nid(ip, nid)


def update_node(old_ip: str, ip: str, name: str, kind: str, location: str | None = None,
                switch: str | None = None, uplink: str | None = None,
                old_name: str | None = None, old_kind: str | None = None,
                hostname: str | None = None, intermapper_url: str | None = None):
    """Edit an existing device/switch (kind may change). If the IP changes, its
    per-IP history / state / position / note are migrated to the new IP. If a
    switch stays a switch but is renamed, children that reference it by name
    (APs'/others' `switch`, child switches' `uplink`) are repointed. Table names
    are hard-coded."""
    with get_conn() as conn:
        if kind == 'switch' and old_kind == 'switch' and old_name and old_name != name:
            conn.execute(q("UPDATE nodes SET switch = ? WHERE switch = ?"), (name, old_name))
            conn.execute(q("UPDATE nodes SET uplink = ? WHERE uplink = ?"), (name, old_name))
        conn.execute(
            q("""UPDATE nodes SET ip = ?, name = ?, kind = ?, location = ?, hostname = ?,
                     intermapper_url = ?, switch = ?, uplink = ?
                 WHERE ip = ?"""),
            (ip, name, kind, location, hostname or None, intermapper_url or None,
             switch, uplink, old_ip)
        )
        if ip != old_ip:
            # Keep device_ip (the safety-net key) aligned across every per-device table
            # on an IP change. node_id on these rows is already stable, so a node_id-keyed
            # read finds them regardless; migrating device_ip keeps the net correct too.
            for table in _PERDEVICE_TABLES:
                conn.execute(q(f"UPDATE {table} SET device_ip = ? WHERE device_ip = ?"),
                             (ip, old_ip))
    if ip != old_ip:
        # The caches are keyed by the stable node_id, which doesn't change with the IP,
        # so nothing needs re-keying — just point the ip <-> node_id map at the new IP.
        # (The device_ip columns are still migrated above as the safety-net key.)
        with _rt_lock:
            nid = _ip_to_nid.get(old_ip)
            _forget_ip(old_ip)
            if nid is not None:
                _set_ip_nid(ip, nid)


def set_device_pause(ip: str, mode: str):
    """Set a device's pause state. `mode`:
      'active'        — resume: agent pings + Slack on (enabled=1, notify=1).
      'notifications' — still pinged (data saved), but no Slack (enabled=1, notify=0).
      'monitoring'    — agent stops pinging, no data recorded (enabled=0).
    Records the interval in pause_periods (with `kind`) so it shows in the log; a
    device is in at most one paused kind at a time (switching closes the other)."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        if mode == 'active':
            conn.execute(q("UPDATE nodes SET enabled = 1, notify = 1 WHERE ip = ?"), (ip,))
            conn.execute(
                q("UPDATE pause_periods SET resumed_at = ? WHERE device_ip = ? AND resumed_at IS NULL"),
                (now, ip))
            return
        kind = 'notifications' if mode == 'notifications' else 'monitoring'
        enabled = 1 if mode == 'notifications' else 0
        notify = 0 if mode == 'notifications' else 1
        conn.execute(q("UPDATE nodes SET enabled = ?, notify = ? WHERE ip = ?"), (enabled, notify, ip))
        # Close any open period of the OTHER kind (mode switch), then open this
        # kind if one isn't already open (idempotent).
        conn.execute(
            q("UPDATE pause_periods SET resumed_at = ? "
              "WHERE device_ip = ? AND resumed_at IS NULL AND kind <> ?"),
            (now, ip, kind))
        already = conn.execute(
            q("SELECT 1 FROM pause_periods WHERE device_ip = ? AND resumed_at IS NULL AND kind = ?"),
            (ip, kind)).fetchone()
        if not already:
            conn.execute(
                q("INSERT INTO pause_periods (device_ip, node_id, paused_at, kind) VALUES (?, ?, ?, ?)"),
                (ip, _nid(ip), now, kind))


def set_node_enabled(ip: str, enabled: bool):
    """Back-compat shim: monitoring pause/resume (no notifications mode)."""
    set_device_pause(ip, 'active' if enabled else 'monitoring')


def delete_pause_period(pause_id: int) -> bool:
    """Delete one monitoring/notification-pause LOG row (pause_periods) by id. Used by
    the admin-password-gated log editor. Returns True if a row was removed. Only removes
    the historical log entry — it does not change a device's current enabled/notify
    state (that lives on the `nodes` row)."""
    with get_conn() as conn:
        cur = conn.execute(q("DELETE FROM pause_periods WHERE id = ?"), (pause_id,))
        return (cur.rowcount or 0) > 0


def set_node_hostname(ip: str, hostname: str | None) -> bool:
    """Set (or clear, with None/'') just a node's DNS hostname by IP. Used by the bulk
    DNS import. Returns True if a row was updated."""
    with get_conn() as conn:
        cur = conn.execute(q("UPDATE nodes SET hostname = ? WHERE ip = ?"),
                           (hostname or None, ip))
        # rowcount is reliable on both psycopg and sqlite3 for UPDATE.
        return (cur.rowcount or 0) > 0


def decommission_node(ip: str) -> bool:
    """Soft-delete a device/switch: mark it decommissioned so it drops off the map,
    the device list and the agent's ping targets, but KEEP its row and history so its
    logs remain in Device Logs. Evicts the live status (it's no longer monitored) and
    saved positions (so a later revive at the same IP starts unplaced); ping_history,
    pause_periods and notes are intentionally left intact. Returns False if unknown."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            q("UPDATE nodes SET decommissioned_at = ? WHERE ip = ? AND decommissioned_at IS NULL"),
            (now, ip))
        changed = (cur.rowcount or 0) > 0
        if changed:
            for table in ('device_positions', 'device_positions_mobile', 'device_positions_geo'):
                conn.execute(q(f"DELETE FROM {table} WHERE device_ip = ?"), (ip,))
    with _rt_lock:
        k = _ip_to_nid.get(ip, ip)          # caches are node_id-keyed
        _states.pop(k, None)
        _positions.pop(k, None)
        _positions_mobile.pop(k, None)
        _positions_geo.pop(k, None)
        # Keep the ip <-> node_id map entry: the row persists, so restore / deleted-device
        # views still resolve, and the freed positions are re-created if it's revived.
    return changed


def restore_node(ip: str) -> bool:
    """Bring a decommissioned device back to the fleet (clears decommissioned_at) so it
    returns to the map / list / ping targets; its retained history resumes. Positions
    were dropped at decommission, so it comes back unplaced. Returns False if `ip` isn't
    a currently-decommissioned node."""
    with get_conn() as conn:
        cur = conn.execute(
            q("UPDATE nodes SET decommissioned_at = NULL WHERE ip = ? AND decommissioned_at IS NOT NULL"),
            (ip,))
        return (cur.rowcount or 0) > 0


def seed_nodes_from_yaml(devices: list[dict], switches: list[dict]):
    """One-time population of the nodes table from devices.yaml. Caller guards
    on an empty table so this is idempotent. Assigns each seeded node a stable
    surrogate node_id (this runs after the migrations on a fresh DB, so the column
    exists but the ip↔node backfill hasn't seen these rows)."""
    with get_conn() as conn:
        nxt = conn.execute("SELECT COALESCE(MAX(node_id), 0) AS m FROM nodes").fetchone()['m'] or 0
        for sw in switches:
            nxt += 1
            conn.execute(
                q("""INSERT INTO nodes (ip, node_id, name, kind, location, switch, uplink, x, y)
                     VALUES (?, ?, ?, 'switch', ?, NULL, ?, ?, ?)"""),
                (sw['ip'], nxt, sw['name'], sw.get('location'), sw.get('uplink'),
                 sw.get('x'), sw.get('y'))
            )
        for d in devices:
            nxt += 1
            conn.execute(
                q("""INSERT INTO nodes (ip, node_id, name, kind, location, switch, uplink, x, y)
                     VALUES (?, ?, ?, 'ap', ?, ?, NULL, ?, ?)"""),
                (d['ip'], nxt, d['name'], d.get('location'), d.get('switch'),
                 d.get('x'), d.get('y'))
            )


# ── Activity log: per-device down/unknown periods + app-wide downtime ─

def _to_dt(v):
    """Parse a stored timestamp (PG datetime, or SQLite ISO / 'YYYY-MM-DD HH:MM:SS'
    string) into a naive datetime for interval math. fromisoformat (3.11+) accepts
    both the 'T' and space separators."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    return datetime.fromisoformat(v)


_LOG_STATUSES = ('down', 'unknown')

# A newly added device is expected to read 'unknown' and may flap briefly while
# it settles, so outage/unknown periods that BEGIN within this many minutes of
# the device being added are not logged.
ADD_GRACE_MINUTES = 30


def _device_transitions(conn, ip, since_iso):
    """Status-change rows for one device (run starts only), via a LAG window so we
    read transitions, not every ping. Returns [(dt, status), ...] ascending."""
    base = """
        SELECT timestamp, status FROM (
            SELECT timestamp, status,
                   LAG(status) OVER (ORDER BY timestamp) AS prev
            FROM ping_history WHERE device_ip = ?{extra}
        ) t WHERE prev IS NULL OR status <> prev
        ORDER BY timestamp"""
    if since_iso is not None:
        rows = conn.execute(q(base.format(extra=" AND timestamp >= ?")), (ip, since_iso)).fetchall()
    else:
        rows = conn.execute(q(base.format(extra="")), (ip,)).fetchall()
    return [(_to_dt(r['timestamp']), r['status']) for r in rows]


def _pause_periods(conn, ip, since_iso):
    """Paused periods for a device: {start, end, ongoing, kind}. `kind` is
    'monitoring' (agent stopped pinging) or 'notifications' (still pinged). Includes
    periods still open (resumed_at NULL) or that ended within the window."""
    if since_iso is not None:
        rows = conn.execute(
            q("""SELECT id, paused_at, resumed_at, kind FROM pause_periods
                 WHERE device_ip = ? AND (resumed_at IS NULL OR resumed_at >= ?)
                 ORDER BY paused_at"""),
            (ip, since_iso)).fetchall()
    else:
        rows = conn.execute(
            q("SELECT id, paused_at, resumed_at, kind FROM pause_periods WHERE device_ip = ? ORDER BY paused_at"),
            (ip,)).fetchall()
    out = []
    for r in rows:
        d = _norm(r)
        out.append({'id': d['id'], 'start': d['paused_at'], 'end': d.get('resumed_at'),
                    'ongoing': d.get('resumed_at') is None,
                    'kind': d.get('kind') or 'monitoring'})
    return out


def _overlap_seconds(a, b, intervals):
    """Total seconds of [a, b) covered by any (start, end) interval in `intervals`."""
    s = 0.0
    for ps, pe in intervals:
        lo, hi = max(a, ps), min(b, pe)
        if hi > lo:
            s += (hi - lo).total_seconds()
    return s


def _pause_intervals(pauses, now_dt):
    """[(start_dt, end_dt), ...] from pause-period dicts; an open pause runs to now.
    Only **monitoring** pauses count here (they clip/exclude down/unknown + uptime,
    since the device wasn't pinged). Notifications pauses keep recording, so their
    time is NOT subtracted — the outage inside them is real and stays in the log."""
    out = []
    for p in pauses or []:
        if p.get('kind', 'monitoring') != 'monitoring':
            continue
        ps = _to_dt(p['start'])
        pe = _to_dt(p['end']) if p.get('end') else now_dt
        if ps is not None and pe is not None and pe > ps:
            out.append((ps, pe))
    return out


def _subtract_pauses(periods, pause_intervals, now_dt):
    """Cut paused time out of (start_dt, period_dict) pairs. A paused device is
    neither up nor down, so a down/unknown run is split around any overlapping
    pause (and an ongoing run that runs into a pause is truncated at pause start).
    Returns new (start_dt, period_dict) pairs."""
    if not pause_intervals:
        return periods
    out = []
    for start_dt, p in periods:
        was_ongoing = p['end'] is None
        end_dt = now_dt if was_ongoing else _to_dt(p['end'])
        segments = [(start_dt, end_dt)]
        for ps, pe in pause_intervals:
            nxt = []
            for s, e in segments:
                if pe <= s or ps >= e:          # no overlap
                    nxt.append((s, e))
                    continue
                if ps > s:                       # keep the piece before the pause
                    nxt.append((s, ps))
                if pe < e:                       # keep the piece after the pause
                    nxt.append((pe, e))
            segments = nxt
        for s, e in segments:
            if e <= s:
                continue
            ongoing = was_ongoing and e == end_dt
            out.append((s, {'status': p['status'], 'start': s.isoformat(),
                            'end': None if ongoing else e.isoformat(),
                            'ongoing': ongoing}))
    return out


def _maintenance_windows(conn, ip, since_iso):
    """Event/maintenance windows for a device: [{id, start, end, category, note,
    event_group_id}] (UTC ISO), those that end on/after the log cutoff."""
    cols = "id, start_at, end_at, category, note, event_group_id"
    if since_iso is not None:
        rows = conn.execute(
            q(f"""SELECT {cols} FROM maintenance_windows
                 WHERE device_ip = ? AND end_at >= ? ORDER BY start_at"""),
            (ip, since_iso)).fetchall()
    else:
        rows = conn.execute(
            q(f"SELECT {cols} FROM maintenance_windows WHERE device_ip = ? ORDER BY start_at"),
            (ip,)).fetchall()
    out = []
    for r in rows:
        d = _norm(r)
        out.append({'id': d['id'], 'start': d['start_at'], 'end': d['end_at'],
                    'category': d.get('category') or 'maintenance',
                    'note': d.get('note'), 'event_group_id': d.get('event_group_id')})
    return out


def _maintenance_intervals(windows):
    """[(start_dt, end_dt), ...] from window dicts, for overlap math."""
    out = []
    for w in windows or []:
        ws, we = _to_dt(w['start']), _to_dt(w['end'])
        if ws is not None and we is not None and we > ws:
            out.append((ws, we))
    return out


# The maintenance form stores minute-precision times, so "mark the whole outage"
# can miss the outage's sub-minute edges by up to ~60s and leave a tiny residual
# offline sliver (a bogus "0 minute outage"). When a window edge lands within this
# tolerance of an outage edge, snap it to the outage edge so the coverage is exact.
# Deliberate narrowing is always ≥1 minute, so it's unaffected.
_MAINT_SNAP_SECONDS = 60


def _snap_window(ws, we, s, e):
    """Snap a window's (ws, we) to an outage's (s, e) edges when within tolerance."""
    if abs((ws - s).total_seconds()) < _MAINT_SNAP_SECONDS:
        ws = s
    if abs((we - e).total_seconds()) < _MAINT_SNAP_SECONDS:
        we = e
    return ws, we


def _overlapping_events(start_iso, end_iso, windows, now_dt):
    """The event windows that overlap a period [start, end) for this device, as
    tags: [{event_group_id, maint_id, category, description}]. An open (ongoing)
    period is treated as ending 'now'. This is an OVERLAY only — the period keeps
    its own status ('down'/'paused'/'unknown'); the tags just say "…and this time
    was part of these event(s)." Window edges are snapped to the period edges to
    absorb the form's minute-level truncation."""
    s = _to_dt(start_iso)
    e = now_dt if not end_iso else _to_dt(end_iso)
    if s is None or e is None or e <= s:
        return []
    tags = []
    for w in windows or []:
        ws, we = _to_dt(w['start']), _to_dt(w['end'])
        if ws is None or we is None or we <= ws:
            continue
        ws, we = _snap_window(ws, we, s, e)
        if we > s and ws < e:                     # real overlap
            tags.append({'event_group_id': w.get('event_group_id'),
                         'maint_id': w['id'],
                         'category': w.get('category') or 'maintenance',
                         'description': w.get('note')})
    return tags


def _tag_events(periods, windows, now_dt):
    """Attach an `events` list to each period that overlaps any event window. The
    period itself is unchanged (still down/paused/unknown); tagging is purely the
    'part of an event' indicator shown in the activity log."""
    for p in periods or []:
        p['events'] = _overlapping_events(p.get('start'), p.get('end'), windows, now_dt)
    return periods


def _first_ping(conn, ip):
    row = conn.execute(
        q("SELECT MIN(timestamp) AS t FROM ping_history WHERE device_ip = ?"), (ip,)
    ).fetchone()
    return row['t'] if row else None


def _node_created(conn, ip):
    row = conn.execute(q("SELECT created_at FROM nodes WHERE ip = ?"), (ip,)).fetchone()
    return row['created_at'] if row else None


def _node_decommissioned_at(conn, ip):
    """Naive-UTC ISO when this device was decommissioned, or None if it's still active."""
    row = conn.execute(q("SELECT decommissioned_at FROM nodes WHERE ip = ?"), (ip,)).fetchone()
    if not row or row['decommissioned_at'] is None:
        return None
    return _norm(row).get('decommissioned_at')


def _cap_ongoing(result, cap_iso):
    """Close any still-ongoing down/unknown/paused run at `cap_iso` (the decommission
    time). Monitoring ended then, so an open outage shouldn't grow forever or read as
    'ongoing' for a device that's no longer watched."""
    if not cap_iso:
        return result
    for key in ('down', 'unknown', 'paused'):
        for p in result.get(key, []):
            if p.get('ongoing') and p.get('start') and p['start'] <= cap_iso:
                p['end'] = cap_iso
                p['ongoing'] = False
    return result


def _assemble_device_log(created_raw, first_raw, trans, since_iso=None, pauses=None,
                         maintenance=None):
    """Build {tracked_since, down[], unknown[]} from a node's created_at, its first
    ping time, and its status transitions. A run ends at the next transition
    (recovery) or is ongoing if it's the last."""
    cutoff = _to_dt(since_iso) if since_iso else None
    tracked = _to_dt(created_raw)
    first_dt = _to_dt(first_raw)
    if tracked is None:
        tracked = first_dt

    # (start_dt, period) pairs so we can filter the post-add grace window below.
    periods = []
    for i, (dt, st) in enumerate(trans):
        if st not in _LOG_STATUSES:
            continue
        end = trans[i + 1][0] if i + 1 < len(trans) else None
        periods.append((dt, {'status': st, 'start': dt.isoformat(),
                             'end': end.isoformat() if end else None, 'ongoing': end is None}))

    # A device is "unknown" from when tracking started until its first ping
    # (awaiting first contact); if never pinged, it's unknown up to now. Only
    # synthesize this when tracking began within the window, so devices first
    # tracked before the start date don't carry a pre-window unknown gap.
    if tracked is not None and (cutoff is None or tracked >= cutoff):
        if first_dt is not None and first_dt > tracked:
            periods.insert(0, (tracked, {'status': 'unknown', 'start': tracked.isoformat(),
                                         'end': first_dt.isoformat(), 'ongoing': False}))
        elif first_dt is None:
            periods.insert(0, (tracked, {'status': 'unknown', 'start': tracked.isoformat(),
                                         'end': None, 'ongoing': True}))

    # Drop outage/unknown periods that began within the first ADD_GRACE_MINUTES
    # after the device was added — the brief "settling in" noise. (This also
    # removes the synthesized initial-unknown gap, which starts at add time.)
    if tracked is not None:
        grace = tracked + timedelta(minutes=ADD_GRACE_MINUTES)
        periods = [(s, p) for (s, p) in periods if s >= grace]

    # A paused device is neither up nor down — subtract paused time from the
    # down/unknown runs so a device that was offline when paused stops being
    # counted offline the moment it's paused (rather than for the whole pause).
    now_dt = datetime.utcnow()
    if pauses:
        periods = _subtract_pauses(periods, _pause_intervals(pauses, now_dt), now_dt)

    # Events are an OVERLAY, not a relabeling: an outage/unknown run keeps its own
    # status and just gets tagged with any event window it overlaps (see
    # _tag_events). So a fully-covered outage still shows as "offline", now marked
    # "part of <event>". `maintenance` stays [] for backward-compat with callers.
    down_list = _tag_events([p for (s, p) in periods if p['status'] == 'down'],
                            maintenance, now_dt)
    unknown_list = _tag_events([p for (s, p) in periods if p['status'] == 'unknown'],
                               maintenance, now_dt)
    return {
        'tracked_since': tracked.isoformat() if tracked else None,
        'down': down_list,
        'maintenance': [],
        'unknown': unknown_list,
    }


def get_device_log(ip, since_iso=None):
    with get_conn() as conn:
        created = _node_created(conn, ip)
        first = _first_ping(conn, ip)
        trans = _device_transitions(conn, ip, since_iso)
        pauses = _pause_periods(conn, ip, since_iso)
        maint = _maintenance_windows(conn, ip, since_iso)
        decom = _node_decommissioned_at(conn, ip)
    result = _assemble_device_log(created, first, trans, since_iso, pauses, maint)
    result['paused'] = _tag_events(pauses, maint, datetime.utcnow())
    result['deleted'] = decom     # removal timestamp (naive-UTC ISO) or None
    return _cap_ongoing(result, decom)


def current_issue_start(ip):
    """The start (naive-UTC ISO) of a device's CURRENT ongoing problem — an in-progress
    outage, unknown stretch, or pause — or None if it's fine right now. Used to anchor
    the window of a 'still affected' device when marking an ongoing event with no start."""
    log = get_device_log(ip)
    cands = [p['start'] for key in ('down', 'unknown', 'paused')
             for p in log.get(key, []) if p.get('ongoing')]
    return min(cands) if cands else None


def ongoing_event_categories(ips=None):
    """{ip: category} for devices CURRENTLY affected by an ongoing event — i.e. they
    have an OPEN-ENDED (sentinel-end) event window that has already started. One entry
    per ip (the most recently started event's category). `ips=None` means all devices."""
    now_dt = datetime.utcnow()
    with get_conn() as conn:
        if ips:
            ph = ','.join('?' * len(ips))
            rows = conn.execute(q(
                f"""SELECT device_ip, start_at, category FROM maintenance_windows
                    WHERE device_ip IN ({ph}) AND CAST(end_at AS TEXT) >= '9999'
                    ORDER BY start_at DESC"""), list(ips)).fetchall()
        else:
            rows = conn.execute(q(
                """SELECT device_ip, start_at, category FROM maintenance_windows
                   WHERE CAST(end_at AS TEXT) >= '9999' ORDER BY start_at DESC""")).fetchall()
    out = {}
    for r in rows:
        d = _norm(r)
        st = _to_dt(d['start_at'])
        if st is not None and st > now_dt:      # a future (scheduled) event hasn't started
            continue
        out.setdefault(d['device_ip'], d.get('category') or 'maintenance')
    return out


def clip_down_to_pauses(down_periods, pauses):
    """Remove paused time from a list of {start, end, ongoing} offline periods.
    Used by the routes' switch-outage overlay: a parent switch's outage must not
    show an AP as offline during a stretch when the AP itself was paused."""
    now_dt = datetime.utcnow()
    intervals = _pause_intervals(pauses, now_dt)
    if not intervals:
        return down_periods
    pairs = [(_to_dt(p['start']), {'status': 'down', **p}) for p in down_periods]
    clipped = _subtract_pauses(pairs, intervals, now_dt)
    return [{'start': p['start'], 'end': p['end'], 'ongoing': p['ongoing']}
            for _, p in clipped]


def get_all_device_logs(ips, since_iso=None):
    """Per-device logs for many devices, keyed by IP, over one connection."""
    out = {}
    with get_conn() as conn:
        for ip in ips:
            created = _node_created(conn, ip)
            first = _first_ping(conn, ip)
            trans = _device_transitions(conn, ip, since_iso)
            pauses = _pause_periods(conn, ip, since_iso)
            maint = _maintenance_windows(conn, ip, since_iso)
            out[ip] = _assemble_device_log(created, first, trans, since_iso, pauses, maint)
            out[ip]['paused'] = _tag_events(pauses, maint, datetime.utcnow())
            _cap_ongoing(out[ip], _node_decommissioned_at(conn, ip))
    return out


def get_outage_counts(since_iso=None):
    """Number of outages per device IP within the window, used to flag devices that
    go down notably more than the others. Outages **fully covered** by an EVENT window
    (any category — maintenance, lightning, power outage, other) are excluded; an
    outage only partly covered still counts, because its uncovered stretch is a real
    problem. Event windows live in maintenance_windows; the exclusion here is
    category-agnostic (all categories exclude)."""
    # Only devices that actually had a down transition in the window — keeps the
    # per-device interval work below tiny (most devices contribute nothing).
    base = """
        SELECT DISTINCT device_ip FROM (
            SELECT device_ip, status,
                   LAG(status) OVER (PARTITION BY device_ip ORDER BY timestamp) AS prev
            FROM ping_history{extra}
        ) t WHERE status = 'down' AND (prev IS NULL OR prev <> 'down')"""
    now_dt = datetime.utcnow()
    counts = {}
    with get_conn() as conn:
        if since_iso is not None:
            ips = [r['device_ip'] for r in
                   conn.execute(q(base.format(extra=" WHERE timestamp >= ?")), (since_iso,)).fetchall()]
        else:
            ips = [r['device_ip'] for r in conn.execute(base.format(extra="")).fetchall()]
        for ip in ips:
            trans = _device_transitions(conn, ip, since_iso)
            wins = _maintenance_intervals(_maintenance_windows(conn, ip, since_iso))
            n = 0
            for i, (dt, st) in enumerate(trans):
                if st != 'down':
                    continue
                end = trans[i + 1][0] if i + 1 < len(trans) else now_dt
                span = (end - dt).total_seconds()
                # Snap window edges to this outage (absorb minute-truncation) so a
                # whole-outage mark counts as fully covered → excluded here too.
                snapped = [_snap_window(ws, we, dt, end) for (ws, we) in wins]
                # Count the outage unless maintenance covers essentially all of it.
                if span - _overlap_seconds(dt, end, snapped) > 1:
                    n += 1
            if n:
                counts[ip] = n
    return counts


# ── Event / maintenance windows (mark an outage stretch as an event) ─────
# An "event" is one or more maintenance_windows rows (one per device) sharing an
# event_group_id, carrying a category (maintenance | lightning | power_outage | other)
# and a free-text description (stored in `note`). A single-device outage tag uses the
# same rows with event_group_id NULL. All categories exclude fully-covered outages.

def add_maintenance_window(device_ip, start_iso, end_iso, note=None, created_by=None,
                           category='maintenance', event_group_id=None):
    """Create one event/maintenance row (start/end naive-UTC ISO). Returns id."""
    cols = "(device_ip, node_id, start_at, end_at, note, created_by, category, event_group_id)"
    vals = (device_ip, _nid(device_ip), start_iso, end_iso, note, created_by, category, event_group_id)
    with get_conn() as conn:
        if _PG:
            row = conn.execute(
                q(f"INSERT INTO maintenance_windows {cols} VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id"),
                vals).fetchone()
            return row['id']
        cur = conn.execute(
            q(f"INSERT INTO maintenance_windows {cols} VALUES (?, ?, ?, ?, ?, ?, ?, ?)"), vals)
        return cur.lastrowid


def add_event(category, description, start_iso, end_iso, device_ips, group_id,
              created_by=None):
    """Create an event: one maintenance_windows row per device sharing `group_id`."""
    for ip in device_ips:
        add_maintenance_window(ip, start_iso, end_iso, note=description,
                               created_by=created_by, category=category,
                               event_group_id=group_id)


def get_events(ips):
    """Events overlapping the given device IPs, grouped by event_group_id (rows with no
    group are their own single-device event). Each: {group_id, category, description,
    start, end, device_ips[], maint_ids[], created_at}. Times are naive-UTC ISO."""
    if not ips:
        return []
    ph = ','.join('?' * len(ips))
    with get_conn() as conn:
        rows = conn.execute(
            q(f"""SELECT id, device_ip, start_at, end_at, note, category,
                         event_group_id, created_at
                  FROM maintenance_windows WHERE device_ip IN ({ph})
                  ORDER BY start_at DESC"""),
            list(ips)).fetchall()
    groups = {}
    for r in rows:
        d = _norm(r)
        key = d['event_group_id'] or f"single:{d['id']}"
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                'group_id': d['event_group_id'], 'category': d.get('category') or 'maintenance',
                'description': d.get('note'), 'start': d['start_at'], 'end': d['end_at'],
                'device_ips': [], 'maint_ids': [], 'devices': [], 'created_at': d.get('created_at'),
            }
        g['device_ips'].append(d['device_ip'])
        g['maint_ids'].append(d['id'])
        # Per-device windows: a device can be resolved (its end capped) independently
        # while the event stays open for others.
        g['devices'].append({'ip': d['device_ip'], 'start': d['start_at'], 'end': d['end_at']})
        # The group's overall span is the extremes of its (possibly differing) rows.
        g['start'] = min(g['start'], d['start_at'])
        g['end'] = max(g['end'], d['end_at'])
    return list(groups.values())


def update_event_times(group_id, start_iso, end_iso, description=None):
    """Move the event window. START applies to every device; the END only moves the
    rows that are still 'open' (at the group's current max end), so devices that were
    individually resolved earlier keep their own end. Description (if given) → all."""
    with get_conn() as conn:
        conn.execute(q("UPDATE maintenance_windows SET start_at = ? WHERE event_group_id = ?"),
                     (start_iso, group_id))
        conn.execute(q("""UPDATE maintenance_windows SET end_at = ?
                          WHERE event_group_id = ? AND end_at = (
                            SELECT MAX(end_at) FROM maintenance_windows WHERE event_group_id = ?)"""),
                     (end_iso, group_id, group_id))
        if description is not None:
            conn.execute(q("UPDATE maintenance_windows SET note = ? WHERE event_group_id = ?"),
                         (description, group_id))


def set_event_device_end(group_id, ip, end_iso, only_open=False):
    """Cap ONE device's window within an event (resolve it) or reopen it (pass the
    open-ended sentinel). Its history stays tagged; later activity past `end_iso` is no
    longer attributed to the event. `only_open=True` caps ONLY the device's open-ended
    (still-ongoing) row(s) — so resolving doesn't clobber separate capped rows recorded
    for the device's earlier event-related outages."""
    where = "event_group_id = ? AND device_ip = ?"
    params = [end_iso, group_id, ip]
    if only_open:
        # CAST forces a TEXT compare — the end_at column has numeric affinity on SQLite,
        # so a bare `end_at >= '9999'` would coerce to numbers and match every row.
        where += " AND CAST(end_at AS TEXT) >= '9999'"   # OPEN_ENDED sentinel starts 9999-…
    with get_conn() as conn:
        conn.execute(q(f"UPDATE maintenance_windows SET end_at = ? WHERE {where}"), params)


def end_event_open_devices(group_id, ips, end_iso) -> bool:
    """Cap the still-open (ongoing) rows of an event for the given device IPs at
    `end_iso`. Used when a linked monitoring pause ends: an event kept "ongoing" only
    by its pause(s) should end when the pause(s) end. Only touches open-ended rows, so
    a device's earlier capped (resolved) rows are untouched. Returns True if the event
    has NO open-ended rows left afterward (i.e. it is now fully ended)."""
    if not ips:
        return False
    ph = ','.join('?' * len(ips))
    with get_conn() as conn:
        # CAST forces a TEXT compare — end_at has numeric affinity on SQLite, so a bare
        # `end_at >= '9999'` would coerce to numbers and match every row.
        conn.execute(
            q(f"""UPDATE maintenance_windows SET end_at = ?
                  WHERE event_group_id = ? AND device_ip IN ({ph})
                    AND CAST(end_at AS TEXT) >= '9999'"""),
            (end_iso, group_id, *ips))
        remaining = _norm(conn.execute(
            q("""SELECT COUNT(*) AS n FROM maintenance_windows
                 WHERE event_group_id = ? AND CAST(end_at AS TEXT) >= '9999'"""),
            (group_id,)).fetchone())
    return (remaining['n'] if remaining else 0) == 0


def update_event_description(group_id, description):
    """Set (or clear, when None) the description on every row of an event group."""
    with get_conn() as conn:
        conn.execute(q("UPDATE maintenance_windows SET note = ? WHERE event_group_id = ?"),
                     (description, group_id))


def update_event_category(group_id, category):
    """Change the category on every row of an event group."""
    with get_conn() as conn:
        conn.execute(q("UPDATE maintenance_windows SET category = ? WHERE event_group_id = ?"),
                     (category, group_id))


def delete_event(group_id):
    """Delete every overlay row of an event group. Returns the device IPs affected."""
    with get_conn() as conn:
        ips = [_norm(r)['device_ip'] for r in conn.execute(
            q("SELECT device_ip FROM maintenance_windows WHERE event_group_id = ?"),
            (group_id,)).fetchall()]
        conn.execute(q("DELETE FROM maintenance_windows WHERE event_group_id = ?"), (group_id,))
    return ips


def get_event(group_id):
    """Meta for one event group (category, description, start, end, device_ips), or None."""
    with get_conn() as conn:
        rows = conn.execute(
            q("""SELECT id, device_ip, start_at, end_at, note, category
                 FROM maintenance_windows WHERE event_group_id = ? ORDER BY start_at"""),
            (group_id,)).fetchall()
    if not rows:
        return None
    ds = [_norm(r) for r in rows]
    d0 = ds[0]
    return {'group_id': group_id, 'category': d0.get('category') or 'maintenance',
            'description': d0.get('note'),
            'start': min(d['start_at'] for d in ds), 'end': max(d['end_at'] for d in ds),
            'device_ips': [d['device_ip'] for d in ds],
            'maint_ids': [d['id'] for d in ds],
            'devices': [{'ip': d['device_ip'], 'start': d['start_at'], 'end': d['end_at']} for d in ds]}


def add_event_devices(group_id, ips, created_by=None):
    """Add devices to an existing event group (one overlay row each, reusing the
    group's window/category/description). Skips devices already in the group. Returns
    the IPs actually added."""
    ev = get_event(group_id)
    if not ev:
        return []
    existing = set(ev['device_ips'])
    added = []
    for ip in ips:
        if ip in existing:
            continue
        add_maintenance_window(ip, ev['start'], ev['end'], note=ev['description'],
                               created_by=created_by, category=ev['category'],
                               event_group_id=group_id)
        added.append(ip)
    return added


def remove_event_devices(group_id, ips):
    """Remove devices from an event group (delete their overlay rows)."""
    if not ips:
        return
    ph = ','.join('?' * len(ips))
    with get_conn() as conn:
        conn.execute(
            q(f"DELETE FROM maintenance_windows WHERE event_group_id = ? AND device_ip IN ({ph})"),
            [group_id, *ips])


def get_pause_log(ips, since_iso=None):
    """Device monitoring/notification pauses for the event log — one entry per
    pause_periods interval (historical + ongoing): {device_ip, start, end, ongoing, kind}."""
    if not ips:
        return []
    ph = ','.join('?' * len(ips))
    with get_conn() as conn:
        if since_iso is not None:
            rows = conn.execute(
                q(f"""SELECT device_ip, paused_at, resumed_at, kind FROM pause_periods
                      WHERE device_ip IN ({ph}) AND (resumed_at IS NULL OR resumed_at >= ?)
                      ORDER BY paused_at DESC"""), [*ips, since_iso]).fetchall()
        else:
            rows = conn.execute(
                q(f"""SELECT device_ip, paused_at, resumed_at, kind FROM pause_periods
                      WHERE device_ip IN ({ph}) ORDER BY paused_at DESC"""), list(ips)).fetchall()
    out = []
    for r in rows:
        d = _norm(r)
        out.append({'device_ip': d['device_ip'], 'start': d['paused_at'],
                    'end': d.get('resumed_at'), 'ongoing': d.get('resumed_at') is None,
                    'kind': d.get('kind') or 'monitoring'})
    return out


def update_maintenance_window(mid, start_iso, end_iso):
    with get_conn() as conn:
        cur = conn.execute(
            q("UPDATE maintenance_windows SET start_at = ?, end_at = ? WHERE id = ?"),
            (start_iso, end_iso, mid))
        return (cur.rowcount or 0) > 0


def set_maintenance_category(mid, category, note=None):
    """Set the category (and optional description) of a single event/maintenance row —
    used by the per-outage 'Mark as event' quick path."""
    with get_conn() as conn:
        conn.execute(q("UPDATE maintenance_windows SET category = ?, note = ? WHERE id = ?"),
                     (category, note, mid))


def delete_maintenance_window(mid):
    with get_conn() as conn:
        cur = conn.execute(q("DELETE FROM maintenance_windows WHERE id = ?"), (mid,))
        return (cur.rowcount or 0) > 0


def get_maintenance_window(mid):
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT id, device_ip, start_at, end_at, category, note FROM maintenance_windows WHERE id = ?"),
            (mid,)).fetchone()
    if not row:
        return None
    d = _norm(row)
    return {'id': d['id'], 'device_ip': d['device_ip'],
            'start': d['start_at'], 'end': d['end_at'],
            'category': d.get('category') or 'maintenance', 'description': d.get('note')}


def get_app_downtime_periods(site, since_iso=None):
    """Periods when a whole site's agent/app went quiet (nothing was pinged).
    Returns the stored, already-detected outage intervals (written by
    record_report), plus a synthesized ongoing one if the site's last report is
    currently stale (an outage in progress that hasn't been closed yet)."""
    now_dt = datetime.utcnow()
    where, params = ["site = ?"], [site]
    if since_iso is not None:
        where.append("end_ts >= ?"); params.append(since_iso)
    wsql = " WHERE " + " AND ".join(where)
    with get_conn() as conn:
        rows = conn.execute(
            q(f"SELECT start_ts, end_ts FROM app_downtime{wsql} ORDER BY start_ts"), params
        ).fetchall()

    periods = [{'start': _to_dt(r['start_ts']).isoformat(),
                'end': _to_dt(r['end_ts']).isoformat(), 'ongoing': False} for r in rows]

    # Outage in progress: the last report (in-memory watermark) is older than the
    # gap threshold, so the site is quiet right now. record_report will close this
    # into a stored interval on the next report.
    last = get_last_report(site)
    last_dt = _to_dt(last) if last else None
    if last_dt is not None and (now_dt - last_dt).total_seconds() > APP_GAP_SECONDS:
        periods.append({'start': last_dt.isoformat(), 'end': None, 'ongoing': True})
    return periods


# ── Sites (locations) ────────────────────────────────────────────────

def get_sites() -> list[dict]:
    """All locations, in display order (first = default site). `pending_delete_at`
    is the naive-UTC ISO time a scheduled soft-delete becomes permanent, or None."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT key, name, pending_delete_at FROM sites ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = _norm(r)
        out.append({'key': d['key'], 'name': d['name'],
                    'pending_delete_at': d.get('pending_delete_at')})
    return out


def schedule_site_deletion(key: str, delete_at):
    """Mark a location for deletion at `delete_at` (a datetime). It stays fully
    functional until then; purge_due_site_deletions() removes it once due."""
    global _pending_deletions
    with get_conn() as conn:
        conn.execute(q("UPDATE sites SET pending_delete_at = ? WHERE key = ?"),
                     (delete_at.isoformat(), key))
    with _rt_lock:
        _pending_deletions = True        # so the purge sweep starts running again


def cancel_site_deletion(key: str):
    """Undo a scheduled deletion (clear the watermark)."""
    with get_conn() as conn:
        conn.execute(q("UPDATE sites SET pending_delete_at = NULL WHERE key = ?"), (key,))
    refresh_pending_deletions()          # maybe others are still pending


def site_pending_delete_at(key: str):
    """The scheduled-deletion time for a site (ISO string), or None."""
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT pending_delete_at FROM sites WHERE key = ?"), (key,)).fetchone()
    return _norm(row).get('pending_delete_at') if row else None


def _row_to_pause(d: dict) -> dict:
    d = dict(d)
    d['device_ips'] = json.loads(d.get('device_ips') or '[]')
    return d


def add_scheduled_pause(site, name, start_at, end_at, device_ips, mode='monitoring',
                        status='scheduled', category=None, description=None,
                        event_group_id=None):
    """Create a pause row. status='scheduled' (default) → the watchdog activates it at
    start_at; status='active' → an IMMEDIATE pause (the caller must also apply
    set_device_pause now); the watchdog then only resumes it at end_at. The
    category/description/event_group_id link it to its Event."""
    global _open_pauses
    start_s = start_at.isoformat() if hasattr(start_at, 'isoformat') else start_at
    end_s = end_at.isoformat() if hasattr(end_at, 'isoformat') else end_at
    with get_conn() as conn:
        conn.execute(
            q("""INSERT INTO scheduled_pauses
                     (site, name, start_at, end_at, device_ips, status, mode,
                      category, description, event_group_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
            (site, name, start_s, end_s, json.dumps(list(device_ips)),
             status, mode, category, description, event_group_id))
    with _rt_lock:
        _open_pauses = True          # so the watchdog starts checking again


def get_scheduled_pauses(site, statuses=('scheduled', 'active')):
    ph = ','.join('?' * len(statuses))
    with get_conn() as conn:
        rows = conn.execute(
            q(f"""SELECT id, site, name, start_at, end_at, device_ips, status, mode,
                         category, description, event_group_id
                  FROM scheduled_pauses WHERE site = ? AND status IN ({ph})
                  ORDER BY start_at"""),
            (site, *statuses)).fetchall()
    return [_row_to_pause(_norm(r)) for r in rows]


def get_pause_modes_by_group(site):
    """{event_group_id: {'status': ..., 'mode': ...}} for the site's pauses that are
    linked to an Event (scheduled or active). Lets the Event Logs list show whether an
    event also pauses, without a per-event query."""
    with get_conn() as conn:
        rows = conn.execute(
            q("""SELECT event_group_id, status, mode FROM scheduled_pauses
                 WHERE site = ? AND event_group_id IS NOT NULL
                   AND status IN ('scheduled', 'active')"""),
            (site,)).fetchall()
    return {r['event_group_id']: {'status': r['status'], 'mode': r['mode']}
            for r in (_norm(x) for x in rows)}


def get_scheduled_pause(pause_id):
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT * FROM scheduled_pauses WHERE id = ?"), (pause_id,)).fetchone()
    return _row_to_pause(_norm(row)) if row else None


def set_scheduled_pause_status(pause_id, status):
    with get_conn() as conn:
        conn.execute(q("UPDATE scheduled_pauses SET status = ? WHERE id = ?"),
                     (status, pause_id))


def get_pause_by_group(group_id):
    """The scheduled/active pause linked to an event group, or None."""
    with get_conn() as conn:
        row = conn.execute(
            q("""SELECT * FROM scheduled_pauses
                 WHERE event_group_id = ? AND status IN ('scheduled', 'active')
                 ORDER BY id DESC"""), (group_id,)).fetchone()
    return _row_to_pause(_norm(row)) if row else None


def update_scheduled_pause_times(pause_id, start_iso, end_iso):
    with get_conn() as conn:
        conn.execute(q("UPDATE scheduled_pauses SET start_at = ?, end_at = ? WHERE id = ?"),
                     (start_iso, end_iso, pause_id))


def set_scheduled_pause_devices(pause_id, device_ips):
    with get_conn() as conn:
        conn.execute(q("UPDATE scheduled_pauses SET device_ips = ? WHERE id = ?"),
                     (json.dumps(list(device_ips)), pause_id))


def delete_scheduled_pause(pause_id):
    with get_conn() as conn:
        conn.execute(q("DELETE FROM scheduled_pauses WHERE id = ?"), (pause_id,))


def due_scheduled_pause_starts(now_iso):
    """Scheduled (not-yet-started) pauses whose start time has arrived."""
    with get_conn() as conn:
        rows = conn.execute(
            q("SELECT id, name, start_at, end_at, device_ips, mode FROM scheduled_pauses "
              "WHERE status = 'scheduled' AND start_at <= ?"), (now_iso,)).fetchall()
    return [_row_to_pause(_norm(r)) for r in rows]


def due_scheduled_pause_ends(now_iso):
    """Active pauses whose end time has arrived."""
    with get_conn() as conn:
        rows = conn.execute(
            q("SELECT id, name, end_at, device_ips, event_group_id FROM scheduled_pauses "
              "WHERE status = 'active' AND end_at <= ?"), (now_iso,)).fetchall()
    return [_row_to_pause(_norm(r)) for r in rows]


def drop_ips_from_active_pauses(ips_to_drop) -> list[str]:
    """Remove the given device IPs from every ACTIVE scheduled pause that covers
    them (used when a device is manually resumed mid-window, so the window's
    device list reflects reality). A window left with no devices is marked 'done'.
    Returns the distinct names of the windows that were adjusted."""
    drop_set = set(ips_to_drop)
    adjusted = []
    with get_conn() as conn:
        rows = conn.execute(
            q("SELECT id, name, device_ips FROM scheduled_pauses WHERE status = 'active'")
        ).fetchall()
        for r in rows:
            d = _norm(r)
            original = json.loads(d.get('device_ips') or '[]')
            remaining = [ip for ip in original if ip not in drop_set]
            if remaining != original:
                adjusted.append(d['name'])
                if remaining:
                    conn.execute(
                        q("UPDATE scheduled_pauses SET device_ips = ? WHERE id = ?"),
                        (json.dumps(remaining), d['id']))
                else:
                    conn.execute(
                        q("UPDATE scheduled_pauses SET device_ips = ?, status = 'done' WHERE id = ?"),
                        (json.dumps(remaining), d['id']))
    return adjusted


def active_pause_ips(exclude_id=None) -> set:
    """Union of device IPs currently held paused by *active* scheduled pauses
    (optionally excluding one), so a window ending won't resume a device another
    window still wants paused."""
    with get_conn() as conn:
        rows = conn.execute(
            q("SELECT id, device_ips FROM scheduled_pauses WHERE status = 'active'")).fetchall()
    ips = set()
    for r in rows:
        d = _norm(r)
        if exclude_id is not None and d['id'] == exclude_id:
            continue
        ips.update(json.loads(d.get('device_ips') or '[]'))
    return ips


# ── User activity (audit trail) ──────────────────────────────────────
# One row per successful, user-initiated mutating request — written by the
# after_request hook in routes.py. Grows only with human actions (slow), not
# with pings/agent traffic, so there's no auto-pruning yet.

def log_user_activity(actor, method, endpoint, path, summary, status, site=None):
    with get_conn() as conn:
        conn.execute(
            q("""INSERT INTO user_activity (actor, method, endpoint, path, summary, status, site)
                 VALUES (?, ?, ?, ?, ?, ?, ?)"""),
            (actor, method, endpoint, path, summary, status, site))


def get_user_activity(limit=200, site=None):
    """Newest-first audit rows. When `site` is given, returns only that location's
    rows (pre-migration rows were backfilled to the default site, so nothing is
    lost — see _migrate_add_user_activity_site)."""
    with get_conn() as conn:
        if site is None:
            rows = conn.execute(
                q("SELECT * FROM user_activity ORDER BY ts DESC, id DESC LIMIT ?"),
                (limit,)).fetchall()
        else:
            rows = conn.execute(
                q("""SELECT * FROM user_activity
                     WHERE site = ?
                     ORDER BY ts DESC, id DESC LIMIT ?"""),
                (site, limit)).fetchall()
    return [_norm(r) for r in rows]


def purge_due_site_deletions() -> list[dict]:
    """Permanently delete every location whose scheduled deletion time has passed.
    Returns [{key, name}, …] of the ones removed (empty if none were due)."""
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        due = conn.execute(
            q("SELECT key, name FROM sites WHERE pending_delete_at IS NOT NULL AND pending_delete_at <= ?"),
            (now,)).fetchall()
    removed = [{'key': r['key'], 'name': r['name']} for r in due]
    for s in removed:
        delete_site(s['key'])          # cascades devices + their per-IP data
    if removed:
        refresh_pending_deletions()    # recompute (future-dated ones may remain)
    return removed


def sites_count() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM sites").fetchone()
    return row['n']


def seed_sites(sites: list[dict]):
    """One-time population from settings.yaml. Caller guards on an empty table."""
    with get_conn() as conn:
        for s in sites:
            conn.execute(q("INSERT INTO sites (key, name) VALUES (?, ?)"),
                         (s['key'], s['name']))


def add_site(key: str, name: str):
    with get_conn() as conn:
        conn.execute(q("INSERT INTO sites (key, name) VALUES (?, ?)"), (key, name))


def rename_site(key: str, name: str):
    with get_conn() as conn:
        conn.execute(q("UPDATE sites SET name = ? WHERE key = ?"), (name, key))


def delete_site(key: str):
    """Permanently remove a location, all its devices/switches, and every per-IP
    data row (state, history, position, note, pauses) belonging to them. The
    site's key is never used as a table name, so no injection surface."""
    with get_conn() as conn:
        ips = [r['ip'] for r in
               conn.execute(q("SELECT ip FROM nodes WHERE site = ?"), (key,)).fetchall()]
        for ip in ips:
            for table in ('device_positions', 'device_positions_mobile',
                          'device_positions_geo', 'device_notes',
                          'device_states', 'ping_history', 'pause_periods'):
                conn.execute(q(f"DELETE FROM {table} WHERE device_ip = ?"), (ip,))
        conn.execute(q("DELETE FROM nodes WHERE site = ?"), (key,))
        conn.execute(q("DELETE FROM app_downtime WHERE site = ?"), (key,))
        conn.execute(q("DELETE FROM sites WHERE key = ?"), (key,))
        # The site's custom map area (if an admin set one) goes with it, so a
        # recreated location with the same key starts from the config default.
        conn.execute(q("DELETE FROM app_flags WHERE key = ?"), (f'basemap_view:{key}',))
    # Evict the removed devices + this site's watermark/check-flag from the caches
    # (caches are node_id-keyed; drop the map entries too since the rows are gone).
    with _rt_lock:
        for ip in ips:
            k = _ip_to_nid.get(ip, ip)
            _states.pop(k, None)
            _positions.pop(k, None)
            _positions_mobile.pop(k, None)
            _positions_geo.pop(k, None)
            _forget_ip(ip)
        _last_report.pop(key, None)
        _check_requested.discard(key)


# ── Simple key/value flags (e.g. "check requested" command channel) ──

def set_flag(key: str, value: str):
    with get_conn() as conn:
        if _PG:
            conn.execute(
                """INSERT INTO app_flags (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, value)
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO app_flags (key, value) VALUES (?, ?)",
                (key, value)
            )


def get_flag(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            q("SELECT value FROM app_flags WHERE key = ?"), (key,)
        ).fetchone()
    return row['value'] if row else None


# ── Editable app settings (the tuning knobs in the UI settings panel) ──────────
# Persisted in app_flags as 'setting:<key>' and cached in memory so the hot paths
# (per report / per fresh load / per agent poll) read them without a DB hit. Each
# spec is (default, min, max, env-var-that-seeds-the-default-if-set).
_SETTINGS_SPEC = {
    'outage_window_days':     (15, 1,  365,  None),
    'frequent_min_outages':   (3,  1,  1000, None),
    'down_confirm_checks':    (2,  1,  10,   'DOWN_CONFIRM_CHECKS'),
    'watchdog_stale_minutes': (30, 1,  1440, 'WATCHDOG_STALE_MINUTES'),
    'reminder_hours':         (6,  0,  168,  None),   # 0 disables the escalation
    'alert_on_recovery':      (1,  0,  1,    None),   # 0/1 toggle
    'ping_count':             (3,  1,  10,   None),
    'ping_timeout':           (5,  1,  30,   None),
}
_app_settings: dict = {}


def _setting_default(key: str) -> int:
    default, _lo, _hi, env = _SETTINGS_SPEC[key]
    if env and os.environ.get(env):
        try:
            return int(os.environ[env])
        except ValueError:
            pass
    return default


def load_app_settings():
    """Seed the in-memory settings cache from app_flags (else env/default). Once at
    startup so later reads never touch the DB."""
    with _rt_lock:
        _app_settings.clear()
        for key in _SETTINGS_SPEC:
            try:
                stored = get_flag('setting:' + key)
            except Exception:       # noqa: BLE001
                stored = None
            if stored is not None:
                try:
                    _app_settings[key] = int(stored)
                    continue
                except ValueError:
                    pass
            _app_settings[key] = _setting_default(key)


def get_setting(key: str) -> int:
    with _rt_lock:
        if key in _app_settings:
            return _app_settings[key]
    return _setting_default(key)


def get_all_settings() -> dict:
    return {k: get_setting(k) for k in _SETTINGS_SPEC}


def set_app_settings(updates: dict) -> dict:
    """Validate/clamp, persist, and update the cache. Ignores unknown/invalid keys.
    Returns the full new settings map."""
    for key, raw in (updates or {}).items():
        if key not in _SETTINGS_SPEC:
            continue
        _default, lo, hi, _env = _SETTINGS_SPEC[key]
        try:
            val = int(raw)
        except (ValueError, TypeError):
            continue
        val = max(lo, min(hi, val))
        try:
            set_flag('setting:' + key, str(val))
        except Exception:           # noqa: BLE001
            log.warning("Failed to persist setting %s", key)
        with _rt_lock:
            _app_settings[key] = val
    return get_all_settings()
