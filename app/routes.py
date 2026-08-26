import hmac
import io
import json
import math
import os
import re
import statistics
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo('America/New_York')
_UTC = ZoneInfo('UTC')

# ── Frequent-outage detection ────────────────────────────────────────
# A device is "frequent" if it has gone down notably more than the fleet over a
# recent window: at least FREQUENT_MIN_OUTAGES and above mean + 1 std-dev of all
# devices' outage counts. The counts come from an OUTAGE_WINDOW_DAYS scan of
# ping_history — the ONLY database read the topology endpoint needs. To keep that scan
# rare:
#   1. It runs ONLY on a fresh app load / refresh — the request the browser makes
#      once when the page first opens (topology?fresh=1). The recurring 30s poll
#      passes no flag and NEVER scans; it serves the last computed value from memory.
#      So the frequent list is computed once when a user opens (or refreshes) the
#      app, and again when another user opens it — but is otherwise static.
#   2. A state change / maintenance edit only marks the cache DIRTY; it does not
#      scan. The next fresh load rescans only if something actually changed since the
#      last scan, so back-to-back opens with no changes reuse the cache. With the app
#      closed there are no fresh loads, so a flapping fleet triggers zero scans.
# (Recomputing eagerly on every state change was tried and reverted: on a flapping
# fleet that means a constant full-window scan even with nobody watching.)
OUTAGE_WINDOW_DAYS = 15   # rolling window for the frequent-outage count
FREQUENT_MIN_OUTAGES = 3
_outage_cache = {'primed': False, 'dirty': True, 'data': {}}
# {ip: category} of devices currently in an ongoing event — same fresh-load-only cache
# discipline as the outage counts, so the 30s poll never queries it.
_ongoing_event_cache = {'primed': False, 'dirty': True, 'data': {}}


def invalidate_outage_cache():
    """Mark the frequent-outage counts stale so the next *fresh app load* recomputes
    them. Deliberately does NOT scan here — called on a device state change /
    maintenance edit; the rescan is deferred to the next time a user opens/refreshes
    the app (so the app being closed, or a flapping fleet, triggers no scans)."""
    _outage_cache['dirty'] = True
    _ongoing_event_cache['dirty'] = True


def _ongoing_events_cached(force=False):
    """{ip: category} of devices currently in an ongoing event, served from memory.
    Recomputed only on first use or a fresh app load/refresh when dirty (like the
    outage counts) — the recurring topology poll passes force=False and never queries."""
    if not _ongoing_event_cache['primed'] or (force and _ongoing_event_cache['dirty']):
        try:
            _ongoing_event_cache['data'] = db.ongoing_event_categories()
            _ongoing_event_cache['primed'] = True
            _ongoing_event_cache['dirty'] = False
        except Exception:
            log.exception("Failed to compute ongoing-event categories")
    return _ongoing_event_cache['data']


def _outage_counts_cached(force=False):
    """{ip: outage_count} across all devices (all sites), served from memory. Runs a
    real DB scan only (a) the first time the value is ever needed, or (b) on a fresh
    app load/refresh (force=True) when something has changed since the last scan
    (dirty). The recurring topology poll passes force=False and never scans. Callers
    scope it to a site by looking up only that site's IPs."""
    if not _outage_cache['primed'] or (force and _outage_cache['dirty']):
        since = (datetime.utcnow() - timedelta(days=db.get_setting('outage_window_days'))).isoformat()
        try:
            _outage_cache['data'] = db.get_outage_counts(since)
            _outage_cache['primed'] = True
            _outage_cache['dirty'] = False
        except Exception:
            log.exception("Failed to compute outage counts")
    return _outage_cache['data']


def _frequent_set(full_counts):
    """full_counts: {ip: outage_count} for every tracked device (zeros included).
    Returns the set of IPs that count as frequent-outage."""
    vals = list(full_counts.values())
    if not vals:
        return set()
    mean = statistics.fmean(vals)
    stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    threshold = max(db.get_setting('frequent_min_outages'), mean + stdev)
    return {ip for ip, c in full_counts.items() if c >= threshold}
from flask import (
    Blueprint, jsonify, request, current_app, session, g,
    render_template, redirect, url_for, send_file,
)

from urllib.parse import urlencode

from . import database as db
from . import geo
from . import monitor
from . import notifier
from .auth import oauth, okta_enabled

bp = Blueprint('main', __name__)

SWITCH_COLS = 4
SWITCH_X_GAP = 270
SWITCH_Y_GAP = 230
SWITCH_X_ORIGIN = 160
SWITCH_Y_ORIGIN = 160

_IP_RE = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
NOTE_MAX_LEN = 2000
# Minimum lead time for scheduling a pause. The watchdog ticks slowly (8h) and only
# tightens to a 30-min cadence once a pause boundary is within this window, so a start
# scheduled nearer than this could be missed. Keep this == monitor.WATCHDOG_CHECK_SECONDS.
SCHEDULED_PAUSE_MIN_LEAD = timedelta(hours=8)

import logging
log = logging.getLogger(__name__)

CHECK_REQUESTED_FLAG = 'check_requested'


# ── Sites (locations) ───────────────────────────────────────────────

def _current_site() -> str:
    """Site the request targets, from ?site= (or JSON body), validated against
    configured sites; defaults to the default site."""
    site = request.args.get('site')
    if not site and request.is_json:
        site = (request.get_json(silent=True) or {}).get('site')
    keys = current_app.config['SITE_KEYS']
    return site if site in keys else current_app.config['DEFAULT_SITE']


def _site_nodes(site: str):
    """(devices, switches) belonging to a site, from the live app config."""
    devices = [d for d in current_app.config['DEVICES'] if d.get('site', current_app.config['DEFAULT_SITE']) == site]
    switches = [s for s in current_app.config['SWITCHES'] if s.get('site', current_app.config['DEFAULT_SITE']) == site]
    return devices, switches


def _check_flag(site: str) -> str:
    return f'{CHECK_REQUESTED_FLAG}:{site}'


def _slugify(name: str) -> str:
    """A URL/env-safe site key from a display name: lowercase, non-alphanumerics
    → dashes, trimmed."""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def _reload_sites(app):
    """Reload the sites list from the DB into app config (after adding one)."""
    sites = db.get_sites() or [{'key': 'location-a', 'name': 'Location A'}]
    app.config['SITES'] = sites
    app.config['DEFAULT_SITE'] = sites[0]['key']
    app.config['SITE_KEYS'] = {s['key'] for s in sites}
    app.config['SETTINGS']['sites'] = sites


# Throttle the soft-delete sweep: it's called from the 30s topology poll and the
# ~2min agent report, but the 24h deletion needs no better than minute precision.
# Querying at most every few minutes keeps these hot paths from waking the DB.
_PURGE_INTERVAL_SECONDS = 300
_last_purge_check = 0.0


def _purge_due_deletions(app) -> list[dict]:
    """Execute any scheduled location soft-deletes whose grace window has elapsed.
    Called opportunistically from frequent endpoints (agent report, topology) so
    the 24h deletion lands without a dedicated scheduler, but throttled so it isn't
    a DB query on every request. Reloads config only when something was removed."""
    # Skip entirely when no soft-delete is pending (the common case) — an in-memory
    # flag, so this hot path doesn't query the DB at all and Neon can stay suspended.
    if not db.has_pending_deletions():
        return []
    global _last_purge_check
    now = time.time()
    if now - _last_purge_check < _PURGE_INTERVAL_SECONDS:
        return []
    _last_purge_check = now
    removed = db.purge_due_site_deletions()
    if removed:
        _reload_config(app)
        _reload_sites(app)
        log.info("Purged %d scheduled location deletion(s): %s",
                 len(removed), ', '.join(s['key'] for s in removed))
    return removed


def _site_pending_delete(site: str) -> bool:
    return any(s['key'] == site and s.get('pending_delete_at')
               for s in current_app.config['SITES'])


def _monitoring_stale_minutes(site: str):
    """Minutes since this site's last agent report if it exceeds the watchdog
    stale threshold (so the UI can warn that statuses are frozen), else None.
    None also when the site has never reported (no baseline — same as watchdog)."""
    last = db.get_last_report(site)
    if not last:
        return None
    try:
        mins = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() / 60
    except ValueError:
        return None
    return int(mins) if mins > db.get_setting('watchdog_stale_minutes') else None


def _agent_token_for(site: str) -> str | None:
    """Per-site agent token from env AGENT_TOKEN_<KEY> (key upper-cased, dashes →
    underscores). The default site also accepts the plain AGENT_TOKEN so the
    original Location A Pi keeps working unchanged."""
    token = os.environ.get('AGENT_TOKEN_' + site.upper().replace('-', '_'))
    if not token and site == current_app.config['DEFAULT_SITE']:
        token = os.environ.get('AGENT_TOKEN')
    return token


def _agent_authorized(site: str) -> bool:
    """Agent endpoints require X-Agent-Token to match that site's token OR the
    master AGENT_TOKEN (which works for any site — used by a single agent that
    covers all locations). If no token is configured, allow (dev) but warn."""
    header = request.headers.get('X-Agent-Token')
    master = os.environ.get('AGENT_TOKEN')
    if master and header == master:
        return True
    expected = _agent_token_for(site)
    if not expected:
        log.warning("No agent token configured for site '%s' — endpoint is unauthenticated", site)
        return True
    return header == expected


def _agent_authorized_all() -> bool:
    """The all-locations agent (?site=all) authenticates with the master
    AGENT_TOKEN. Unset → allow (dev) but warn."""
    master = os.environ.get('AGENT_TOKEN')
    if not master:
        log.warning("AGENT_TOKEN (master) not set — all-locations agent endpoint is unauthenticated")
        return True
    return request.headers.get('X-Agent-Token') == master


# ── Layout helpers ──────────────────────────────────────────────────

def _area_key(sw: dict) -> str:
    """Group switches by physical area. Use the location with a trailing
    'Switch' stripped (e.g. 'Dining Switch' -> 'Dining'), falling back to the
    name. This is only used to seed a sensible default layout — the user nudges
    clusters into real geography afterwards (saved client-side)."""
    loc = (sw.get('location') or '').strip()
    if loc:
        return re.sub(r'\s*switch$', '', loc, flags=re.IGNORECASE).strip() or loc
    return sw.get('name', '')


def _switch_slots(switches: list[dict]) -> dict:
    """Map each switch IP to a grid slot index, ordered by area so related
    switches start near each other."""
    ordered = sorted(
        range(len(switches)),
        key=lambda i: (_area_key(switches[i]).lower(), switches[i].get('name', '')),
    )
    return {switches[orig]['ip']: slot for slot, orig in enumerate(ordered)}


def _switch_default_pos(index: int) -> dict:
    col = index % SWITCH_COLS
    row = index // SWITCH_COLS
    return {'x': SWITCH_X_ORIGIN + col * SWITCH_X_GAP,
            'y': SWITCH_Y_ORIGIN + row * SWITCH_Y_GAP}


def _ap_default_pos(index: int, count: int, sw_pos: dict) -> dict:
    radius = max(75, min(130, 40 + count * 7))
    angle = (2 * math.pi * index / max(count, 1)) - math.pi / 2
    return {'x': sw_pos['x'] + radius * math.cos(angle),
            'y': sw_pos['y'] + radius * math.sin(angle)}


# ── Response builders ───────────────────────────────────────────────

def _yaml_pos(entry: dict) -> dict | None:
    """A hand-tuned x/y baked into devices.yaml, if present. Used as the
    checked-in layout baseline — overridden by a user-dragged DB position,
    falls back to the computed default when absent."""
    if entry.get('x') is not None and entry.get('y') is not None:
        return {'x': entry['x'], 'y': entry['y']}
    return None


def _switch_response(sw: dict, state: dict | None, index: int, positions: dict,
                     geo_positions: dict | None = None) -> dict:
    pos = positions.get(sw['ip']) or _yaml_pos(sw) or _switch_default_pos(index)
    gpos = (geo_positions or {}).get(sw['ip'])
    enabled = sw.get('enabled', True)
    status = (state['current_status'] if state else 'unknown') if enabled else 'disabled'
    return {
        'name': sw['name'], 'ip': sw['ip'],
        'location': sw.get('location', ''), 'type': 'switch',
        'hostname': sw.get('hostname'),
        'intermapper_url': sw.get('intermapper_url'),
        'uplink': sw.get('uplink'), 'enabled': enabled,
        'notify': sw.get('notify', True),
        'status': status,
        'last_check': state['last_check'] if state else None,
        # When the device entered its CURRENT status (durably persisted on transition,
        # so it survives restarts and stays right even while an agent is silent) — the
        # panel shows "up/down since" from this instead of the flaky last_check.
        'since': state['last_change'] if state else None,
        'x': pos['x'], 'y': pos['y'],
        # Where the device physically is, for locations with an aerial basemap. The
        # browser projects this to map pixels; null means "not placed yet".
        'lat': gpos['lat'] if gpos else None,
        'lng': gpos['lng'] if gpos else None,
    }


def _device_response(device: dict, state: dict | None,
                     index: int, count: int, sw_pos: dict, positions: dict,
                     geo_positions: dict | None = None) -> dict:
    pos = positions.get(device['ip']) or _yaml_pos(device) or _ap_default_pos(index, count, sw_pos)
    gpos = (geo_positions or {}).get(device['ip'])
    enabled = device.get('enabled', True)
    status = (state['current_status'] if state else 'unknown') if enabled else 'disabled'
    return {
        'name': device['name'], 'ip': device['ip'],
        'location': device.get('location', ''), 'type': device.get('kind', 'ap'),
        'hostname': device.get('hostname'),
        'intermapper_url': device.get('intermapper_url'),
        'switch_name': device.get('switch'), 'enabled': enabled,
        'notify': device.get('notify', True),
        'status': status,
        'last_check': state['last_check'] if state else None,
        'since': state['last_change'] if state else None,
        'x': pos['x'], 'y': pos['y'],
        'lat': gpos['lat'] if gpos else None,
        'lng': gpos['lng'] if gpos else None,
    }


# ── Aerial basemap ──────────────────────────────────────────────────
# Config comes from config/basemaps.yaml (per site); the tile manifest — which
# carries the georeferencing — is read from the generated tile directory and
# cached in memory, so serving basemap config never touches disk twice or the DB
# more than once per site. A site with no entry simply has no imagery.

_BASEMAP_VIEW_FLAG = 'basemap_view'      # app_flags key prefix for an admin's custom area
_OPEN_VIEW_FLAG = 'default_open_view'    # app_flags key prefix for the saved opening view
_manifest_cache: dict[str, dict | None] = {}


def _basemap_cfg(site: str) -> dict | None:
    """The raw basemaps.yaml entry for a site, or None."""
    cfg = (current_app.config.get('BASEMAPS') or {}).get(site)
    return cfg if isinstance(cfg, dict) and cfg.get('tiles') else None


def _basemap_manifest(site: str) -> dict | None:
    """The tile manifest for a site's basemap (cached). None if the site has no
    basemap configured or the tiles haven't been generated — in which case the map
    silently falls back to the plain grid rather than erroring."""
    if site in _manifest_cache:
        return _manifest_cache[site]
    result = None
    cfg = _basemap_cfg(site)
    if cfg:
        path = os.path.join(current_app.static_folder,
                            cfg['tiles'].strip('/'), 'manifest.json')
        try:
            with open(path) as f:
                result = json.load(f)
        except FileNotFoundError:
            log.warning(
                "Basemap configured for site '%s' but %s is missing — run "
                "maps/build_tiles.py. Falling back to the grid map.", site, path)
        except Exception:
            log.exception("Could not read basemap manifest %s", path)
    _manifest_cache[site] = result
    return result


def _basemap_view(site: str, manifest: dict) -> tuple[dict, bool]:
    """(view window, is_custom) for a site: an admin's saved area if valid, else the
    basemaps.yaml default, else the whole image."""
    try:
        saved = db.get_flag(f'{_BASEMAP_VIEW_FLAG}:{site}')
        if saved:
            view = json.loads(saved)
            if geo.valid_view(view):
                return {k: float(view[k]) for k in ('south', 'west', 'north', 'east')}, True
    except Exception:
        log.exception("Ignoring unreadable custom map area for site '%s'", site)

    cfg = _basemap_cfg(site) or {}
    if geo.valid_view(cfg.get('view')):
        v = cfg['view']
        return {k: float(v[k]) for k in ('south', 'west', 'north', 'east')}, False

    c = manifest['georef']['image_corners_latlng']
    return {'south': c['bottom_left']['lat'], 'west': c['top_left']['lng'],
            'north': c['top_left']['lat'], 'east': c['top_right']['lng']}, False


def _basemap_payload(site: str) -> dict:
    """What the frontend needs to draw the imagery, or {} for a site without it."""
    manifest = _basemap_manifest(site)
    if not manifest:
        return {}
    cfg = _basemap_cfg(site)
    view, is_custom = _basemap_view(site, manifest)
    return {
        'tile_base': '/static/' + cfg['tiles'].strip('/'),
        'tile_size': manifest.get('tile_size', 512),
        'tile_ext': manifest.get('tile_ext', 'jpg'),
        'levels': manifest['levels'],
        'georef': manifest['georef'],
        'attribution': manifest.get('attribution', ''),
        'view': view,
        'view_is_custom': is_custom,
        'default_view': _basemap_default_view(site, manifest),
        'open_view': _basemap_open_view(site, 'desktop'),
        'open_view_mobile': _basemap_open_view(site, 'mobile'),
    }


def seed_geo_positions(app) -> None:
    """One-time per site: give every device a starting lat/lng on the aerial photo by
    fitting its current logical layout into the map area.

    Without this, a location that just gained a basemap would show 100+ unplaced
    devices — either stacked in one pile or missing. Instead the familiar layout
    appears over the property, shrunk to fit, and an admin drags each device onto its
    real building from there. The logical x/y layout is untouched (it stays in
    device_positions), so toggling the photo off returns to exactly the old map.

    Guarded by an app_flags flag so it runs once per site — a later reseed would
    undo real placement work. Devices added afterwards are placed by the frontend
    near their parent switch.
    """
    for site in app.config['SITES']:
        key = site['key']
        with app.app_context():
            if not _basemap_manifest(key):
                continue
            flag = f'geo_seeded:{key}'
            try:
                if db.get_flag(flag):
                    continue
            except Exception:
                log.exception("Could not read %s — skipping geo seed", flag)
                continue

            manifest = _basemap_manifest(key)
            view, _ = _basemap_view(key, manifest)
            devices, switches = _site_nodes(key)
            if not devices and not switches:
                continue

            # Resolve each node's effective logical position through the same
            # three-tier fallback the topology endpoint uses, so the seeded shape
            # matches what the map actually shows today.
            all_ips = [s['ip'] for s in switches] + [d['ip'] for d in devices]
            saved = db.get_positions(all_ips)
            slots = _switch_slots(switches)
            logical: dict[str, dict] = {}
            sw_pos_by_name: dict[str, dict] = {}
            for sw in switches:
                p = saved.get(sw['ip']) or _yaml_pos(sw) or _switch_default_pos(slots[sw['ip']])
                logical[sw['ip']] = p
                sw_pos_by_name[sw['name']] = p

            by_switch: dict[str, list] = {}
            for d in devices:
                by_switch.setdefault(d.get('switch', '__none__'), []).append(d)
            for sw_name, members in by_switch.items():
                sw_pos = sw_pos_by_name.get(sw_name, {'x': 600, 'y': 400})
                for i, d in enumerate(members):
                    logical[d['ip']] = (saved.get(d['ip']) or _yaml_pos(d)
                                        or _ap_default_pos(i, len(members), sw_pos))

            # Only seed devices that don't already have a geo position, but fit the
            # transform to the WHOLE layout so a partial seed lands consistently
            # with whatever was placed before.
            placed = geo.fit_layout_to_view(logical, manifest['georef'], view)
            seeded = 0
            for ip, (lat, lng) in placed.items():
                if db.has_geo_position(ip):
                    continue
                try:
                    db.set_geo_position(ip, lat, lng)
                    seeded += 1
                except Exception:
                    log.exception("Could not seed geo position for %s", ip)
                    return   # DB unwritable; leave the flag unset and retry next boot
            try:
                db.set_flag(flag, '1')
            except Exception:
                log.exception("Seeded %d geo positions for '%s' but could not set %s "
                              "— may reseed on restart", seeded, key, flag)
            if seeded:
                log.info("Seeded %d device position(s) onto the '%s' aerial map "
                         "from its existing layout.", seeded, key)


def _basemap_default_view(site: str, manifest: dict) -> dict:
    """The config-file view, ignoring any admin override — so the UI can offer
    "reset to default" without a second round trip."""
    cfg = _basemap_cfg(site) or {}
    if geo.valid_view(cfg.get('view')):
        v = cfg['view']
        return {k: float(v[k]) for k in ('south', 'west', 'north', 'east')}
    c = manifest['georef']['image_corners_latlng']
    return {'south': c['bottom_left']['lat'], 'west': c['top_left']['lng'],
            'north': c['top_left']['lat'], 'east': c['top_right']['lng']}


def _open_view_flag(site: str, layout: str) -> str:
    """app_flags key for the saved opening view, one per layout (a wide desktop view
    frames a tall, narrow phone poorly, so phones get their own)."""
    suffix = '_mobile' if layout == 'mobile' else ''
    return f'{_OPEN_VIEW_FLAG}{suffix}:{site}'


def _basemap_open_view(site: str, layout: str = 'desktop') -> dict | None:
    """The admin-saved default opening view (geographic center + zoom scale) that
    everyone on this `layout` gets on their first map load, or None. Independent of the
    map area / pan clamp — it only decides where the map OPENS; users pan/zoom freely
    after."""
    try:
        saved = db.get_flag(_open_view_flag(site, layout))
        if saved:
            v = json.loads(saved)
            lat, lng, k = float(v['lat']), float(v['lng']), float(v['k'])
            if -90 <= lat <= 90 and -180 <= lng <= 180 and k > 0:
                out = {'lat': lat, 'lng': lng, 'k': k}
                box = v.get('box')
                if isinstance(box, dict) and all(s in box for s in ('south', 'west', 'north', 'east')):
                    out['box'] = {s: float(box[s]) for s in ('south', 'west', 'north', 'east')}
                return out
    except Exception:
        log.exception("Ignoring unreadable default view for site '%s'", site)
    return None


# ── Config reload ───────────────────────────────────────────────────

def _reload_config(app):
    """Reload the device/switch list from the DB (the `nodes` table is the
    source of truth) into app config after an add."""
    nodes = db.get_nodes()
    devices = nodes['devices']
    switches = nodes['switches']

    app.config['DEVICES'] = devices
    app.config['SWITCHES'] = switches

    # The in-process scheduler only runs on-LAN (ENABLE_LOCAL_PING=1); restart
    # it so it picks up the new target. On Heroku it's disabled, and the remote
    # agent sees the new device on its next /api/agent/targets poll (which reads
    # app.config['DEVICES'] live) — so no restart is needed or wanted there.
    if os.environ.get('ENABLE_LOCAL_PING') == '1':
        monitor.stop_scheduler()
        monitor.start_scheduler(devices, switches, app.config['SETTINGS'])


# ── Auth ────────────────────────────────────────────────────────────
# A single shared password (the APP_PASSWORD config var) gates the whole UI
# and its JSON API. Login state lives in the signed Flask session cookie.
# The remote ping agent is exempt — it authenticates separately with
# X-Agent-Token (see _agent_authorized), so monitoring keeps working even
# when no human is logged in.

# Endpoints reachable without a login session. Agent endpoints carry their
# own token auth; static files and the login page must be open or you can't
# render/submit the login form.
_PUBLIC_ENDPOINTS = {
    'main.login', 'main.logout', 'main.logged_out', 'main.authorize', 'static',
    'main.agent_targets', 'main.agent_report',
}


def _auth_required() -> bool:
    """True when a login gate is configured — Okta SSO (preferred) or the shared
    APP_PASSWORD. If neither is set the app is open (dev convenience), mirroring
    how AGENT_TOKEN behaves."""
    return okta_enabled() or bool(os.environ.get('APP_PASSWORD'))


def _admin_group() -> str:
    """directory group whose members get admin (edit) rights. Tolerant of a value
    that was pasted with surrounding quotes or stray whitespace."""
    return os.environ.get('ADMIN_GROUP', 'Network Monitor Admins').strip().strip('"').strip("'").strip()


# U-M's Okta delivers directory group membership in the eduPerson
# `edumember_ismemberof` claim (a list of group names), NOT the generic `groups`
# claim. We read that first and fall back to `groups` in case a deploy configures
# a plain groups claim instead.
_GROUP_CLAIMS = ('edumember_ismemberof', 'groups')


def _decode_jwt_payload(jwt_str: str) -> dict:
    """Base64url-decode a JWT's payload segment to a dict. Used to read the ID
    token's claims directly — Authlib has already validated its signature/nonce/exp
    during the code exchange, so reading the payload for group info is safe, and it
    avoids depending on which claims Authlib copies into `token['userinfo']` or on
    the userinfo endpoint (which on U-M's default server omits groups)."""
    import base64
    import json
    try:
        payload_b64 = jwt_str.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)   # restore padding
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return {}


def _group_list(claims: dict) -> list:
    """The user's group names from whichever group claim is present."""
    for key in _GROUP_CLAIMS:
        groups = claims.get(key)
        if not groups:
            continue
        return [groups] if isinstance(groups, str) else list(groups)
    return []


def _is_admin_from_claims(claims: dict) -> bool:
    """Test admin-group membership from the Okta claims. Reads the U-M
    `edumember_ismemberof` group list (falling back to `groups`), then — as a
    safety net for a differently-named group claim — scans every claim value for
    the admin group name. Match is whitespace-tolerant and case-insensitive."""
    want = _admin_group().casefold()
    if any((g or '').strip().casefold() == want for g in _group_list(claims)):
        return True
    # Deep scan: any claim whose value (string or list) contains the group name.
    for v in claims.values():
        vals = v if isinstance(v, (list, tuple)) else [v]
        if any(isinstance(x, str) and x.strip().casefold() == want for x in vals):
            return True
    return False


def is_admin() -> bool:
    """Whether the current request is from an admin (edit-capable) user. In open
    (no-auth) mode everyone is an admin — matching how the app is fully usable
    without a login gate configured."""
    if not _auth_required():
        return True
    return bool(session.get('is_admin'))


# Endpoints a signed-in VIEWER (authed but not admin) may reach. Everything else
# that mutates state — plus the user-activity audit trail — is admin-only.
#   • All safe GET/HEAD reads are allowed EXCEPT those in _VIEWER_BLOCKED.
#   • _VIEWER_ALLOWED_WRITES are read-only "exports" that happen to be POSTs.
_VIEWER_BLOCKED = {'main.user_activity'}          # the User Activity log — admin-only
_VIEWER_ALLOWED_WRITES = {'main.export_xlsx'}     # building an xlsx from posted rows


@bp.before_request
def _require_login():
    if not _auth_required():
        return  # no password configured → app is open (logged at startup)
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return
    if session.get('authed'):
        return
    # API calls get a 401 (so the frontend can redirect); page loads get the
    # login form.
    if request.path.startswith('/api/'):
        return jsonify({'error': 'authentication required'}), 401
    return redirect(url_for('main.login', next=request.path))


@bp.before_request
def _require_admin():
    """Second gate (runs after _require_login): viewers get read-only access. Blocks
    every mutating request and the user-activity trail for non-admins, so the
    everyone-else experience is genuinely view-only even if the frontend is bypassed."""
    if not _auth_required():
        return                                   # open mode → everyone is admin
    ep = request.endpoint
    if ep in _PUBLIC_ENDPOINTS or ep is None:
        return
    if session.get('is_admin'):
        return                                   # admins: unrestricted
    # Viewer: allow safe reads (except the blocked ones) + whitelisted exports.
    if request.method in _CSRF_SAFE_METHODS and ep not in _VIEWER_BLOCKED:
        return
    if ep in _VIEWER_ALLOWED_WRITES:
        return
    if request.path.startswith('/api/'):
        return jsonify({'error': 'admin access required'}), 403
    return redirect(url_for('main.index'))


# CSRF protection: "required custom header" pattern. Browsers refuse to let a
# cross-site page attach custom headers to a cross-origin request without a
# CORS preflight, and this app doesn't opt into CORS — so only same-origin
# JS (our own map.js, via a wrapped fetch) can set X-Requested-With. A forged
# form/img/fetch from another site therefore can't include it. We only need
# to guard cookie-authenticated, state-changing requests: GET/HEAD/OPTIONS
# are safe by convention, and anything without a session cookie isn't a CSRF
# target in the first place (the ping agent uses its own X-Agent-Token with
# no cookie, and the login POST happens before a session exists).
_CSRF_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


@bp.before_request
def _csrf_protect():
    # Only cookie-authenticated, state-changing browser requests are CSRF targets.
    if request.method in _CSRF_SAFE_METHODS:
        return
    if not session.get('authed'):
        # No session cookie in play → not a CSRF vector. Covers the token-authed
        # agent endpoints, the pre-auth login POST, and open (no-auth) mode.
        return
    # Same-origin fetch/XHR from our own JS sets this header; a cross-site page
    # cannot add a custom header to a request aimed at our origin, so its absence
    # on an authenticated mutating request means a forged request → reject.
    if request.headers.get('X-Requested-With'):
        return
    return jsonify({'error': 'CSRF check failed — missing X-Requested-With header'}), 403


def _resolve_ref(ref: str) -> str:
    """Map a device REFERENCE to its current IP. Accepts a Device ID (the stable `name`
    key — preferred) or an IP (legacy). A Device ID is resolved to the node's current
    IP; anything else is returned unchanged, so existing IP-keyed calls behave exactly
    as before. This is the seam that lets the interface reference devices by their
    stable Device ID while storage is still IP-keyed (migrated in a later phase).

    Cheap in the common case (an in-memory scan of the ~110 config nodes, no DB); only
    a decommissioned Device ID or an unknown ref falls through to a DB lookup so that
    restore / deleted-device routes resolve too."""
    nodes = (*current_app.config['DEVICES'], *current_app.config['SWITCHES'])
    for n in nodes:
        if n['name'] == ref:
            return n['ip']
    if any(n['ip'] == ref for n in nodes):
        return ref                          # already a live IP → unchanged
    for n in db.get_decommissioned_nodes():
        if n['name'] == ref:
            return n['ip']
    return ref                              # unknown → unchanged (handled downstream as today)


@bp.before_request
def _resolve_device_ref():
    """Resolve the `ip` path segment of every /api/devices/<ip>/… route from a Device ID
    (preferred) or IP to the current IP, so the whole per-device API accepts the stable
    Device ID without each view needing its own lookup. Runs after auth; no-op for routes
    without an `ip` view-arg."""
    va = request.view_args or {}
    if 'ip' in va and isinstance(va['ip'], str):
        va['ip'] = _resolve_ref(va['ip'])


# ── Audit trail ──────────────────────────────────────────────────────
# One log row per successful, user-initiated mutating request. Excludes
# noisy/read-only/non-human endpoints: map-drag position saves, xlsx export,
# the token-authed agent endpoints, and auth itself.
_AUDIT_EXCLUDE_ENDPOINTS = {
    'main.set_device_position', 'main.set_device_geo_position',   # map-drag saves
    'main.export_xlsx', 'main.agent_report',
    'main.agent_targets', 'main.login', 'main.logout', 'main.authorize',
    'main.event_device_log',   # read-only (POST only to carry a device list)
    'main.log_edit_unlock',    # password check, not a data change
}


def _ap_label(node, fallback=None):
    """Friendly device label for audit lines, with an 'AP' suffix for access points
    (matching the activity log's '<name> AP' convention). Switches and 'other'
    devices get no suffix."""
    label = (node.get('location') or '').strip() or node.get('name') or fallback
    return f"{label} AP" if node.get('kind', 'ap') == 'ap' else label


def _device_label(ip):
    """Friendly device Name (location → Device ID → IP, + 'AP' for access points)
    for audit lines. Prefers a name the route stashed in g (for deletes/edits where
    the node is gone/changed by the time the after_request hook builds the summary)."""
    name = g.get('audit_device_name')
    if name:
        return name
    node = next((d for d in current_app.config['DEVICES'] if d['ip'] == ip), None) \
        or next((s for s in current_app.config['SWITCHES'] if s['ip'] == ip), None)
    if node:
        return _ap_label(node, ip)
    return ip


def _audit_summary(endpoint, view_args, body) -> str:
    view_args = view_args or {}
    if endpoint == 'main.add_device':
        label = body.get('location') or body.get('name', '?')
        if (body.get('kind') or 'ap') == 'ap':
            label = f"{label} AP"
        return f"Added device {label}"
    if endpoint == 'main.add_switch':
        return f"Added switch {body.get('location') or body.get('name', '?')}"
    if endpoint == 'main.update_device':
        return f"Edited device {_device_label(view_args.get('ip'))}"
    if endpoint == 'main.delete_device':
        return f"Deleted device {_device_label(view_args.get('ip'))}"
    if endpoint == 'main.restore_device':
        return f"Restored device {_device_label(view_args.get('ip'))}"
    if endpoint == 'main.set_device_enabled':
        # 'Resumed monitoring' / 'Paused monitoring' / 'Paused notifications'.
        if body.get('enabled', True):
            action = 'Resumed monitoring'
        else:
            action = 'Paused notifications' if g.get('audit_pause_mode') == 'notifications' \
                else 'Paused monitoring'
        suffix = ' (+ connected devices)' if body.get('include_children') else ''
        override_note = ''
        if body.get('enabled', True):
            overrode = g.get('audit_resume_overrode')
            if overrode:
                if len(overrode) == 1:
                    override_note = f" (overrode scheduled pause '{overrode[0]}')"
                else:
                    names = ', '.join(f"'{n}'" for n in overrode)
                    override_note = f" (overrode scheduled pauses {names})"
        return f"{action} for {_device_label(view_args.get('ip'))}{suffix}{override_note}"
    if endpoint == 'main.device_note':
        return f"Edited note on {_device_label(view_args.get('ip'))}"
    if endpoint == 'main.add_site':
        return f"Added location {body.get('name', '?')}"
    if endpoint == 'main.rename_site':
        return f"Renamed location {view_args.get('key')}"
    if endpoint == 'main.delete_site':
        return f"Requested deletion of location {view_args.get('key')}"
    if endpoint == 'main.undo_site_deletion':
        return f"Undid deletion of location {view_args.get('key')}"
    if endpoint == 'main.scheduled_pauses' and request.method == 'POST':
        return f"Scheduled a pause: {body.get('name', '?')}"
    if endpoint == 'main.cancel_scheduled_pause':
        name = g.get('audit_pause_name')   # stashed by the route before deletion
        return f"Canceled scheduled pause {name}" if name else \
            f"Canceled scheduled pause #{view_args.get('pause_id')}"
    if endpoint == 'main.delete_pause_log':
        return "Deleted a monitoring-pause log entry"
    if endpoint == 'main.check_now':
        return "Requested an immediate check"
    if endpoint == 'main.app_settings':
        return "Changed app settings"
    if endpoint == 'main.set_basemap_view':
        return "Reset the map area to its default" if g.get('audit_basemap_reset') \
            else "Changed the map area"
    if endpoint == 'main.set_basemap_open_view':
        which = 'mobile' if g.get('audit_open_view_layout') == 'mobile' else 'desktop'
        return f"Reset the {which} default map view" if g.get('audit_open_view_reset') \
            else f"Set the {which} default map view"
    if endpoint == 'main.events' and request.method == 'POST':
        return f"Marked a {_event_cat_label(g.get('audit_event_category'))} event"
    if endpoint == 'main.schedule_event':
        return f"Scheduled a {_event_cat_label(g.get('audit_event_category'))} event"
    if endpoint == 'main.event_detail':
        verb = 'Removed' if request.method == 'DELETE' else 'Edited'
        return f"{verb} an event"
    if endpoint == 'main.event_devices':
        return "Changed an event's devices"
    if endpoint == 'main.resolve_event_device':
        verb = 'Reopened' if g.get('audit_resolve_reopen') else 'Resolved'
        return f"{verb} {_device_label(None)} in an event"
    if endpoint == 'main.combine_events':
        return "Combined events"
    if endpoint == 'main.add_maintenance':
        return f"Marked an outage as a {_event_cat_label(g.get('audit_event_category'))} event on {_device_label(view_args.get('ip'))}"
    if endpoint == 'main.update_maintenance':
        return f"Edited an event on {_device_label(None)}"
    if endpoint == 'main.delete_maintenance':
        return f"Removed an event mark on {_device_label(None)}"
    return f"{request.method} {request.path}"


def _event_cat_label(cat) -> str:
    return {'maintenance': 'maintenance', 'lightning': 'lightning',
            'power_outage': 'power outage', 'scheduled_downtime': 'scheduled downtime',
            'other': 'other'}.get(cat, 'maintenance')


@bp.before_request
def _stash_audit_site():
    """Resolve which location a mutating request belongs to, *before* the route
    runs (so it still works after a delete). For device-scoped actions this is the
    target device's own site — authoritative and independent of whether the client
    sent ?site=. Location actions belong to the site key they target. Everything
    else falls back to the viewed site (_current_site) in _audit_log."""
    if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return
    va = request.view_args or {}
    ip = va.get('ip')
    if ip:
        node = next((d for d in current_app.config['DEVICES'] if d['ip'] == ip), None) \
            or next((s for s in current_app.config['SWITCHES'] if s['ip'] == ip), None)
        if node:
            g.audit_site = node.get('site', current_app.config['DEFAULT_SITE'])
    elif va.get('key'):   # site rename/delete/undo act on that location
        g.audit_site = va['key']


@bp.after_request
def _audit_log(response):
    try:
        if request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return response
        if response.status_code >= 400:
            return response
        if not session.get('authed'):
            return response
        if request.endpoint in _AUDIT_EXCLUDE_ENDPOINTS:
            return response

        actor = session.get('user') or '(shared password)'
        body = request.get_json(silent=True) or {}
        summary = _audit_summary(request.endpoint, request.view_args, body)
        site = g.get('audit_site') or _current_site()
        db.log_user_activity(actor, request.method, request.endpoint,
                              request.path, summary, response.status_code, site)
    except Exception:
        log.warning("Failed to write audit log entry", exc_info=True)
    return response


def _safe_next(dest: str) -> str:
    """Only allow same-app relative redirects (blocks open-redirect abuse)."""
    return dest if dest.startswith('/') and not dest.startswith('//') else ''


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if not _auth_required():
        return redirect(url_for('main.index'))
    if session.get('authed'):
        return redirect(url_for('main.index'))

    # Okta SSO: bounce straight to the identity provider.
    if okta_enabled():
        session['next'] = _safe_next(request.args.get('next', ''))
        redirect_uri = os.environ.get('OKTA_REDIRECT_URI') or url_for('main.authorize', _external=True)
        return oauth.okta.authorize_redirect(redirect_uri)

    # Shared-password fallback.
    error = None
    if request.method == 'POST':
        supplied = request.form.get('password', '')
        expected = os.environ.get('APP_PASSWORD', '')
        # Constant-time compare to avoid leaking length/contents via timing.
        if hmac.compare_digest(supplied, expected):
            session['authed'] = True
            # The shared password grants full (admin) access — there's no group
            # info in password mode, so it can't distinguish viewers.
            session['is_admin'] = True
            session.permanent = True
            return redirect(_safe_next(request.args.get('next', '')) or url_for('main.index'))
        error = 'Incorrect password.'

    return render_template('login.html', error=error, okta=False)


@bp.route('/auth/callback')
def authorize():
    """Okta OIDC redirect target: exchange the code and establish the login
    session."""
    if not okta_enabled():
        return redirect(url_for('main.index'))
    try:
        token = oauth.okta.authorize_access_token()   # validates id_token + nonce
    except Exception as e:
        log.warning("Okta token exchange failed: %s: %s", type(e).__name__, e)
        # Surface the underlying reason on the page when AUTH_DEBUG=1 (temporary
        # diagnostics — unset it once sign-in works).
        detail = f' [{type(e).__name__}: {e}]' if os.environ.get('AUTH_DEBUG') == '1' else ''
        return render_template('login.html', okta=True,
                               error='Sign-in failed. Please try again.' + detail), 400

    # Collect claims from every source Okta might carry the group list in, so admin
    # detection works regardless of where the app is configured to release it:
    #   1. token['userinfo'] — what Authlib parsed out of the exchange,
    #   2. the ID token payload decoded directly (Authlib already validated it),
    #   3. the /userinfo endpoint (some U-M setups only release groups here).
    # setdefault keeps the first (validated) source's value for any shared key.
    claims = dict(token.get('userinfo') or {})
    id_claims = _decode_jwt_payload(token.get('id_token') or '')
    for k, v in id_claims.items():
        claims.setdefault(k, v)
    if not _group_list(claims):
        try:
            info = oauth.okta.userinfo(token=token) or {}
            for k, v in info.items():
                claims.setdefault(k, v)
        except Exception as e:
            log.warning("Okta userinfo fetch failed (groups fallback): %s: %s",
                        type(e).__name__, e)

    # Identify users by their uniqname (preferred_username), falling back to the
    # local part of the email (jdoe@example.com → jdoe), then 'unknown'.
    who = claims.get('preferred_username') \
        or (claims.get('email') or '').split('@')[0] \
        or 'unknown'

    # Access is gated by Okta app assignment — only users assigned to the Okta
    # app can complete a login, so any successful login here is authorized to VIEW.
    # Admin (edit) rights are additionally gated on membership in the admin
    # directory group, delivered via the ID token's `edumember_ismemberof` claim
    # (requires the `edumember` scope). Everyone else gets the read-only viewer view.
    admin = _is_admin_from_claims(claims)
    if os.environ.get('AUTH_DEBUG') == '1':
        # Dump the claim keys received (and the raw id_token keys) so a missing group
        # claim is diagnosable from the logs without exposing claim values.
        log.info("Okta claims for %s: keys=%s | id_token_keys=%s | groups=%s | "
                 "admin_group=%r | admin=%s",
                 who, sorted(claims.keys()), sorted(id_claims.keys()),
                 _group_list(claims), _admin_group(), admin)
    session['authed'] = True
    session['user'] = who
    session['is_admin'] = admin
    session['id_token'] = token.get('id_token')   # for RP-initiated logout
    session.permanent = True
    log.info("SSO login: %s (%s)", who, 'admin' if admin else 'viewer')
    return redirect(session.pop('next', '') or url_for('main.index'))


@bp.route('/logout')
def logout():
    id_token = session.get('id_token')
    session.clear()
    # RP-initiated logout: end the Okta session too, then return to our own
    # "signed out" page. Falls back to the local login page.
    if okta_enabled():
        try:
            end = oauth.okta.load_server_metadata().get('end_session_endpoint')
        except Exception:
            end = None
        if end:
            params = {}
            # Land back on our own confirmation page. OKTA_LOGOUT_REDIRECT_URI can
            # override where Okta returns to; otherwise default to /logged-out.
            # NOTE: whichever URL is used must be registered in the Okta app's
            # "Sign-out redirect URIs".
            params['post_logout_redirect_uri'] = (
                os.environ.get('OKTA_LOGOUT_REDIRECT_URI')
                or url_for('main.logged_out', _external=True)
            )
            if id_token:
                params['id_token_hint'] = id_token
            return redirect(end + '?' + urlencode(params))
    return redirect(url_for('main.logged_out'))


@bp.route('/logged-out')
def logged_out():
    """Post-logout confirmation page with a "log back in" button. Public so a
    signed-out user can actually see it."""
    return render_template('logged_out.html')


# ── Routes ──────────────────────────────────────────────────────────

def _demo_mode():
    """Demo mode (DEMO_MODE=1) abstracts device/location identifiers in the UI for a
    public-shareable video. It's display-only — the real IPs stay the routing keys, so
    nothing about auth or the API changes."""
    return os.environ.get('DEMO_MODE') == '1'


def _demo_data():
    """Demo-DATA build (DEMO_DATA=1): this is the localhost demo with entirely fake,
    seeded data — the opposite of DEMO_MODE. Nothing needs hiding, so everything shows;
    it only drives the "this is a demo" disclaimer popup, the one-time fake-data seed
    (see app/demo_seed.py), and suppressing the agent-silence banner (there's no agent)."""
    return os.environ.get('DEMO_DATA') == '1'


@bp.route('/')
def index():
    sites = current_app.config['SITES']
    demo = _demo_mode()
    if demo:
        # Abstract the org identifiers (location names) into "Location A/B/…". The
        # frontend reads these names straight from the DOM, so overriding them here is
        # enough; device/event abstraction happens client-side (see DEMO in map.js).
        sites = [{**s, 'name': f'Location {chr(65 + i)}'} for i, s in enumerate(sites)]
    return render_template('index.html', auth_enabled=_auth_required(),
                           sites=sites, demo=demo, is_admin=is_admin(),
                           demo_data=_demo_data(),
                           default_site=current_app.config['DEFAULT_SITE'])


@bp.route('/api/topology')
def get_topology():
    _purge_due_deletions(current_app._get_current_object())   # lazy scheduler tick
    site = _current_site()
    devices, switches = _site_nodes(site)

    all_ips = [s['ip'] for s in switches] + [d['ip'] for d in devices]
    states = db.get_all_states(all_ips)           # from memory (no DB)
    # Positions come from the desktop layout, overlaid with the mobile layout when the
    # client asks for it (?layout=mobile) — so the mobile map starts identical to the
    # desktop one and only diverges for devices dragged in mobile's own Edit Map.
    layout = 'mobile' if request.args.get('layout') == 'mobile' else 'desktop'
    positions = db.get_positions(all_ips)         # desktop (from memory)
    if layout == 'mobile':
        positions.update(db.get_positions(all_ips, 'mobile'))
    # Geographic positions for the aerial basemap — also memory-only, so this
    # stays off the DB on the 30s poll.
    geo_positions = db.get_geo_positions(all_ips)

    slots = _switch_slots(switches)
    sw_by_name = {}
    sw_result = []
    for sw in switches:
        r = _switch_response(sw, states.get(sw['ip']), slots[sw['ip']], positions,
                             geo_positions)
        sw_result.append(r)
        sw_by_name[sw['name']] = r

    by_switch: dict[str, list] = {}
    for d in devices:
        by_switch.setdefault(d.get('switch', '__none__'), []).append(d)

    dev_result = []
    for sw_name, members in by_switch.items():
        sw_pos = sw_by_name.get(sw_name, {'x': 600, 'y': 400})
        for i, d in enumerate(members):
            dev_result.append(
                _device_response(d, states.get(d['ip']), i, len(members), sw_pos,
                                 positions, geo_positions)
            )

    # Flag frequent-outage devices (amber on the map, floated to the top of the
    # sidebar). Recomputed only on a fresh app load/refresh (?fresh=1); the recurring
    # 30s poll serves the cached counts from memory (no DB scan).
    fresh = bool(request.args.get('fresh'))
    counts = _outage_counts_cached(force=fresh)
    full_counts = {ip: counts.get(ip, 0) for ip in all_ips}
    frequent = _frequent_set(full_counts)
    # Devices currently in an ongoing event get an event symbol in the sidebar.
    ongoing_events = _ongoing_events_cached(force=fresh)
    for r in sw_result + dev_result:
        r['outage_count'] = full_counts.get(r['ip'], 0)
        r['frequent'] = r['ip'] in frequent and r.get('enabled', True)
        r['event_cat'] = ongoing_events.get(r['ip'])

    # The demo build has no agent, so the "agent went quiet" banner would always fire —
    # suppress it there (the seeded data is static by design).
    stale_minutes = None if _demo_data() else _monitoring_stale_minutes(site)
    return jsonify({'switches': sw_result, 'devices': dev_result,
                    'pending_delete': _site_pending_delete(site),
                    'monitoring_stale': stale_minutes is not None,
                    'stale_minutes': stale_minutes})


@bp.route('/api/devices')
def get_devices():
    resp = get_topology().get_json()
    return jsonify(resp['switches'] + resp['devices'])


@bp.route('/api/devices/<ip>/history')
def get_history(ip):
    limit = request.args.get('limit', 50, type=int)
    return jsonify(db.get_recent_history(ip, limit=limit))


def _log_cutoff_iso():
    """Lower bound for the activity log: the configured `logging.start_date` at
    midnight Eastern, as a naive-UTC ISO string, so the log begins on that day
    and accumulates forward. None if unset (show all history)."""
    cfg = current_app.config['SETTINGS'].get('logging') or {}
    start = cfg.get('start_date')
    if not start:
        return None
    try:
        d = datetime.strptime(str(start), '%Y-%m-%d').replace(tzinfo=_EASTERN)
    except ValueError:
        return None
    return d.astimezone(_UTC).replace(tzinfo=None).isoformat()


def _log_since():
    """Effective lower bound: the configured start date, optionally narrowed by a
    `?days=N` rolling window (whichever is more recent)."""
    cutoff = _log_cutoff_iso()
    days = request.args.get('days', type=int)
    if days and days > 0:
        rolling = (datetime.utcnow() - timedelta(days=days)).isoformat()
        bounds = [b for b in (cutoff, rolling) if b]
        return max(bounds) if bounds else None
    return cutoff


def _merge_down(own, extra):
    """Union two lists of {start, end, ongoing} offline periods into
    non-overlapping intervals — used to overlay a parent switch's outages onto
    its APs (a device is treated offline whenever its switch is down)."""
    ivals = sorted(((p['start'], None if p.get('ongoing') else p.get('end'))
                    for p in list(own) + list(extra)), key=lambda x: x[0])
    merged = []
    for s, e in ivals:
        if merged and (merged[-1][1] is None or s <= merged[-1][1]):
            le = merged[-1][1]
            merged[-1][1] = None if (le is None or e is None) else max(le, e)
        else:
            merged.append([s, e])
    return [{'start': s, 'end': e, 'ongoing': e is None} for s, e in merged]


def _descendant_ips(devices, switches, switch_name):
    """IPs of everything downstream of a switch: its child APs/others, its child
    switches, and (recursively) their descendants. Excludes the switch itself.
    Association is by parent NAME (APs via `switch`, switches via `uplink`)."""
    result = set()
    seen = {switch_name}
    stack = [switch_name]
    while stack:
        parent = stack.pop()
        for d in devices:
            if d.get('switch') == parent:
                result.add(d['ip'])
        for s in switches:
            if s.get('uplink') == parent and s['name'] not in seen:
                seen.add(s['name'])
                result.add(s['ip'])
                stack.append(s['name'])   # inherit this child switch's own children
    return result


@bp.route('/api/devices/<ip>/tree-log')
def device_tree_log(ip):
    """Activity for a switch AND every device downstream of it (recursively
    through child switches), as a flat newest-first entry list (same shape as
    /api/log). Each entry carries `self` = True when it's the switch itself, so
    the UI can toggle 'this switch' vs 'connected devices'. 404 if not a switch."""
    since = _log_since()
    devices, switches = _site_nodes(_current_site())
    sw = next((s for s in switches if s['ip'] == ip), None)
    if sw is None:
        return jsonify({'error': f'{ip} is not a switch at this location'}), 404

    wanted = {ip} | _descendant_ips(devices, switches, sw['name'])
    by_ip = {d['ip']: (d.get('kind', 'ap'), d) for d in devices if d['ip'] in wanted}
    by_ip.update({s['ip']: ('switch', s) for s in switches if s['ip'] in wanted})

    logs = db.get_all_device_logs(list(by_ip.keys()), since_iso=since)

    entries = []
    for dip, (kind, node) in by_ip.items():
        dl = logs.get(dip, {})
        down = dl.get('down', [])
        # Overlay each AP/other's parent-switch outage onto its offline periods,
        # matching the single-device /log route.
        if kind != 'switch' and node.get('switch'):
            parent = next((s for s in switches if s['name'] == node['switch']), None)
            sw_down = logs.get(parent['ip'], {}).get('down', []) if parent else []
            if sw_down:
                down = db.clip_down_to_pauses(_merge_down(down, sw_down), dl.get('paused', []))
        meta = {'category': kind, 'kind': kind,
                'name': node.get('location') or node['name'], 'device_id': node['name'],
                'ip': dip, 'self': dip == ip}
        ts = dl.get('tracked_since')
        if ts and (since is None or ts >= since):
            entries.append({**meta, 'event': 'tracked', 'start': ts, 'end': None, 'ongoing': False})
        for p in down:
            entries.append({**meta, 'event': 'down', **p})
        for p in dl.get('unknown', []):
            entries.append({**meta, 'event': 'unknown', **p})
        for p in dl.get('paused', []):
            entries.append({**meta, 'event': 'paused', **p})

    # Site-wide "app downtime" context (not tied to a specific device), matching
    # the global log; the UI shows it via the app-downtime filter (off by default).
    for p in db.get_app_downtime_periods(_current_site(), since_iso=since):
        entries.append({'category': 'app', 'kind': 'app', 'name': None, 'ip': None,
                        'self': False, 'event': 'app_downtime', **p})

    entries.sort(key=lambda e: e['start'], reverse=True)
    return jsonify({'entries': entries, 'since': since, 'switch_ip': ip})


@bp.route('/api/devices/<ip>/log')
def device_log(ip):
    """Per-device log: tracked-since date + offline/unknown periods. For an AP/
    other device, its parent switch's outages are overlaid onto its offline
    periods — if the switch is down we treat the device as offline too."""
    since = _log_since()
    result = db.get_device_log(ip, since_iso=since)

    devices, switches = _site_nodes(_current_site())
    node = next((d for d in devices if d['ip'] == ip), None)   # APs/others (not switches)
    if node:
        sw = next((s for s in switches if s['name'] == node.get('switch')), None)
        if sw:
            sw_down = db.get_device_log(sw['ip'], since_iso=since).get('down', [])
            if sw_down:
                merged = _merge_down(result.get('down', []), sw_down)
                # The switch's outage must not paint this AP offline while the AP
                # itself was paused, so re-clip the union to the AP's pauses.
                result['down'] = db.clip_down_to_pauses(merged, result.get('paused', []))

    # Site-wide "app downtime" (nothing pinged) as optional context — the UI
    # exposes it as a filter that's off by default on device pages.
    result['app_downtime'] = db.get_app_downtime_periods(_current_site(), since_iso=since)
    result['since'] = since
    return jsonify(result)


# ── Maintenance windows (mark an outage stretch as maintenance) ──────
# Marking is an overlay: the overlapping slice of an outage shows blue and, when
# it fully covers an outage, that outage stops counting toward the frequent list.
# Times are entered Eastern (date + time) and stored UTC, like scheduled pauses.

def _node_by_ip(ip):
    return next((d for d in current_app.config['DEVICES'] if d['ip'] == ip), None) \
        or next((s for s in current_app.config['SWITCHES'] if s['ip'] == ip), None)


# Sentinel end for an OPEN-ENDED (ongoing / no-end-yet) event: far future, so the
# overlay covers everything from `start` on and the event never buckets as "past"
# until a real end is set. The frontend shows any end from this year as "ongoing".
OPEN_ENDED_ISO = '9999-12-31T23:59:59'


def _maint_times(data):
    """(start_iso, end_iso) from Eastern date/time fields. The END is OPTIONAL — both
    end fields empty → an open-ended event (OPEN_ENDED_ISO). None on bad/partial input."""
    try:
        start_at = _parse_eastern(data['start_date'], data['start_time'])
    except (KeyError, ValueError, TypeError):
        return None
    ed = (data.get('end_date') or '').strip()
    et = (data.get('end_time') or '').strip()
    if not ed and not et:
        return start_at.isoformat(), OPEN_ENDED_ISO
    if not ed or not et:
        return None
    try:
        end_at = _parse_eastern(ed, et)
    except (ValueError, TypeError):
        return None
    if end_at <= start_at:
        return None
    return start_at.isoformat(), end_at.isoformat()


def _clean_log_devices(raw, site_ips):
    """Normalize a `log_devices`/`add_log` payload — [{ip, start, end}] taken straight
    from activity-log entries (already naive-UTC). These devices join an event with the
    entry's OWN window, so they're auto-'resolved' at the entry's end (a still-ongoing
    entry with a blank end stays open via OPEN_ENDED_ISO). Keeps site devices with a
    valid start, drops end-before-start, and collapses repeats of one ip to its
    min-start/max-end. Returns {ip: (start_iso, end_iso)}."""
    out = {}
    for d in raw or []:
        ip = d.get('ip')
        if ip not in site_ips:
            continue
        s = (d.get('start') or '').replace(' ', 'T')
        e = ((d.get('end') or '').replace(' ', 'T')) or OPEN_ENDED_ISO
        if not s or e <= s:
            continue
        out[ip] = (min(out[ip][0], s), max(out[ip][1], e)) if ip in out else (s, e)
    return out


@bp.route('/api/devices/<ip>/maintenance', methods=['POST'])
def add_maintenance(ip):
    """Tag a stretch of ONE device's outage as an event (default category maintenance).
    The quick path from a log entry; the general multi-device flow is POST /api/events."""
    node = _node_by_ip(ip)
    if node is None:
        return jsonify({'error': f'{ip} not found'}), 404
    data = request.get_json() or {}
    times = _maint_times(data)
    if not times:
        return jsonify({'error': 'valid start/end date and time are required '
                                 '(end after start)'}), 400
    category = data.get('category') if data.get('category') in EVENT_CATEGORIES else 'maintenance'
    description = (data.get('description') or '').strip()[:500] or None
    mid = db.add_maintenance_window(ip, times[0], times[1], note=description,
                                    created_by=session.get('user'), category=category)
    g.audit_device_name = _ap_label(node)
    g.audit_event_category = category
    invalidate_outage_cache()          # mark dirty; a viewer's poll recomputes
    return jsonify({'ok': True, 'id': mid})


@bp.route('/api/maintenance/<int:mid>', methods=['PATCH'])
def update_maintenance(mid):
    """Adjust a maintenance window's start/end (e.g. narrow it to only the
    maintenance portion of an outage)."""
    w = db.get_maintenance_window(mid)
    if w is None:
        return jsonify({'error': 'not found'}), 404
    times = _maint_times(request.get_json() or {})
    if not times:
        return jsonify({'error': 'valid start/end date and time are required '
                                 '(end after start)'}), 400
    db.update_maintenance_window(mid, times[0], times[1])
    data = request.get_json() or {}
    if data.get('category') in EVENT_CATEGORIES or 'description' in data:
        category = data.get('category') if data.get('category') in EVENT_CATEGORIES else w['category']
        description = (data.get('description') or '').strip()[:500] or None
        db.set_maintenance_category(mid, category, description)
    node = _node_by_ip(w['device_ip'])
    g.audit_device_name = _ap_label(node) if node else w['device_ip']
    invalidate_outage_cache()
    return jsonify({'ok': True})


@bp.route('/api/maintenance/<int:mid>', methods=['DELETE'])
def delete_maintenance(mid):
    """Unmark: delete a maintenance window (the outage reverts to plain offline)."""
    w = db.get_maintenance_window(mid)
    if w is None:
        return jsonify({'error': 'not found'}), 404
    db.delete_maintenance_window(mid)
    node = _node_by_ip(w['device_ip'])
    g.audit_device_name = _ap_label(node) if node else w['device_ip']
    invalidate_outage_cache()
    return jsonify({'ok': True})


@bp.route('/api/log')
def activity_log():
    """Global chronological log: every device's tracking start + offline/unknown
    periods (by name), plus app-wide downtime. Newest first. Categorized as
    switch/ap/app so the UI can filter. Only events on/after the start date."""
    since = _log_since()
    devices, switches = _site_nodes(_current_site())

    by_ip = {d['ip']: (d.get('kind', 'ap'), d) for d in devices}
    by_ip.update({s['ip']: ('switch', s) for s in switches})
    # Decommissioned (soft-deleted) devices keep their history in the log even though
    # they're gone from the map/list/ping targets.
    for n in db.get_decommissioned_nodes(_current_site()):
        by_ip.setdefault(n['ip'], (n.get('kind', 'ap'), n))
    logs = db.get_all_device_logs(list(by_ip.keys()), since_iso=since)

    entries = []
    for ip, (kind, node) in by_ip.items():
        dl = logs.get(ip, {})
        meta = {'category': kind, 'kind': kind,
                'name': node.get('location') or node['name'], 'device_id': node['name'],
                'ip': ip}
        ts = dl.get('tracked_since')
        # Only surface the tracking-start event if it falls within the window —
        # devices first tracked before the start date get a clean slate.
        if ts and (since is None or ts >= since):
            entries.append({**meta, 'event': 'tracked',
                            'start': ts, 'end': None, 'ongoing': False})
        for p in dl.get('down', []):
            entries.append({**meta, 'event': 'down', **p})
        for p in dl.get('unknown', []):
            entries.append({**meta, 'event': 'unknown', **p})
        for p in dl.get('paused', []):
            entries.append({**meta, 'event': 'paused', **p})
        # A decommissioned (soft-deleted) device logs a "deleted" event at its removal.
        dat = node.get('decommissioned_at')
        if dat and (since is None or dat >= since):
            entries.append({**meta, 'event': 'deleted', 'start': dat, 'end': None, 'ongoing': False})

    for p in db.get_app_downtime_periods(_current_site(), since_iso=since):
        entries.append({'category': 'app', 'kind': 'app', 'name': None, 'ip': None,
                        'event': 'app_downtime', **p})

    entries.sort(key=lambda e: e['start'], reverse=True)
    return jsonify({'entries': entries, 'since': since})


@bp.route('/api/export/xlsx', methods=['POST'])
def export_xlsx():
    """Build an .xlsx from a posted {filename, sheet, headers, rows} payload, with
    each column auto-sized to its widest value. The client has already applied the
    active view/filters, so this just formats exactly what it sends. Login-gated."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    data = request.get_json(silent=True) or {}
    headers = [str(h) for h in (data.get('headers') or [])]
    rows = data.get('rows') or []
    filename = (data.get('filename') or 'export').strip() or 'export'
    if not filename.endswith('.xlsx'):
        filename += '.xlsx'

    wb = Workbook()
    ws = wb.active
    ws.title = (data.get('sheet') or 'Sheet1')[:31]      # Excel caps sheet names at 31
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        # Guard against spreadsheet formula injection: prefix a leading =,+,-,@.
        ws.append(['' if v is None else
                   ("'" + str(v) if str(v)[:1] in ('=', '+', '-', '@') else str(v))
                   for v in row])
    ws.freeze_panes = 'A2'                                 # keep the header row visible

    for c in range(1, len(headers) + 1):
        widest = max((len(str(ws.cell(row=r, column=c).value or ''))
                      for r in range(1, ws.max_row + 1)), default=0)
        ws.column_dimensions[get_column_letter(c)].width = min(max(widest + 2, 8), 60)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/api/devices/<ip>/position', methods=['POST'])
def set_device_position(ip):
    data = request.get_json()
    x, y = data.get('x'), data.get('y')
    if x is None or y is None:
        return jsonify({'error': 'x and y required'}), 400
    # 'mobile' saves to the separate mobile layout; anything else → desktop.
    layout = 'mobile' if data.get('layout') == 'mobile' else 'desktop'
    db.set_position(ip, float(x), float(y), layout)
    return jsonify({'ok': True})


@bp.route('/api/devices/<ip>/geo-position', methods=['POST'])
def set_device_geo_position(ip):
    """Where a device physically is, saved when Edit Map is used on a location with
    an aerial basemap. Unlike the x/y layouts there is no desktop/mobile split — a
    building is in one place."""
    data = request.get_json(silent=True) or {}
    lat, lng = data.get('lat'), data.get('lng')
    if lat is None or lng is None:
        return jsonify({'error': 'lat and lng required'}), 400
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return jsonify({'error': 'lat and lng must be numbers'}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({'error': 'lat/lng out of range'}), 400
    db.set_geo_position(ip, lat, lng)
    return jsonify({'ok': True})


@bp.route('/api/basemap')
def get_basemap():
    """Tile source + georeferencing + visible window for a location's aerial map,
    or {} when it has none. Fetched on page load and on switching location — never
    on the recurring topology poll."""
    return jsonify(_basemap_payload(_current_site()))


@bp.route('/api/basemap/view', methods=['PUT'])
def set_basemap_view():
    """Refine (or reset) the visible map area for a location. Admin-only via the
    _require_admin hook. Stored per-site in app_flags, overriding basemaps.yaml, so
    the window can be tightened without a redeploy — and a future, larger raster can
    be cropped to just the part that matters."""
    site = _current_site()
    if not _basemap_manifest(site):
        return jsonify({'error': 'This location has no aerial map'}), 400
    data = request.get_json(silent=True) or {}

    if data.get('reset'):
        db.set_flag(f'{_BASEMAP_VIEW_FLAG}:{site}', '')
        g.audit_basemap_reset = True
        return jsonify({'ok': True, **_basemap_payload(site)})

    if not geo.valid_view(data):
        return jsonify({'error': 'south/west/north/east required, with south < north '
                                 'and west < east'}), 400
    view = {k: float(data[k]) for k in ('south', 'west', 'north', 'east')}
    db.set_flag(f'{_BASEMAP_VIEW_FLAG}:{site}', json.dumps(view))
    return jsonify({'ok': True, **_basemap_payload(site)})


@bp.route('/api/basemap/open-view', methods=['PUT'])
def set_basemap_open_view():
    """Save (or reset) the default opening view — the pan + zoom everyone gets on
    their first map load. Captured from the admin's current view via the "Set default
    view" button in Edit Map. Saved per **layout** (`desktop`/`mobile`, from the
    request), since a wide desktop framing suits a phone poorly. Admin-only via the
    _require_admin hook. Stored per-site in app_flags; unlike the map area it does NOT
    clamp panning, it only sets where the map opens."""
    site = _current_site()
    if not _basemap_manifest(site):
        return jsonify({'error': 'This location has no aerial map'}), 400
    data = request.get_json(silent=True) or {}
    layout = 'mobile' if data.get('layout') == 'mobile' else 'desktop'

    if data.get('reset'):
        db.set_flag(_open_view_flag(site, layout), '')
        g.audit_open_view_reset = True
        g.audit_open_view_layout = layout
        return jsonify({'ok': True, **_basemap_payload(site)})

    try:
        lat, lng, k = float(data['lat']), float(data['lng']), float(data['k'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'lat, lng and k (zoom) are required numbers'}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180 and k > 0):
        return jsonify({'error': 'lat/lng out of range or non-positive zoom'}), 400
    payload = {'lat': lat, 'lng': lng, 'k': k}
    # Optional auto-crop rectangle (lat/lng box). When present, fitView contains this
    # box in the current viewport instead of using the fixed `k`, so the crop adapts
    # to any window shape.
    box = data.get('box')
    if isinstance(box, dict):
        try:
            b = {side: float(box[side]) for side in ('south', 'west', 'north', 'east')}
            if (-90 <= b['south'] <= b['north'] <= 90
                    and -180 <= b['west'] <= 180 and -180 <= b['east'] <= 180):
                payload['box'] = b
        except (KeyError, TypeError, ValueError):
            pass
    db.set_flag(_open_view_flag(site, layout), json.dumps(payload))
    g.audit_open_view_layout = layout
    return jsonify({'ok': True, **_basemap_payload(site)})


@bp.route('/api/devices/<ip>/enabled', methods=['POST'])
def set_device_enabled(ip):
    """Pause/resume monitoring for a device (off-season / maintenance). Paused
    devices stay listed but are dropped from the agent's ping targets."""
    all_devices = current_app.config['DEVICES']
    all_switches = current_app.config['SWITCHES']
    node = (next((d for d in all_devices if d['ip'] == ip), None)
            or next((s for s in all_switches if s['ip'] == ip), None))
    if node is None:
        return jsonify({'error': f'{ip} not found'}), 404
    # Stash the friendly Name now so the audit line reads "…for Pole Barn", not
    # the IP (the after_request hook builds the summary after config reloads).
    g.audit_device_name = _ap_label(node)
    data = request.get_json() or {}
    enabled = bool(data.get('enabled', True))
    # Pause mode: 'notifications' (Slack off, still monitored) or 'monitoring'
    # (agent stops pinging — no data). Resume ('active') clears whatever was set.
    if enabled:
        mode = 'active'
    else:
        mode = 'notifications' if data.get('mode') == 'notifications' else 'monitoring'
    g.audit_pause_mode = mode

    ips = {ip}
    # Optionally cascade to a switch's downstream devices (recursively). Used by
    # the "also pause connected devices" checkbox and by resuming a switch.
    if data.get('include_children') and node.get('kind', 'ap') == 'switch':
        site = node.get('site', current_app.config['DEFAULT_SITE'])
        devices, switches = _site_nodes(site)
        ips |= _descendant_ips(devices, switches, node['name'])

    for target in ips:
        db.set_device_pause(target, mode)
    if enabled:
        overrode = db.drop_ips_from_active_pauses(ips)
        if overrode:
            g.audit_resume_overrode = overrode
    _reload_config(current_app._get_current_object())
    return jsonify({'ok': True, 'enabled': enabled, 'mode': mode, 'affected': len(ips)})


@bp.route('/api/devices/<ip>/note', methods=['GET', 'PUT'])
def device_note(ip):
    """Free-text note for a device or switch, keyed by IP. Gated by the normal
    login session (not in _PUBLIC_ENDPOINTS)."""
    if request.method == 'PUT':
        data = request.get_json() or {}
        note = (data.get('note') or '')[:NOTE_MAX_LEN]
        db.set_note(ip, note)
        return jsonify({'ok': True})
    return jsonify({'note': db.get_note(ip)})


@bp.route('/api/devices/add', methods=['POST'])
def add_device():
    """Add an access point ('ap') or a generic 'other' device. Both may have an
    optional parent switch. The kind comes from the request (default 'ap')."""
    data = request.get_json()
    name = (data.get('name') or '').strip()
    ip   = (data.get('ip') or '').strip()
    loc  = (data.get('location') or '').strip()
    sw   = (data.get('switch') or '').strip() or None
    host = (data.get('hostname') or '').strip() or None
    imap = (data.get('intermapper_url') or '').strip() or None
    kind = (data.get('kind') or 'ap').strip().lower()
    if kind not in ('ap', 'other'):
        kind = 'ap'

    if not name or not ip or not loc:
        return jsonify({'error': 'Device ID, Name, and IP are required'}), 400
    if not imap:
        return jsonify({'error': 'An Intermapper link is required'}), 400
    if not _IP_RE.match(ip):
        return jsonify({'error': 'invalid IP address format'}), 400

    site = _current_site()
    all_devices  = current_app.config['DEVICES']
    all_switches = current_app.config['SWITCHES']
    # IP/name must be unique across ALL sites (they key the per-IP data tables).
    all_ips   = {d['ip'] for d in all_devices} | {s['ip'] for s in all_switches}
    all_names = {d['name'] for d in all_devices} | {s['name'] for s in all_switches}

    if ip in all_ips:
        return jsonify({'error': f'IP {ip} already exists'}), 409
    if name in all_names:
        return jsonify({'error': f'Name "{name}" already exists'}), 409
    # A parent switch must be at the same site.
    site_switch_names = {s['name'] for s in all_switches if s.get('site') == site}
    if sw and sw not in site_switch_names:
        return jsonify({'error': f'Switch "{sw}" not found at this location'}), 404

    db.add_node(ip=ip, name=name, kind=kind, site=site, location=loc or None, switch=sw,
                hostname=host, intermapper_url=imap)
    _reload_config(current_app._get_current_object())

    entry = {'name': name, 'ip': ip}
    if loc:
        entry['location'] = loc
    if sw:
        entry['switch'] = sw
    return jsonify({'ok': True, 'device': entry}), 201


@bp.route('/api/switches/add', methods=['POST'])
def add_switch():
    data = request.get_json()
    name   = (data.get('name') or '').strip()
    ip     = (data.get('ip') or '').strip()
    loc    = (data.get('location') or '').strip()
    uplink = (data.get('uplink') or '').strip() or None
    host   = (data.get('hostname') or '').strip() or None
    imap   = (data.get('intermapper_url') or '').strip() or None

    if not name or not ip or not loc:
        return jsonify({'error': 'Device ID, Name, and IP are required'}), 400
    if not imap:
        return jsonify({'error': 'An Intermapper link is required'}), 400
    if not _IP_RE.match(ip):
        return jsonify({'error': 'invalid IP address format'}), 400

    site = _current_site()
    all_devices  = current_app.config['DEVICES']
    all_switches = current_app.config['SWITCHES']
    all_ips   = {d['ip'] for d in all_devices} | {s['ip'] for s in all_switches}
    all_names = {d['name'] for d in all_devices} | {s['name'] for s in all_switches}

    if ip in all_ips:
        return jsonify({'error': f'IP {ip} already exists'}), 409
    if name in all_names:
        return jsonify({'error': f'Name "{name}" already exists'}), 409
    if uplink:
        if uplink == name:
            return jsonify({'error': 'a switch cannot uplink to itself'}), 400
        if uplink not in {s['name'] for s in all_switches if s.get('site') == site}:
            return jsonify({'error': f'Uplink switch "{uplink}" not found at this location'}), 404

    db.add_node(ip=ip, name=name, kind='switch', site=site, location=loc or None, uplink=uplink,
                hostname=host, intermapper_url=imap)
    _reload_config(current_app._get_current_object())

    entry = {'name': name, 'ip': ip}
    if loc:
        entry['location'] = loc
    if uplink:
        entry['uplink'] = uplink
    return jsonify({'ok': True, 'switch': entry}), 201


@bp.route('/api/devices/<ip>', methods=['DELETE'])
def delete_device(ip):
    """Remove a device or switch from the map, device list and ping targets, but keep
    its logs. This is a SOFT delete (decommission): the node's row and history stay so
    its entries remain in Device Logs; it's just excluded from the live fleet. Gated by
    the normal login session."""
    all_devices  = current_app.config['DEVICES']
    all_switches = current_app.config['SWITCHES']
    all_ips = {d['ip'] for d in all_devices} | {s['ip'] for s in all_switches}
    if ip not in all_ips:
        return jsonify({'error': f'{ip} not found'}), 404

    node = (next((d for d in all_devices if d['ip'] == ip), None)
            or next((s for s in all_switches if s['ip'] == ip), None))
    if node:
        g.audit_device_name = _ap_label(node)

    db.decommission_node(ip)
    _reload_config(current_app._get_current_object())
    return jsonify({'ok': True})


@bp.route('/api/deleted-devices')
def deleted_devices():
    """Decommissioned (soft-deleted) devices for the current site — powers the
    hamburger's Deleted Devices page. Each can be opened like a live device (its
    history/logs are retained). A safe GET, so viewers may browse it too."""
    return jsonify({'devices': db.get_decommissioned_nodes(_current_site())})


@bp.route('/api/devices/<ip>/restore', methods=['POST'])
def restore_device(ip):
    """Restore a decommissioned device back into the fleet (map / list / ping targets).
    Admin-only (mutating). Its retained history resumes; it returns unplaced."""
    node = next((n for n in db.get_decommissioned_nodes() if n['ip'] == ip), None)
    if node:
        g.audit_device_name = _ap_label(node)
    if not db.restore_node(ip):
        return jsonify({'error': f'{ip} is not a deleted device'}), 404
    _reload_config(current_app._get_current_object())
    return jsonify({'ok': True})


@bp.route('/api/devices/<ip>', methods=['PUT'])
def update_device(ip):
    """Edit a device/switch: name, IP, location, and parent (AP's switch /
    switch's uplink). Kind is fixed. Renames repoint children and IP changes
    migrate history; both handled in db.update_node. Login-gated."""
    data = request.get_json() or {}

    all_devices  = current_app.config['DEVICES']
    all_switches = current_app.config['SWITCHES']
    existing = (next((d for d in all_devices if d['ip'] == ip), None)
               or next((s for s in all_switches if s['ip'] == ip), None))
    if existing is None:
        return jsonify({'error': f'{ip} not found'}), 404
    g.audit_device_name = _ap_label(existing)
    old_kind = existing.get('kind', 'ap')
    kind = (data.get('kind') or old_kind).strip().lower()
    if kind not in ('ap', 'switch', 'other'):
        kind = old_kind

    name   = (data.get('name') or '').strip()
    new_ip = (data.get('ip') or '').strip()
    loc    = (data.get('location') or '').strip()
    # Only overwrite the hostname when the key is present in the payload (so a client
    # that doesn't send it leaves the DNS name untouched); '' clears it.
    host   = ((data.get('hostname') or '').strip() or None) if 'hostname' in data \
             else existing.get('hostname')
    # Same "only overwrite when present" rule for the Intermapper link. It's required
    # at creation; on edit we leave it as-is when the client omits the key, but if the
    # key IS sent it must be non-empty (an edit can't blank out an existing link).
    if 'intermapper_url' in data:
        imap = (data.get('intermapper_url') or '').strip() or None
        if not imap:
            return jsonify({'error': 'An Intermapper link is required'}), 400
    else:
        imap = existing.get('intermapper_url')

    if not name or not new_ip or not loc:
        return jsonify({'error': 'Device ID, Name, and IP are required'}), 400
    if not _IP_RE.match(new_ip):
        return jsonify({'error': 'invalid IP address format'}), 400

    # Collision checks exclude the device being edited (by its current IP).
    other = [d for d in all_devices + all_switches if d['ip'] != ip]
    if new_ip in {d['ip'] for d in other}:
        return jsonify({'error': f'IP {new_ip} already exists'}), 409
    if name in {d['name'] for d in other}:
        return jsonify({'error': f'Name "{name}" already exists'}), 409

    # Converting a switch into an AP/Other would orphan anything that connects to
    # it by name — block until those children are reassigned.
    if old_kind == 'switch' and kind != 'switch':
        children = [d['name'] for d in other
                    if d.get('switch') == existing['name'] or d.get('uplink') == existing['name']]
        if children:
            return jsonify({'error':
                f'{len(children)} device(s) connect to this switch — reassign them before changing its type'}), 409

    switch = uplink = None
    if kind in ('ap', 'other'):
        switch = (data.get('switch') or '').strip() or None
        site = existing.get('site', current_app.config['DEFAULT_SITE'])
        if switch and switch not in {s['name'] for s in all_switches if s.get('site') == site}:
            return jsonify({'error': f'Switch "{switch}" not found at this location'}), 404
    else:
        site = existing.get('site', current_app.config['DEFAULT_SITE'])
        uplink = (data.get('uplink') or '').strip() or None
        if uplink:
            if uplink == name:
                return jsonify({'error': 'a switch cannot uplink to itself'}), 400
            if uplink not in {s['name'] for s in all_switches if s['ip'] != ip and s.get('site') == site}:
                return jsonify({'error': f'Uplink switch "{uplink}" not found at this location'}), 404

    db.update_node(ip, new_ip, name, kind, location=loc or None, switch=switch,
                   uplink=uplink, old_name=existing['name'], old_kind=old_kind,
                   hostname=host, intermapper_url=imap)
    _reload_config(current_app._get_current_object())
    return jsonify({'ok': True, 'ip': new_ip})


@bp.route('/api/sites', methods=['POST'])
def add_site():
    """Create a new location. Devices can then be added to it, and the
    all-locations agent (SITE=all) picks it up automatically. Login-gated."""
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'a location name is required'}), 400

    base = _slugify(name)
    if not base:
        return jsonify({'error': 'name must contain letters or numbers'}), 400
    if name in {s['name'] for s in current_app.config['SITES']}:
        return jsonify({'error': f'Location "{name}" already exists'}), 409

    # Ensure a unique key (append -2, -3, … on collision).
    keys = current_app.config['SITE_KEYS']
    key, n = base, 2
    while key in keys:
        key, n = f'{base}-{n}', n + 1

    db.add_site(key, name)
    _reload_sites(current_app._get_current_object())
    return jsonify({'ok': True, 'site': {'key': key, 'name': name}}), 201


@bp.route('/api/sites/<key>', methods=['PATCH'])
def rename_site(key):
    """Rename a location (its key/devices are unchanged). Login-gated."""
    if key not in current_app.config['SITE_KEYS']:
        return jsonify({'error': 'location not found'}), 404
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'a location name is required'}), 400
    if not _slugify(name):
        return jsonify({'error': 'name must contain letters or numbers'}), 400
    # Reject a name already used by a *different* location.
    if any(s['name'] == name and s['key'] != key for s in current_app.config['SITES']):
        return jsonify({'error': f'Location "{name}" already exists'}), 409

    db.rename_site(key, name)
    _reload_sites(current_app._get_current_object())
    return jsonify({'ok': True, 'site': {'key': key, 'name': name}})


SITE_DELETE_GRACE_HOURS = 24


@bp.route('/api/sites/<key>', methods=['DELETE'])
def delete_site(key):
    """Request deletion of a location. Rather than delete immediately, this
    SCHEDULES a soft-delete SITE_DELETE_GRACE_HOURS in the future and posts a
    time-sensitive Slack notice; the location keeps working (and shows an in-app
    'undo' popup) until then, when purge_due_site_deletions() finally removes it.
    Requires the ADMIN_PASSWORD on top of the normal login."""
    if key not in current_app.config['SITE_KEYS']:
        return jsonify({'error': 'location not found'}), 404
    if len(current_app.config['SITES']) <= 1:
        return jsonify({'error': 'cannot delete the only remaining location'}), 409

    admin_pw = os.environ.get('ADMIN_PASSWORD')
    if not admin_pw:
        return jsonify({'error':
            'Location deletion is disabled — no ADMIN_PASSWORD is configured on the server.'}), 403
    supplied = (request.get_json(silent=True) or {}).get('password') or ''
    if not hmac.compare_digest(supplied, admin_pw):
        return jsonify({'error': 'Incorrect admin password'}), 403

    name = next((s['name'] for s in current_app.config['SITES'] if s['key'] == key), key)
    delete_at = datetime.utcnow() + timedelta(hours=SITE_DELETE_GRACE_HOURS)
    db.schedule_site_deletion(key, delete_at)
    notifier.send_site_deletion_notice(
        current_app.config['SETTINGS'], name, app_url=request.url_root.rstrip('/'), site=key)
    _reload_sites(current_app._get_current_object())   # refresh pending flag in config
    return jsonify({'ok': True, 'scheduled': True,
                    'delete_at': delete_at.isoformat(),
                    'grace_hours': SITE_DELETE_GRACE_HOURS})


@bp.route('/api/sites/<key>/undo-delete', methods=['POST'])
def undo_site_deletion(key):
    """Cancel a scheduled location deletion (the in-app 'undo'). Login-gated only —
    reverting an accidental deletion should be easy, so no admin password."""
    if key not in current_app.config['SITE_KEYS']:
        return jsonify({'error': 'location not found'}), 404
    db.cancel_site_deletion(key)
    _reload_sites(current_app._get_current_object())
    return jsonify({'ok': True})


_EASTERN_TZ = ZoneInfo('America/New_York')


def _parse_eastern(date_str: str, time_str: str) -> datetime:
    """Combine a 'YYYY-MM-DD' date + 'HH:MM' time entered in Eastern into a
    naive-UTC datetime for storage (matching the app's UTC-in-DB convention)."""
    local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return local.replace(tzinfo=_EASTERN_TZ).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)


@bp.route('/api/scheduled-pauses', methods=['GET', 'POST'])
def scheduled_pauses():
    """List (GET) or create (POST) scheduled monitoring-pause windows for the
    current site. Times are entered in Eastern and stored UTC."""
    site = _current_site()
    if request.method == 'GET':
        return jsonify({'pauses': db.get_scheduled_pauses(site)})

    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'a name is required'}), 400
    try:
        start_at = _parse_eastern(data['start_date'], data['start_time'])
        end_at = _parse_eastern(data['end_date'], data['end_time'])
    except (KeyError, ValueError, TypeError):
        return jsonify({'error': 'valid start/end date and time are required'}), 400
    if end_at <= start_at:
        return jsonify({'error': 'the end must be after the start'}), 400
    if end_at <= datetime.utcnow():
        return jsonify({'error': 'that window is already in the past'}), 400
    # A pause must be scheduled at least SCHEDULED_PAUSE_MIN_LEAD ahead: the watchdog
    # normally ticks slowly and only tightens once a pause is within that lead time,
    # so a nearer start could be missed.
    if start_at < datetime.utcnow() + SCHEDULED_PAUSE_MIN_LEAD:
        hrs = int(SCHEDULED_PAUSE_MIN_LEAD.total_seconds() // 3600)
        return jsonify({'error': f'a pause must be scheduled at least {hrs} hours in advance'}), 400

    devices, switches = _site_nodes(site)
    site_ips = {d['ip'] for d in devices} | {s['ip'] for s in switches}
    device_ips = [ip for ip in (data.get('device_ips') or []) if ip in site_ips]
    if not device_ips:
        return jsonify({'error': 'select at least one device at this location'}), 400

    mode = 'notifications' if data.get('mode') == 'notifications' else 'monitoring'
    db.add_scheduled_pause(site, name, start_at, end_at, device_ips, mode=mode)
    db.refresh_open_pauses()      # update the watchdog's open-flag + next boundary now
    monitor.wake_watchdog()       # re-evaluate cadence now (may tighten to 30-min ticks)
    return jsonify({'ok': True}), 201


@bp.route('/api/scheduled-pauses/<int:pause_id>', methods=['DELETE'])
def cancel_scheduled_pause(pause_id):
    """Cancel/remove a scheduled pause. If it's already active, resume its devices
    (unless another active window still covers them) before removing it."""
    sp = db.get_scheduled_pause(pause_id)
    if not sp:
        return jsonify({'error': 'not found'}), 404
    g.audit_pause_name = sp.get('name')   # for the audit log (the row is deleted below)
    if sp['status'] == 'active':
        still_paused = db.active_pause_ips(exclude_id=pause_id)
        for ip in sp['device_ips']:
            if ip not in still_paused:
                db.set_node_enabled(ip, True)
        _reload_config(current_app._get_current_object())
        # An active pause is part of an ongoing event → cap that event's still-ongoing
        # parts for these devices at now, so an event kept ongoing only by this pause
        # ends when the pause is cancelled. (Only for an active pause: a future/scheduled
        # pause's event isn't ongoing yet.)
        if sp.get('event_group_id'):
            db.end_event_open_devices(sp['event_group_id'], sp['device_ips'],
                                      datetime.utcnow().isoformat())
            invalidate_outage_cache()
    db.delete_scheduled_pause(pause_id)
    db.refresh_open_pauses()      # clear/refresh the watchdog's open-flag + boundary
    monitor.wake_watchdog()       # re-evaluate cadence now (may relax back to 8-hour ticks)
    return jsonify({'ok': True})


# ── Events (generalized maintenance overlay + optional pause) ────────
EVENT_CATEGORIES = ('maintenance', 'lightning', 'power_outage', 'scheduled_downtime', 'other')


@bp.route('/api/events', methods=['GET', 'POST'])
def events():
    """List (GET, grouped Upcoming/Present/Past) or create (POST) events for the site.
    An event is always an overlay that excludes covered outages from the frequent list;
    a pause is attached only when the window reaches the future and the user opts in."""
    site = _current_site()
    devices, switches = _site_nodes(site)
    site_ips = {d['ip'] for d in devices} | {s['ip'] for s in switches}

    if request.method == 'GET':
        raw = db.get_events(list(site_ips))
        pause_by_group = db.get_pause_modes_by_group(site)
        now = datetime.utcnow()
        buckets = {'upcoming': [], 'present': [], 'past': []}

        def _bucket(s, e):
            return 'past' if e <= now else ('present' if s <= now else 'upcoming')

        for ev in raw:
            try:
                s, e = datetime.fromisoformat(ev['start']), datetime.fromisoformat(ev['end'])
            except (ValueError, TypeError):
                continue
            ev['type'] = 'event'
            ev['pause'] = pause_by_group.get(ev['group_id']) if ev.get('group_id') else None
            buckets[_bucket(s, e)].append(ev)

        # (Device monitoring/notification pauses are intentionally NOT listed here — the
        # Event Logs tab shows events only. Pauses still appear in the device/activity log.)

        # Reverse chronological (most recent first) within every section.
        for b in buckets.values():
            b.sort(key=lambda x: x['start'], reverse=True)
        return jsonify(buckets)

    # POST — MARK a PAST or ONGOING event. (FUTURE scheduling is a separate flow:
    # POST /api/events/schedule.) Composition:
    #   • log_devices [{ip,start,end}] — outages/pauses picked from the activity log;
    #     each joins with its OWN window (auto-resolved at the entry's end).
    #   • affected_ips — devices STILL affected (only when `ongoing`); open-ended rows
    #     that must be resolved later. Optional pause applies to THESE only.
    #   • device_ips — back-compat full-window devices (the maint modal quick path).
    data = request.get_json() or {}
    category = data.get('category')
    if category not in EVENT_CATEGORIES:
        return jsonify({'error': 'a valid category is required'}), 400
    description = (data.get('description') or '').strip()[:500] or None

    # Optional event-level bounds (each hidden behind a checkbox → may be absent).
    def _opt_eastern(dk, tk):
        d = (data.get(dk) or '').strip(); t = (data.get(tk) or '').strip()
        if not d or not t:
            return None
        return _parse_eastern(d, t).isoformat()
    try:
        ev_start = _opt_eastern('start_date', 'start_time')
        ev_end = _opt_eastern('end_date', 'end_time')
    except (ValueError, TypeError):
        return jsonify({'error': 'invalid date/time'}), 400
    if ev_start and ev_end and ev_end <= ev_start:
        return jsonify({'error': 'the end must be after the start'}), 400

    ongoing = bool(data.get('ongoing'))
    affected = [ip for ip in (data.get('affected_ips') or []) if ip in site_ips] if ongoing else []
    log_devs = _clean_log_devices(data.get('log_devices'), site_ips)
    device_ips = [ip for ip in (data.get('device_ips') or []) if ip in site_ips]
    if not affected and not log_devs and not device_ips:
        return jsonify({'error': 'pick at least one outage from the activity log '
                                 '(or mark a device as still affected)'}), 400

    now = datetime.utcnow()
    group_id = uuid.uuid4().hex

    # (1) Still-affected devices → open-ended rows, anchored at the given start or the
    #     device's current ongoing-issue start (fallback now).
    for ip in affected:
        s = ev_start or db.current_issue_start(ip) or now.isoformat()
        db.add_maintenance_window(ip, s, OPEN_ENDED_ISO, note=description,
                                  created_by=session.get('user'), category=category,
                                  event_group_id=group_id)
    # (2) Log-selected outages → each entry's own window, clipped to any event bounds.
    #     A still-affected device keeps its open-ended row AND gets a separate capped row
    #     for each EARLIER (already-ended) outage picked from the log — only its ongoing
    #     selection is skipped (already covered by the open-ended row).
    for ip, (s, e) in log_devs.items():
        if ip in affected and e >= OPEN_ENDED_ISO:
            continue
        cs = max(s, ev_start) if ev_start else s
        ce = min(e, ev_end) if ev_end else e
        if ce <= cs:
            continue
        db.add_maintenance_window(ip, cs, ce, note=description, created_by=session.get('user'),
                                  category=category, event_group_id=group_id)
    # (3) Back-compat full-window devices (maint modal) over [ev_start, ev_end].
    for ip in device_ips:
        if ip in affected or ip in log_devs:
            continue
        db.add_maintenance_window(ip, ev_start or now.isoformat(), ev_end or OPEN_ENDED_ISO,
                                  note=description, created_by=session.get('user'),
                                  category=category, event_group_id=group_id)

    # Optional pause — applies to the still-affected devices only (present pause, applied
    # now, resumed when each device is resolved).
    pause_mode = data.get('pause_mode') if data.get('pause_mode') in ('monitoring', 'notifications') else None
    if pause_mode and affected:
        db.add_scheduled_pause(site, category, now, datetime.fromisoformat(OPEN_ENDED_ISO),
                               affected, mode=pause_mode, status='active', category=category,
                               description=description, event_group_id=group_id)
        for ip in affected:
            db.set_device_pause(ip, pause_mode)
        _reload_config(current_app._get_current_object())
        db.refresh_open_pauses()
        monitor.wake_watchdog()

    g.audit_event_category = category
    invalidate_outage_cache()
    return jsonify({'ok': True, 'group_id': group_id}), 201


@bp.route('/api/events/schedule', methods=['POST'])
def schedule_event():
    """Create a FUTURE (scheduled) event — the future-events flow, fully separate from
    marking past/ongoing events (POST /api/events). An overlay over [start, end] for the
    chosen devices plus an OPTIONAL scheduled pause. Body: category, description,
    start_date/time (required, must be future), end_date/time?, device_ips (device list
    only — no log items), pause_mode?. A pause needs the watchdog's 8h lead."""
    site = _current_site()
    devices, switches = _site_nodes(site)
    site_ips = {d['ip'] for d in devices} | {s['ip'] for s in switches}
    data = request.get_json() or {}
    category = data.get('category')
    if category not in EVENT_CATEGORIES:
        return jsonify({'error': 'a valid category is required'}), 400
    description = (data.get('description') or '').strip()[:500] or None
    times = _maint_times(data)             # start required; blank end → OPEN_ENDED
    if not times:
        return jsonify({'error': 'a valid start date/time is required'}), 400
    start_iso, end_iso = times
    start_dt, end_dt = datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso)
    now = datetime.utcnow()
    if start_dt <= now:
        return jsonify({'error': 'a scheduled event must start in the future — '
                                 'use "Mark an event" for something that already happened'}), 400
    device_ips = [ip for ip in (data.get('device_ips') or []) if ip in site_ips]
    if not device_ips:
        return jsonify({'error': 'select at least one device'}), 400
    pause_mode = data.get('pause_mode') if data.get('pause_mode') in ('monitoring', 'notifications') else None
    if pause_mode and start_dt < now + SCHEDULED_PAUSE_MIN_LEAD:
        hrs = int(SCHEDULED_PAUSE_MIN_LEAD.total_seconds() // 3600)
        return jsonify({'error': f'to pause, schedule the event at least {hrs} hours ahead'}), 400

    group_id = uuid.uuid4().hex
    db.add_event(category, description, start_iso, end_iso, device_ips, group_id,
                 created_by=session.get('user'))
    if pause_mode:
        db.add_scheduled_pause(site, category, start_dt, end_dt, device_ips, mode=pause_mode,
                               status='scheduled', category=category, description=description,
                               event_group_id=group_id)
        db.refresh_open_pauses()
        monitor.wake_watchdog()
    g.audit_event_category = category
    invalidate_outage_cache()
    return jsonify({'ok': True, 'group_id': group_id}), 201


@bp.route('/api/events/<group_id>', methods=['PATCH', 'DELETE'])
def event_detail(group_id):
    """Edit times/description or delete an entire event group (all its devices).
    Deleting also cancels a linked pause (resuming devices if it was active)."""
    site = _current_site()
    if request.method == 'DELETE':
        db.delete_event(group_id)
        pause = db.get_pause_by_group(group_id)
        if pause:
            if pause['status'] == 'active':
                still = db.active_pause_ips(exclude_id=pause['id'])
                for ip in pause['device_ips']:
                    if ip not in still:
                        db.set_node_enabled(ip, True)
                _reload_config(current_app._get_current_object())
            db.delete_scheduled_pause(pause['id'])
            db.refresh_open_pauses()
            monitor.wake_watchdog()
        g.audit_event_group = group_id
        invalidate_outage_cache()
        return jsonify({'ok': True})

    data = request.get_json() or {}
    # Description-only edit (from the detail view's description box) — no time fields sent.
    if 'start_date' not in data:
        description = (data.get('description') or '').strip()[:500] or None
        db.update_event_description(group_id, description)
        g.audit_event_group = group_id
        invalidate_outage_cache()
        return jsonify({'ok': True})
    times = _maint_times(data)
    if not times:
        return jsonify({'error': 'valid start/end date and time are required (end after start)'}), 400
    description = (data.get('description') or '').strip()[:500] or None
    db.update_event_times(group_id, times[0], times[1], description=description)
    if data.get('category') in EVENT_CATEGORIES:
        db.update_event_category(group_id, data['category'])
    pause = db.get_pause_by_group(group_id)
    if pause:
        # Keep a linked pause's window in sync (best-effort). A scheduled (future) pause
        # tracks both edges; an active one only its end (its start already happened).
        new_start = times[0] if pause['status'] == 'scheduled' else pause['start_at']
        db.update_scheduled_pause_times(pause['id'], new_start, times[1])
        db.refresh_open_pauses()
        monitor.wake_watchdog()
    g.audit_event_group = group_id
    invalidate_outage_cache()
    return jsonify({'ok': True})


@bp.route('/api/events/<group_id>/devices', methods=['POST'])
def event_devices(group_id):
    """Add/remove devices on an existing event. Body: {add: [ips], remove: [ips],
    add_log: [{ip,start,end}]}. `add` devices join with the event's full window; `add_log`
    devices (picked from the activity log) join with their entry's OWN window so they're
    auto-resolved at the entry's end. Keeps a linked pause's device set + state in sync."""
    site = _current_site()
    devices, switches = _site_nodes(site)
    site_ips = {d['ip'] for d in devices} | {s['ip'] for s in switches}
    data = request.get_json() or {}
    add = [ip for ip in (data.get('add') or []) if ip in site_ips]
    remove = [ip for ip in (data.get('remove') or []) if ip in site_ips]
    log_devs = _clean_log_devices(data.get('add_log'), site_ips)
    for ip in add:
        log_devs.pop(ip, None)          # a full-window add wins over a log add
    if not add and not remove and not log_devs:
        return jsonify({'error': 'nothing to change'}), 400
    if remove:
        db.remove_event_devices(group_id, remove)
    if add:
        db.add_event_devices(group_id, add, created_by=session.get('user'))
    if log_devs:
        ev = db.get_event(group_id)
        existing = set(ev['device_ips']) if ev else set()
        cat = ev['category'] if ev else 'maintenance'
        note = ev['description'] if ev else None
        for ip, (s, e) in log_devs.items():
            # Add the picked outage as its own capped row even if the device is already
            # in the event (e.g. an EARLIER event-related outage for a still-affected
            # device). Only skip a still-ONGOING pick for a device already present, since
            # its ongoing window already covers that.
            if ip in existing and e >= OPEN_ENDED_ISO:
                continue
            db.add_maintenance_window(ip, s, e, note=note, created_by=session.get('user'),
                                      category=cat, event_group_id=group_id)

    pause = db.get_pause_by_group(group_id)
    if pause:
        new_ips = [ip for ip in pause['device_ips'] if ip not in remove]
        for ip in add:
            if ip not in new_ips:
                new_ips.append(ip)
        db.set_scheduled_pause_devices(pause['id'], new_ips)
        if pause['status'] == 'active':
            # Apply the pause to newly-added devices now; resume removed ones (unless
            # another active window still covers them).
            for ip in add:
                db.set_device_pause(ip, pause['mode'])
            still = db.active_pause_ips(exclude_id=pause['id'])
            for ip in remove:
                if ip not in still:
                    db.set_node_enabled(ip, True)
            _reload_config(current_app._get_current_object())
        db.refresh_open_pauses()
        monitor.wake_watchdog()

    g.audit_event_group = group_id
    invalidate_outage_cache()
    return jsonify({'ok': True})


@bp.route('/api/events/<group_id>/resolve-device', methods=['POST'])
def resolve_event_device(group_id):
    """End (or reopen) ONE device's participation in an ongoing event. Capping a
    device's window keeps its earlier activity tagged as part of the event but stops
    later, unrelated outages from being attributed to it — while the event stays open
    for the other devices. Body: {ip, end_date?, end_time?} (Eastern; default now), or
    {ip, reopen: true} to make that device open-ended again."""
    data = request.get_json() or {}
    ip = data.get('ip')
    ev = db.get_event(group_id)
    if not ev or ip not in ev['device_ips']:
        return jsonify({'error': 'device not part of this event'}), 404
    # The device may have several rows (an open-ended one + capped historical ones);
    # resolving concerns its OPEN-ended row.
    ongoing_starts = [d['start'] for d in ev['devices'] if d['ip'] == ip and d['end'] >= OPEN_ENDED_ISO]

    if data.get('reopen'):
        db.set_event_device_end(group_id, ip, OPEN_ENDED_ISO)
    else:
        ed = (data.get('end_date') or '').strip()
        et = (data.get('end_time') or '').strip()
        if ed and et:
            try:
                end_iso = _parse_eastern(ed, et).isoformat()
            except (ValueError, TypeError):
                return jsonify({'error': 'invalid resolution time'}), 400
        else:
            end_iso = datetime.utcnow().isoformat()
        if ongoing_starts and end_iso <= min(ongoing_starts):
            return jsonify({'error': 'resolution time must be after the event start'}), 400
        db.set_event_device_end(group_id, ip, end_iso, only_open=True)
        # The device is fixed now → if a linked pause is actively pausing it, resume it
        # (unless another active window still covers it) and drop it from that pause.
        pause = db.get_pause_by_group(group_id)
        if pause and pause['status'] == 'active' and ip in pause['device_ips']:
            still = db.active_pause_ips(exclude_id=pause['id'])
            if ip not in still:
                db.set_node_enabled(ip, True)
            db.set_scheduled_pause_devices(pause['id'], [x for x in pause['device_ips'] if x != ip])
            _reload_config(current_app._get_current_object())
            db.refresh_open_pauses()
            monitor.wake_watchdog()

    node = _node_by_ip(ip)
    g.audit_device_name = _ap_label(node) if node else ip
    g.audit_event_group = group_id
    g.audit_resolve_reopen = bool(data.get('reopen'))
    invalidate_outage_cache()
    return jsonify({'ok': True})


@bp.route('/api/events/device-log', methods=['POST'])
def event_device_log():
    """Log entries (offline / event / unknown / paused) for a set of devices, clipped
    to an event window [start, end]. Powers the 'device activity during this event'
    section of the single-event detail view. Body: {device_ips, start, end}."""
    data = request.get_json() or {}
    ips = data.get('device_ips') or []
    start, end = data.get('start'), data.get('end')
    if not ips or not start or not end:
        return jsonify({'entries': []})
    # Compare naive-UTC ISO as strings, but normalize the date/time separator so a
    # space-vs-'T' mismatch can't corrupt the ordering.
    _iso = lambda v: v.replace(' ', 'T') if isinstance(v, str) else v
    start, end = _iso(start), _iso(end)
    devices, switches = _site_nodes(_current_site())
    by_ip = {d['ip']: (d.get('kind', 'ap'), d) for d in devices}
    by_ip.update({s['ip']: ('switch', s) for s in switches})
    ips = [ip for ip in ips if ip in by_ip]

    def _overlaps(p):
        s = _iso(p.get('start'))
        if not s or s >= end:
            return False
        e = _iso(p.get('end'))
        return e is None or e > start

    entries = []
    for ip in ips:
        kind, node = by_ip[ip]
        result = db.get_device_log(ip)          # full history; clipped to the window below
        # Overlay a parent switch's outages onto an AP/other (same as /api/devices/<ip>/log).
        if kind != 'switch':
            sw = next((s for s in switches if s['name'] == node.get('switch')), None)
            if sw:
                sw_down = db.get_device_log(sw['ip']).get('down', [])
                if sw_down:
                    merged = _merge_down(result.get('down', []), sw_down)
                    result['down'] = db.clip_down_to_pauses(merged, result.get('paused', []))
        meta = {'category': kind, 'kind': kind,
                'name': node.get('location') or node['name'], 'device_id': node['name'], 'ip': ip}
        for ev_type in ('down', 'unknown', 'paused'):
            for p in result.get(ev_type, []):
                if _overlaps(p):
                    entries.append({**meta, 'event': ev_type, **p})
    entries.sort(key=lambda e: e['start'], reverse=True)
    return jsonify({'entries': entries})


@bp.route('/api/events/combine', methods=['POST'])
def combine_events():
    """Merge selected items into one event: union of devices, earliest start → latest
    end, the first EVENT's category + joined descriptions. Events/single marks are
    consumed (deleted, linked pauses cancelled); device monitoring/notification pauses
    are NOT deleted — they're real monitoring history, and the new event just spans
    them so they show as 'part of' it via the overlay. Body: {group_ids, maint_ids,
    pauses: [{device_ip, start, end}]}."""
    site = _current_site()
    devices, switches = _site_nodes(site)
    site_ips = {d['ip'] for d in devices} | {s['ip'] for s in switches}
    data = request.get_json() or {}
    group_ids = list(data.get('group_ids') or [])
    maint_ids = []
    for m in (data.get('maint_ids') or []):
        try:
            maint_ids.append(int(m))
        except (ValueError, TypeError):
            pass

    # Normalize events, single-outage marks, and device pauses into one shape so any
    # mix can be combined.
    sources = []
    for gid in group_ids:
        e = db.get_event(gid)
        if e:
            sources.append({'kind': 'group', 'ref': gid, 'device_ips': e['device_ips'],
                            'start': e['start'], 'end': e['end'],
                            'category': e['category'], 'description': e.get('description')})
    for mid in maint_ids:
        w = db.get_maintenance_window(mid)
        if w:
            sources.append({'kind': 'single', 'ref': mid, 'device_ips': [w['device_ip']],
                            'start': w['start'], 'end': w['end'],
                            'category': w['category'], 'description': w.get('description')})
    for p in (data.get('pauses') or []):
        ip, s0 = p.get('device_ip'), p.get('start')
        if ip in site_ips and s0:
            # An ongoing pause (no end) makes the merged event open-ended.
            sources.append({'kind': 'pause', 'ref': None, 'device_ips': [ip],
                            'start': s0, 'end': p.get('end') or OPEN_ENDED_ISO,
                            'category': None, 'description': None})
    if len(sources) < 2:
        return jsonify({'error': 'select at least two items to combine'}), 400

    dev_ips, starts, ends, descs = [], [], [], []
    for s in sources:
        dev_ips += [ip for ip in s['device_ips'] if ip in site_ips]
        starts.append(s['start'])
        ends.append(s['end'])
        if s.get('description'):
            descs.append(s['description'])
    dev_ips = list(dict.fromkeys(dev_ips))    # dedupe, keep order
    if not dev_ips:
        return jsonify({'error': 'no devices to combine'}), 400
    # Category comes from the first real event; pure-pause combines default to maintenance.
    category = next((s['category'] for s in sources if s.get('category')), 'maintenance')
    description = ('; '.join(dict.fromkeys(descs)))[:500] or None
    new_group = uuid.uuid4().hex
    db.add_event(category, description, min(starts), max(ends), dev_ips, new_group,
                 created_by=session.get('user'))

    # Consume the event sources — groups also cancel their pauses (resuming devices not
    # still covered); singles are just a row delete. Device pauses are left intact.
    for s in sources:
        if s['kind'] == 'pause':
            continue
        if s['kind'] == 'single':
            db.delete_maintenance_window(s['ref'])
            continue
        db.delete_event(s['ref'])
        pause = db.get_pause_by_group(s['ref'])
        if pause:
            if pause['status'] == 'active':
                still = db.active_pause_ips(exclude_id=pause['id'])
                for ip in pause['device_ips']:
                    if ip not in still:
                        db.set_node_enabled(ip, True)
            db.delete_scheduled_pause(pause['id'])
    _reload_config(current_app._get_current_object())
    db.refresh_open_pauses()
    monitor.wake_watchdog()
    g.audit_event_group = new_group
    invalidate_outage_cache()
    return jsonify({'ok': True, 'group_id': new_group})


@bp.route('/api/check-now', methods=['POST'])
def check_now():
    # Pinging happens on the remote agent, so we can't ping here. Instead set a
    # per-site flag the agent picks up on its next /api/agent/targets poll and
    # pings immediately. An `ip` in the body → a TARGETED check of just that device
    # (the single-device panel button); no `ip` → a FULL sweep (the header button).
    # If a local scheduler is enabled (on-LAN mode), also run a one-off check so the
    # button stays instant there.
    # A `device` (Device ID, preferred) or `ip` (legacy) targets one device; neither
    # → a full sweep.
    site = _current_site()
    data = request.get_json(silent=True) or {}
    ref = (data.get('device') or data.get('ip') or '').strip()
    ip = _resolve_ref(ref) if ref else ''
    db.request_check(site, ips=[ip] if ip else None)
    if os.environ.get('ENABLE_LOCAL_PING') == '1':
        devices, switches = _site_nodes(site)
        if ip:   # ping only the requested device
            devices = [d for d in devices if d['ip'] == ip]
            switches = [s for s in switches if s['ip'] == ip]
        monitor.run_check_now(devices, switches, current_app.config['SETTINGS'])
    return jsonify({'ok': True, 'message': 'Check requested — agent will ping within its poll interval'})


# ── Log editor (admin-password-gated pause-log deletion) ─────────────
def _log_edit_ok() -> bool:
    """Whether this session has unlocked log editing with the admin password (valid
    for an hour after unlocking)."""
    return float(session.get('log_edit_until', 0)) > time.time()


@bp.route('/api/log-edit/unlock', methods=['POST'])
def log_edit_unlock():
    """Re-enter ADMIN_PASSWORD to enable deleting monitoring-pause log entries. On
    success a short-lived session flag authorizes the delete route. Admin-only (the
    _require_admin hook) on top of the password."""
    admin_pw = os.environ.get('ADMIN_PASSWORD')
    if not admin_pw:
        return jsonify({'error': 'Log editing is disabled — no ADMIN_PASSWORD is configured on the server.'}), 403
    supplied = (request.get_json(silent=True) or {}).get('password', '')
    if not hmac.compare_digest(supplied, admin_pw):
        return jsonify({'error': 'Incorrect password.'}), 403
    session['log_edit_until'] = time.time() + 3600   # 1 hour
    return jsonify({'ok': True})


@bp.route('/api/pause-periods/<int:pause_id>', methods=['DELETE'])
def delete_pause_log(pause_id):
    """Delete one monitoring/notification-pause LOG entry. Requires the log-edit unlock
    (admin password) in addition to the normal admin login. Removes only the historical
    row — a device's current pause state (its enabled/notify flags) is untouched."""
    if not _log_edit_ok():
        return jsonify({'error': 'Enter the admin password to edit logs first.'}), 403
    if not db.delete_pause_period(pause_id):
        return jsonify({'error': 'not found'}), 404
    return jsonify({'ok': True})


@bp.route('/api/user-activity', methods=['GET'])
def user_activity():
    """Read-only audit trail of successful, user-initiated mutating requests.
    Login-gated (not in _PUBLIC_ENDPOINTS); this is a GET so it isn't self-logged.
    Scoped to the current location (legacy rows with no site show everywhere)."""
    return jsonify({'entries': db.get_user_activity(200, _current_site())})


@bp.route('/agent/download')
def agent_download():
    """Download a ready-to-run agent bundle (zip): ping_agent.py, README, and a
    pre-filled .env. The agent always runs as SITE=all (one agent covers every
    location) with the master AGENT_TOKEN, so the bundle is identical regardless of
    which location is being viewed. Login-gated (admin-only)."""
    base = os.path.dirname(os.path.dirname(__file__))
    agent_dir = os.path.join(base, 'agent')
    server_url = request.url_root.rstrip('/')
    env = (
        f"# Agent config. Fill in AGENT_TOKEN (must match the MASTER AGENT_TOKEN config\n"
        f"# var on Heroku — SITE=all uses the master token), then run.\n"
        f"SERVER_URL={server_url}\n"
        f"AGENT_TOKEN=change-me-to-a-long-random-string\n"
        f"SITE=all\n"
        f"POLL_INTERVAL=60\n"
        f"FLAG_POLL_INTERVAL=20\n"
        f"\n"
        f"# Friendly name for this agent (e.g. windows-server), recorded on the outages\n"
        f"# it reports. Defaults to the machine's hostname if left unset.\n"
        f"# AGENT_NAME=windows-server\n"
        f"\n"
        f"# Optional persistent log (recommended on Windows). Uncomment to write a\n"
        f"# size-capped, auto-rotating log (LOG_MAX_MB x LOG_BACKUPS, ~20 MB by\n"
        f"# default). If set, do NOT also redirect output to the same file.\n"
        f"# LOG_FILE=C:\\ap-monitor-agent\\agent.log\n"
        f"# LOG_MAX_MB=5\n"
        f"# LOG_BACKUPS=3\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for fn in ('ping_agent.py', 'README.md'):
            path = os.path.join(agent_dir, fn)
            if os.path.exists(path):
                z.write(path, arcname=f'agent/{fn}')
        z.writestr('agent/.env', env)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name='ap-monitor-agent.zip')


# ── Remote ping agent endpoints ─────────────────────────────────────

@bp.route('/api/agent/targets')
def agent_targets():
    """The agent pulls its target list + ping config here. `?site=` picks a
    location (default site if absent, so the original Camp agent works unchanged);
    `?site=all` returns every location's devices for a single all-locations agent.
    Reading this consumes the check-requested flag(s) so a forced check fires once."""
    site_param = request.args.get('site')
    all_sites = site_param == 'all'
    if all_sites:
        if not _agent_authorized_all():
            return jsonify({'error': 'unauthorized'}), 401
        all_devices = current_app.config['DEVICES']
        all_switches = current_app.config['SWITCHES']
        # Consume every site's check flag. In-memory (no DB touch on the agent's ~20s
        # poll). List comprehension (not a generator) so every site's flag is consumed,
        # not short-circuited. If ANY site wants a full sweep, ping everything (a full
        # sweep covers the targeted IPs); otherwise collect the targeted IPs.
        consumed = [db.consume_check(key) for key in current_app.config['SITE_KEYS']]
        any_full = any(full for full, _ in consumed)
        check_ips = [] if any_full else sorted({ip for _, ips in consumed for ip in ips})
        check_requested = any_full or bool(check_ips)
    else:
        site = _current_site()
        if not _agent_authorized(site):
            return jsonify({'error': 'unauthorized'}), 401
        all_devices, all_switches = _site_nodes(site)
        full, check_ips = db.consume_check(site)
        check_requested = full or bool(check_ips)

    switches = [s for s in all_switches if s.get('enabled', True)]
    devices = [d for d in all_devices if d.get('enabled', True)]
    polling = current_app.config['SETTINGS'].get('polling', {})

    return jsonify({
        # `host` is what the agent actually PINGS (the DNS hostname when set, else the
        # IP); `ip` stays the identity key it reports results under. Older agents ignore
        # `host` and ping `ip` — so they keep working, just by IP.
        'switches': [
            {'name': s['name'], 'ip': s['ip'], 'host': s.get('hostname') or s['ip'],
             'location': s.get('location', ''), 'site': s.get('site')}
            for s in switches
        ],
        'devices': [
            {'name': d['name'], 'ip': d['ip'], 'host': d.get('hostname') or d['ip'],
             'location': d.get('location', ''), 'switch': d.get('switch'), 'site': d.get('site')}
            for d in devices
        ],
        'polling': {
            'ping_count': db.get_setting('ping_count'),
            'ping_timeout': db.get_setting('ping_timeout'),
        },
        'check_requested': check_requested,
        # When non-empty, the agent should ping ONLY these device IPs (a targeted
        # single-device check) instead of the whole list. Empty → full sweep. Old
        # agents ignore this field and just do a full sweep on check_requested.
        'check_ips': check_ips,
    })


@bp.route('/api/agent/report', methods=['POST'])
def agent_report():
    """The agent POSTs ping results here. We record them and fire Slack per site.
    `?site=all` groups results by each device's location (mapped by IP) and alerts
    each location independently."""
    site_param = request.args.get('site')
    data = request.get_json(silent=True) or {}
    results = data.get('results', [])
    if not isinstance(results, list):
        return jsonify({'error': 'results must be a list'}), 400
    settings = current_app.config['SETTINGS']
    # Which agent sent this report (informational; stored on the transition rows it
    # records). Trimmed to a sane length; the agent sets it via AGENT_NAME.
    reported_by = (request.headers.get('X-Agent-Name') or '').strip()[:80] or None
    _purge_due_deletions(current_app._get_current_object())   # lazy scheduler tick

    if site_param == 'all':
        if not _agent_authorized_all():
            return jsonify({'error': 'unauthorized'}), 401
        # Map each device IP to its site, group results, and process per site.
        default = current_app.config['DEFAULT_SITE']
        ip_site = {d['ip']: d.get('site', default) for d in current_app.config['DEVICES']}
        ip_site.update({s['ip']: s.get('site', default) for s in current_app.config['SWITCHES']})
        by_site = {}
        for r in results:
            s = ip_site.get(r.get('ip'))
            if s:
                by_site.setdefault(s, []).append(r)
        recorded, changed = 0, False
        for s, rs in by_site.items():
            devices, switches = _site_nodes(s)
            summary = monitor.process_results(rs, devices, switches, settings, site=s,
                                              reported_by=reported_by)
            recorded += summary['recorded']
            changed = changed or summary['changed']
        if changed:
            invalidate_outage_cache()   # mark dirty; a viewer's poll recomputes
        return jsonify({'ok': True, 'recorded': recorded, 'changed': changed})

    site = _current_site()
    if not _agent_authorized(site):
        return jsonify({'error': 'unauthorized'}), 401
    devices, switches = _site_nodes(site)
    summary = monitor.process_results(results, devices, switches, settings, site=site,
                                      reported_by=reported_by)
    if summary.get('changed'):
        invalidate_outage_cache()       # mark dirty; a viewer's poll recomputes
    return jsonify({'ok': True, **summary})


@bp.route('/api/stats')
def get_stats():
    devices, switches = _site_nodes(_current_site())
    all_ips  = [d['ip'] for d in devices] + [s['ip'] for s in switches]
    states   = db.get_all_states(all_ips)
    total = len(all_ips)
    up    = sum(1 for s in states.values() if s['current_status'] == 'up')
    down  = sum(1 for s in states.values() if s['current_status'] == 'down')
    return jsonify({'total': total, 'up': up, 'down': down, 'unknown': total - up - down})


@bp.route('/api/settings', methods=['GET', 'PUT'])
def app_settings():
    """The editable tuning knobs shown in the settings panel. GET returns the current
    values; PUT applies + persists a partial update (login-gated, CSRF-guarded)."""
    if request.method == 'GET':
        return jsonify(db.get_all_settings())
    updated = db.set_app_settings(request.get_json(silent=True) or {})
    invalidate_outage_cache()          # frequent window/min may have changed
    monitor.wake_watchdog()            # pick up a new stale threshold promptly
    g.audit_settings = True
    return jsonify(updated)
