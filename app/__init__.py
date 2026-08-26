import logging
import os
import yaml
from flask import Flask, request

from . import database as db
from . import monitor


def load_yaml(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_optional_yaml(path: str) -> dict:
    """load_yaml for a file the app can run without. Unlike settings/devices,
    a missing or malformed basemap config must degrade to "no imagery" rather
    than crash create_app()."""
    if not os.path.exists(path):
        return {}
    try:
        return load_yaml(path) or {}
    except Exception:
        logging.getLogger(__name__).exception(
            "Could not read %s — continuing without it.", path
        )
        return {}


def create_app():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    base_dir = os.path.dirname(os.path.dirname(__file__))
    config_dir = os.path.join(base_dir, 'config')

    settings = load_yaml(os.path.join(config_dir, 'settings.yaml'))

    # Allow env vars (e.g. Heroku config vars) to override secrets/cadence so
    # they don't have to live in the committed YAML.
    if os.environ.get('SLACK_WEBHOOK_URL'):
        settings.setdefault('slack', {})['webhook_url'] = os.environ['SLACK_WEBHOOK_URL']
    # Per-location Slack routing: SLACK_WEBHOOK_URL_<KEY> (key upper-cased, dashes →
    # underscores) sends that location's outage/agent alerts to its own channel. The
    # default location — and app-wide alerts (DB write-failure) — use SLACK_WEBHOOK_URL.
    # e.g. SLACK_WEBHOOK_URL_LOCATION_A posts Location A alerts to its channel while
    # the default location keeps the plain SLACK_WEBHOOK_URL.
    _site_webhooks = {}
    _prefix = 'SLACK_WEBHOOK_URL_'
    for _k, _v in os.environ.items():
        if _k.startswith(_prefix) and _v:
            _site_webhooks[_k[len(_prefix):].lower().replace('_', '-')] = _v
    if _site_webhooks:
        settings.setdefault('slack', {})['webhooks'] = _site_webhooks
    if os.environ.get('POLLING_INTERVAL_MINUTES'):
        settings.setdefault('polling', {})['interval_minutes'] = int(
            os.environ['POLLING_INTERVAL_MINUTES']
        )
    # Public base URL of the app (e.g. https://monitor.example.com), used to
    # deep-link each device in Slack outage alerts to its view page. Optional — if
    # unset, alerts fall back to showing the plain hostname/IP (no link).
    _app_url = os.environ.get('APP_BASE_URL', '').strip().rstrip('/')
    if _app_url:
        settings['app_url'] = _app_url

    # The device/switch list is the source of truth in the DB (the `nodes`
    # table). On a fresh database we seed it once from the committed
    # devices.yaml; thereafter the UI's Add-Device flow writes straight to the
    # DB so additions survive Heroku dyno restarts. Editing devices.yaml after
    # the table is populated has no effect — reseed an empty table to re-import.
    db.init_db()

    # DEMO_DATA build: on a fresh DB, seed the whole fake fleet + rich history (two
    # locations, every device/event/pause/user-activity type) so the localhost demo
    # showcases every feature with no agent or real data. See app/demo_seed.py.
    if os.environ.get('DEMO_DATA') == '1' and db.nodes_count() == 0:
        from .demo_seed import seed_demo
        seed_demo()
        logging.getLogger(__name__).info("Seeded DEMO_DATA fleet + history (fake data).")
    else:
        # Locations live in the DB (the `sites` table). Seed once from settings.yaml,
        # then the UI's "Add location" flow writes straight to the DB.
        if db.sites_count() == 0:
            seed_sites = settings.get('sites') or [{'key': 'location-a', 'name': 'Location A'}]
            db.seed_sites(seed_sites)

        if db.nodes_count() == 0:
            devices_cfg = load_yaml(os.path.join(config_dir, 'devices.yaml')) or {}
            seed_devices = devices_cfg.get('devices', [])
            seed_switches = devices_cfg.get('switches', [])
            if seed_devices or seed_switches:
                db.seed_nodes_from_yaml(seed_devices, seed_switches)
                logging.getLogger(__name__).info(
                    "Seeded nodes table from devices.yaml (%d switches, %d devices).",
                    len(seed_switches), len(seed_devices),
                )

    # Load runtime state into memory (device statuses + whether any scheduled pause
    # exists) so steady-state operation barely touches the DB — see the cache note
    # in database.py. The DB stays a durable backing, written only on transitions.
    db.load_runtime_caches()
    db.load_app_settings()          # editable tuning knobs (UI settings panel)
    db.refresh_open_pauses()
    db.refresh_pending_deletions()

    nodes = db.get_nodes()
    devices = nodes['devices']
    switches = nodes['switches']

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, 'templates'),
        static_folder=os.path.join(base_dir, 'static'),
    )
    app.config['DEVICES'] = devices
    app.config['SWITCHES'] = switches
    app.config['SETTINGS'] = settings

    # Per-location aerial basemap (tile path + default visible window). Optional:
    # a location with no entry renders on the plain grid, as does every location
    # if the file is absent.
    app.config['BASEMAPS'] = load_optional_yaml(
        os.path.join(config_dir, 'basemaps.yaml')
    )

    # Locations ("sites") from the DB (source of truth). Each has its own device
    # list, map, and agent; the first is the default view.
    sites = db.get_sites() or [{'key': 'location-a', 'name': 'Location A'}]
    app.config['SITES'] = sites
    app.config['DEFAULT_SITE'] = sites[0]['key']
    app.config['SITE_KEYS'] = {s['key'] for s in sites}
    settings['sites'] = sites   # so Slack labels resolve names for new sites too

    # Session cookie signing key. Set SECRET_KEY as a Heroku config var so
    # login sessions survive restarts; without it we fall back to a random
    # per-process key (sessions drop on every restart) and warn.
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        secret = os.urandom(32).hex()
        logging.getLogger(__name__).warning(
            "SECRET_KEY not set — using an ephemeral key; logins will not "
            "survive a restart. Set SECRET_KEY as a config var in production."
        )
    app.secret_key = secret

    # Session-cookie hardening. SameSite=Lax lets the cookie ride the top-level
    # GET redirect back from Okta (needed for the OAuth state check); Secure is on
    # by default when SSO is enabled (Heroku serves HTTPS) — set
    # SESSION_COOKIE_SECURE=0 only for local HTTP testing.
    from . import auth
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SECURE'] = (
        os.environ.get('SESSION_COOKIE_SECURE', '1' if auth.okta_enabled() else '0') == '1'
    )
    auth.init_okta(app)

    from .routes import bp, seed_geo_positions
    app.register_blueprint(bp)

    # Give devices a starting position on the aerial photo (once per location) by
    # fitting their existing logical layout into the map area — so a location that
    # gains a basemap doesn't open with everything unplaced.
    seed_geo_positions(app)

    # Basemap tiles are immutable: the version segment in their path
    # (static/basemap/<site>/v<n>/) changes whenever the imagery is regenerated, so a
    # given URL always names the same bytes. Cache them hard — a fresh map view
    # requests dozens of tiles, and without this every one is a conditional request
    # back to the dyno. Registered app-level (not on the blueprint) because static
    # files are served by Flask's own `static` endpoint.
    @app.after_request
    def _cache_basemap_tiles(response):
        if request.path.startswith('/static/basemap/') and response.status_code == 200:
            # The manifest is the one mutable-ish file (regenerated in place at the
            # same version during development), so let it revalidate.
            if request.path.endswith('manifest.json'):
                response.headers['Cache-Control'] = 'public, max-age=300'
            else:
                response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    # Pinging normally happens on a remote agent (the Raspberry Pi) that POSTs
    # results to /api/agent/report. The in-process scheduler is only useful when
    # this app itself runs on the camp LAN — gate it behind ENABLE_LOCAL_PING so
    # it stays off on Heroku (which can't reach the private VLAN anyway).
    if os.environ.get('ENABLE_LOCAL_PING') == '1':
        monitor.start_scheduler(devices, switches, settings)
    else:
        logging.getLogger(__name__).info(
            "Local ping scheduler disabled (set ENABLE_LOCAL_PING=1 to run on-LAN). "
            "Expecting ping results from a remote agent via /api/agent/report."
        )

    # Always-on background thread: alerts to Slack if an agent goes silent, and
    # applies scheduled-pause windows. Safe on a non-sleeping dyno (Basic+).
    # Disable with ENABLE_WATCHDOG=0.
    if os.environ.get('ENABLE_WATCHDOG', '1') == '1':
        monitor.load_silence_alerted(app)   # survive a restart mid-agent-silence
        monitor.start_watchdog(app)

    return app
