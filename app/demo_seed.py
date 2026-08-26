"""Fake data for the localhost demo build (DEMO_DATA=1).

Seeds two locations with a full, invented fleet and rich history so the demo can
showcase every feature with no agent, no real network, and no external database:
current outages, past outages, unknown/awaiting devices, monitoring & notification
pauses, a frequent-outage device, events (lightning / maintenance / ongoing power
outage) with covered outages and a linked pause, a decommissioned device, per-device
notes, app-downtime, scheduled pauses, and a user-activity trail across every category.

All IPs are RFC-5737 documentation ranges; every name/URL is invented. The "storyline"
is arbitrary — the point is coverage. Runs once, only on an empty DB (see create_app).
"""
from datetime import datetime, timedelta

from . import database as db

# Far-future sentinel for an open-ended ("ongoing") event — mirrors routes.OPEN_ENDED_ISO.
_OPEN_ENDED = '9999-12-31T23:59:59'
_A, _B = 'location-a', 'location-b'
_IM = 'https://intermapper.example.com/demo/device/{}/device.html'


def _iso(days=0, hours=0, minutes=0):
    return (datetime.utcnow() - timedelta(days=days, hours=hours, minutes=minutes)).isoformat()


def seed_demo():
    if db.sites_count() == 0:
        db.seed_sites([{'key': _A, 'name': 'Location A'},
                       {'key': _B, 'name': 'Location B'}])

    _add_nodes()
    with db.get_conn() as conn:
        _backdate_created(conn)
        _states_and_history(conn)
        _pauses(conn)
        _events(conn)
        _scheduled_pauses(conn)
        _misc(conn)


# ── Topology ──────────────────────────────────────────────────────────
def _add(ip, name, kind, site, loc, switch=None, uplink=None, x=None, y=None, host=None):
    db.add_node(ip=ip, name=name, kind=kind, site=site, location=loc, switch=switch,
                uplink=uplink, x=x, y=y, hostname=host, intermapper_url=_IM.format(name))


def _add_nodes():
    # Location A — the topology the demo brief asked for, plus one 'other' device.
    _add('192.0.2.11', 'switch-1', 'switch', _A, 'Switch 1', x=-260, y=-30,
         host='switch-1.demo.example.net')
    _add('198.51.100.11', 'ap-1-1', 'ap', _A, 'AP 1.1', switch='switch-1',
         host='ap-1-1.demo.example.net')
    _add('198.51.100.12', 'ap-1-2', 'ap', _A, 'AP 1.2', switch='switch-1')

    _add('192.0.2.12', 'switch-2', 'switch', _A, 'Switch 2', x=300, y=-30,
         host='switch-2.demo.example.net')
    for i in range(1, 9):
        # Give the first few APs a DNS hostname so the "DNS" row + ping-by-DNS is shown.
        host = f'ap-2-{i}.demo.example.net' if i <= 4 else None
        _add(f'198.51.100.{20 + i}', f'ap-2-{i}', 'ap', _A, f'AP 2.{i}', switch='switch-2', host=host)
    _add('198.51.100.40', 'camera-1', 'other', _A, 'Lobby Camera', switch='switch-2')

    # A decommissioned device so the "Deleted devices" page has content.
    _add('198.51.100.41', 'ap-2-9', 'ap', _A, 'AP 2.9 (retired)', switch='switch-2')

    # Location B — a second site, to showcase multi-location.
    _add('192.0.2.51', 'switch-b1', 'switch', _B, 'Building B Switch', x=0, y=0,
         host='switch-b1.demo.example.net')
    for i in range(1, 4):
        _add(f'198.51.100.{50 + i}', f'ap-b{i}', 'ap', _B, f'Building B AP {i}', switch='switch-b1')


def _backdate_created(conn):
    # Track everything from ~45 days ago so outages fall outside the post-add grace window.
    conn.execute(db.q("UPDATE nodes SET created_at = ?"), (_iso(days=45),))
    # Retire ap-2-9 five days ago (shows on the Deleted Devices page).
    conn.execute(db.q("UPDATE nodes SET decommissioned_at = ? WHERE ip = ?"),
                 (_iso(days=5), '198.51.100.41'))


# ── Status + ping history ─────────────────────────────────────────────
def _ping(conn, ip, status, when):
    conn.execute(
        db.q("INSERT INTO ping_history (device_ip, node_id, status, timestamp) VALUES (?, ?, ?, ?)"),
        (ip, db._nid(ip), status, when))


def _state(conn, ip, status, last_change, last_check=None):
    conn.execute(
        db.q("""INSERT INTO device_states (device_ip, node_id, current_status, last_check,
                    last_change, alert_active) VALUES (?, ?, ?, ?, ?, ?)"""),
        (ip, db._nid(ip), status, last_check or _iso(minutes=1), last_change,
         1 if status == 'down' else 0))


def _outage(conn, ip, start_days, dur_hours):
    """A resolved past outage: down at start, up `dur_hours` later."""
    _ping(conn, ip, 'down', _iso(days=start_days))
    _ping(conn, ip, 'up', _iso(days=start_days, hours=-dur_hours))


def _states_and_history(conn):
    up_now = lambda ip, lc=44: _state(conn, ip, 'up', _iso(days=lc))

    # switch-1: up, one past outage (later tagged 'maintenance' → excluded from frequent).
    _outage(conn, '192.0.2.11', 20, 1); _ping(conn, '192.0.2.11', 'up', _iso(minutes=5)); up_now('192.0.2.11')
    # switch-2 + healthy APs: up, clean.
    for ip in ('192.0.2.12', '198.51.100.11', '198.51.100.26', '192.0.2.51', '198.51.100.51'):
        _ping(conn, ip, 'up', _iso(days=44)); up_now(ip)

    # ap-1-2: currently PAUSED (monitoring) — see _pauses; also one past outage.
    _outage(conn, '198.51.100.12', 12, 2); up_now('198.51.100.12')
    conn.execute(db.q("UPDATE nodes SET enabled = 0 WHERE ip = ?"), ('198.51.100.12',))

    # ap-2-1: DOWN NOW (ongoing) — part of the lightning event.
    _ping(conn, '198.51.100.21', 'down', _iso(days=2, hours=4))
    _state(conn, '198.51.100.21', 'down', _iso(days=2, hours=4))

    # ap-2-2: UNKNOWN now (no ping ever → awaiting first contact).
    _state(conn, '198.51.100.22', 'unknown', _iso(days=45))

    # ap-2-3: NOTIFICATIONS paused now (still pinged) — see _pauses.
    _ping(conn, '198.51.100.23', 'up', _iso(days=44)); up_now('198.51.100.23')
    conn.execute(db.q("UPDATE nodes SET notify = 0 WHERE ip = ?"), ('198.51.100.23',))

    # ap-2-4, ap-2-5: up now, but had outages during the lightning window.
    _outage(conn, '198.51.100.24', 2, 3); _ping(conn, '198.51.100.24', 'up', _iso(days=1)); up_now('198.51.100.24')
    _outage(conn, '198.51.100.25', 2, 2); _ping(conn, '198.51.100.25', 'up', _iso(days=1)); up_now('198.51.100.25')

    # ap-2-7: up, single older outage.
    _outage(conn, '198.51.100.27', 8, 4); _ping(conn, '198.51.100.27', 'up', _iso(days=7)); up_now('198.51.100.27')

    # ap-2-8: FREQUENT-outage device — 7 short outages in the last 14 days, up now.
    for d in (13, 11, 9, 7, 5, 3, 1):
        _outage(conn, '198.51.100.28', d, 0.5)
    _ping(conn, '198.51.100.28', 'up', _iso(hours=6)); up_now('198.51.100.28')

    # camera-1 ('other'): up, one past outage.
    _outage(conn, '198.51.100.40', 15, 1); up_now('198.51.100.40')

    # ap-2-9 (retired): had history before it was decommissioned.
    _outage(conn, '198.51.100.41', 30, 6); _ping(conn, '198.51.100.41', 'up', _iso(days=28)); up_now('198.51.100.41')

    # Location B — ap-b2 down now, ap-b3 up (under an ongoing power-outage event).
    _ping(conn, '198.51.100.52', 'down', _iso(days=1, hours=6))
    _state(conn, '198.51.100.52', 'down', _iso(days=1, hours=6))
    _ping(conn, '198.51.100.53', 'up', _iso(days=44)); up_now('198.51.100.53')


# ── Pauses ────────────────────────────────────────────────────────────
def _pause(conn, ip, paused_days, resumed_days, kind='monitoring'):
    conn.execute(
        db.q("""INSERT INTO pause_periods (device_ip, node_id, paused_at, resumed_at, kind)
                VALUES (?, ?, ?, ?, ?)"""),
        (ip, db._nid(ip), _iso(days=paused_days),
         _iso(days=resumed_days) if resumed_days is not None else None, kind))


def _pauses(conn):
    _pause(conn, '198.51.100.12', 3, None, 'monitoring')      # ap-1-2: ongoing monitoring pause
    _pause(conn, '198.51.100.23', 2, None, 'notifications')   # ap-2-3: ongoing notifications pause
    _pause(conn, '198.51.100.24', 2, 2 - 3 / 24, 'monitoring')  # ap-2-4: closed pause during lightning
    _pause(conn, '198.51.100.27', 18, 17, 'notifications')    # a closed historical pause


# ── Events (overlay) ──────────────────────────────────────────────────
def _event_row(conn, ip, start, end, note, category, group_id):
    conn.execute(
        db.q("""INSERT INTO maintenance_windows (device_ip, node_id, start_at, end_at, note,
                    created_by, category, event_group_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""),
        (ip, db._nid(ip), start, end, note, 'demo.admin', category, group_id))


def _events(conn):
    # Lightning event over three APs; ap-2-1 still affected (open-ended), the others resolved.
    lg = 'evt-lightning-1'
    note = 'Lightning storm over the north field — APs 2.1 / 2.4 / 2.5 knocked offline.'
    _event_row(conn, '198.51.100.21', _iso(days=2, hours=4), _OPEN_ENDED, note, 'lightning', lg)
    _event_row(conn, '198.51.100.24', _iso(days=2, hours=4), _iso(days=2), note, 'lightning', lg)
    _event_row(conn, '198.51.100.25', _iso(days=2, hours=4), _iso(days=2), note, 'lightning', lg)

    # Past maintenance event on switch-1 — covers its outage (excluded from the frequent count).
    _event_row(conn, '192.0.2.11', _iso(days=20, hours=1), _iso(days=20, hours=-2),
               'Scheduled firmware upgrade.', 'maintenance', 'evt-maint-1')

    # Ongoing power outage at Location B (open-ended).
    _event_row(conn, '198.51.100.53', _iso(days=1, hours=8), _OPEN_ENDED,
               'Building B utility power outage — ticket #4471.', 'power_outage', 'evt-power-1')


# ── Scheduled pauses ──────────────────────────────────────────────────
def _sched(conn, site, name, start, end, ips, status, mode='monitoring',
           category=None, description=None, group=None):
    import json
    conn.execute(
        db.q("""INSERT INTO scheduled_pauses (site, name, start_at, end_at, device_ips, status,
                    mode, category, description, event_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""),
        (site, name, start, end, json.dumps(ips), status, mode, category, description, group))


def _scheduled_pauses(conn):
    # Past pause linked to the lightning event (status done).
    _sched(conn, _A, 'Lightning response', _iso(days=2, hours=4), _iso(days=2),
           ['198.51.100.24'], 'done', 'monitoring', 'lightning',
           'Paused while crews reset the north-field APs.', 'evt-lightning-1')
    # Upcoming scheduled pause (shows in the Scheduled Pauses tab).
    _sched(conn, _A, 'Planned maintenance', _iso(days=-3), _iso(days=-3, hours=-2),
           ['192.0.2.11', '198.51.100.11', '198.51.100.12'], 'scheduled', 'monitoring',
           'maintenance', 'Quarterly switch firmware window.')


# ── Notes, app-downtime, user activity ────────────────────────────────
def _ua(conn, actor, endpoint, summary, days, site=_A, method='POST'):
    conn.execute(
        db.q("""INSERT INTO user_activity (ts, actor, method, endpoint, path, summary, status, site)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""),
        (_iso(days=days), actor, method, endpoint, '/', summary, 200, site))


def _misc(conn):
    # Per-device note.
    conn.execute(
        db.q("INSERT INTO device_notes (device_ip, node_id, note, updated_at) VALUES (?, ?, ?, ?)"),
        ('192.0.2.11', db._nid('192.0.2.11'), 'Core switch — replaced PSU after the lightning event.',
         _iso(days=1)))

    # App-downtime interval (the pipeline went quiet once).
    conn.execute(db.q("INSERT INTO app_downtime (site, start_ts, end_ts) VALUES (?, ?, ?)"),
                 (_A, _iso(days=16, hours=1), _iso(days=16)))

    # User-activity trail spanning every category (added/deleted/monitoring/maintenance/other).
    _ua(conn, 'demo.admin', 'main.add_device', 'Added device Lobby Camera', 40)
    _ua(conn, 'jdoe', 'main.update_device', 'Edited device Switch 1', 21)
    _ua(conn, 'demo.admin', 'main.settings', 'Changed app settings', 12)
    _ua(conn, 'demo.admin', 'main.events', 'Marked a lightning event on AP 2.1, AP 2.4, AP 2.5', 2)
    _ua(conn, 'demo.admin', 'main.event_detail', 'Resolved AP 2.4 on an event', 2)
    _ua(conn, 'jdoe', 'main.set_device_enabled', 'Paused monitoring for AP 1.2', 3)
    _ua(conn, 'jdoe', 'main.scheduled_pauses', 'Scheduled a pause for Switch 1', 1)
    _ua(conn, 'demo.admin', 'main.delete_device', 'Deleted device AP 2.9 (retired)', 5)
    _ua(conn, 'jdoe', 'main.set_device_enabled', 'Paused notifications for Building B AP 1', 2, site=_B)
