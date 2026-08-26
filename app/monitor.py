import os
import subprocess
import platform
import time
import logging
import threading
import hashlib
from datetime import datetime

from . import database as db
from . import notifier

log = logging.getLogger(__name__)


def _invalidate_outage_cache():
    """Mark the routes-layer frequent-outage / ongoing-event caches stale from the
    watchdog thread. Lazy import avoids a circular import at module load (routes
    imports monitor); by the time the watchdog runs, routes is fully loaded."""
    try:
        from .routes import invalidate_outage_cache
        invalidate_outage_cache()
    except Exception:
        pass


_scheduler_thread = None
_stop_event = threading.Event()

# app_flags key recording the kind ('down' | 'allclear') of the last Slack
# message actually sent, so we don't send consecutive all-clear messages.
LAST_SLACK_KIND_FLAG = 'last_slack_kind'

# Sites whose post-startup baseline has been established. The FIRST agent report after
# a (re)start only RECORDS the current state — it does not alert. This prevents a
# restart from re-firing "currently down" messages for devices that were already down
# before the restart: the in-memory `_states` cache is re-seeded from `device_states`
# at startup, so if that persisted state is stale (e.g. writes had failed), the first
# report makes still-down devices look like brand-new transitions and would re-alert.
_baseline_ready: set[str] = set()

# ── Agent-silence watchdog ──────────────────────────────────────────
# A tiny always-on daemon thread (safe on a non-sleeping Basic+ dyno) that
# alerts once when a site's agent stops reporting, and once when it resumes.
# Detects the outage WHILE it's happening — unlike everything else, which only
# runs when an agent actually POSTs.
_watchdog_thread = None
_watchdog_stop = threading.Event()
# Set to interrupt the between-tick sleep so the loop re-evaluates its cadence
# immediately — e.g. right after a pause is created/cancelled (see wake_watchdog).
_watchdog_wake = threading.Event()
WATCHDOG_STALE_MINUTES = int(os.environ.get('WATCHDOG_STALE_MINUTES', '30'))
# Base cadence: 8h. The watchdog's checks are all in-memory in the common case (no DB
# query), so a slow tick is cheap; ticking rarely avoids any needless background work.
WATCHDOG_CHECK_SECONDS = int(os.environ.get('WATCHDOG_CHECK_SECONDS', str(8 * 3600)))
# When a scheduled-pause boundary (start/end) falls within the base window, tighten to
# this cadence so the sweep applies/resumes the pause on time.
WATCHDOG_NEAR_PAUSE_SECONDS = int(os.environ.get('WATCHDOG_NEAR_PAUSE_SECONDS', str(30 * 60)))


def start_watchdog(app):
    """Start the always-on background thread: agent-silence + DB-writable alerts and
    the scheduled-pause sweep. Idempotent. Takes the Flask `app` so the sweep can
    refresh the device list in `app.config` after flipping `enabled` flags."""
    global _watchdog_thread
    if _watchdog_thread and _watchdog_thread.is_alive():
        return
    _watchdog_stop.clear()
    _watchdog_wake.clear()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, args=(app,), daemon=True, name='watchdog')
    _watchdog_thread.start()
    log.info("Watchdog started (base tick %ds; %ds when a pause is within the base window; "
             "agent-silence alert > %d min)",
             WATCHDOG_CHECK_SECONDS, WATCHDOG_NEAR_PAUSE_SECONDS, WATCHDOG_STALE_MINUTES)


def stop_watchdog():
    _watchdog_stop.set()
    _watchdog_wake.set()        # break the sleep so the loop can exit promptly


def wake_watchdog():
    """Interrupt the watchdog's sleep so it re-runs its checks and recomputes its tick
    cadence now. Called when a pause is created/cancelled so a new near-term pause can
    immediately tighten the cadence (rather than waiting up to a full base tick)."""
    _watchdog_wake.set()


def _next_watchdog_interval() -> int:
    """Base cadence, tightened when a scheduled-pause boundary is within the base
    window (so we catch a pause's start/end on time). Purely in-memory."""
    secs = db.seconds_until_next_pause_boundary()
    if secs is not None and secs <= WATCHDOG_CHECK_SECONDS:
        return WATCHDOG_NEAR_PAUSE_SECONDS
    return WATCHDOG_CHECK_SECONDS


def _watchdog_loop(app):
    if _watchdog_stop.wait(30):        # let the app finish starting up
        return
    while not _watchdog_stop.is_set():
        try:
            apply_scheduled_pauses(app)
        except Exception as e:
            log.error("Scheduled-pause sweep error: %s", e)
        try:
            check_agent_silence(app)
        except Exception as e:
            log.error("Watchdog check error: %s", e)
        try:
            check_db_writable(app)
        except Exception as e:
            log.error("Watchdog DB-write check error: %s", e)
        # Sleep until the next tick, but wake early if a pause change asks us to
        # re-evaluate (wake_watchdog) or we're asked to stop.
        _watchdog_wake.wait(_next_watchdog_interval())
        _watchdog_wake.clear()


def apply_scheduled_pauses(app):
    """Flip device `enabled` flags at scheduled-pause boundaries: pause a window's
    devices when it starts, resume them when it ends (unless another active window
    still covers them). Reloads app.config so the agent's target list reflects the
    change on its next poll. Catches up if the app was down across a boundary."""
    # Skip the DB entirely unless an open pause has actually reached its next
    # boundary (activate/resume). Both checks are in-memory, so a quiet fleet — or a
    # long-running active pause between its start and end — issues no DB query here,
    # letting Neon's compute suspend.
    if not db.pause_sweep_due():
        return
    now = datetime.utcnow()
    now_iso = now.isoformat()
    changed = False

    for sp in db.due_scheduled_pause_starts(now_iso):
        # If the whole window already elapsed (app was down through it), don't
        # pause now — just mark it done.
        if _iso_to_dt(sp['end_at']) <= now:
            db.set_scheduled_pause_status(sp['id'], 'done')
            continue
        mode = sp.get('mode') or 'monitoring'
        for ip in sp['device_ips']:
            db.set_device_pause(ip, mode)
        db.set_scheduled_pause_status(sp['id'], 'active')
        changed = True
        log.info("Scheduled pause '%s' started — paused %d device(s)",
                 sp.get('name') or sp['id'], len(sp['device_ips']))

    for sp in db.due_scheduled_pause_ends(now_iso):
        still_paused = db.active_pause_ips(exclude_id=sp['id'])
        for ip in sp['device_ips']:
            if ip not in still_paused:
                db.set_node_enabled(ip, True)
        db.set_scheduled_pause_status(sp['id'], 'done')
        # If this pause belongs to an event, cap that event's still-ongoing parts for
        # these devices at the pause's end — so an event kept "ongoing" only by its
        # monitoring pause(s) ends once the pause(s) end. Other devices still open for
        # a different reason keep the event ongoing.
        if sp.get('event_group_id'):
            fully_ended = db.end_event_open_devices(
                sp['event_group_id'], sp['device_ips'], sp['end_at'])
            _invalidate_outage_cache()   # ongoing-event overlay changed
            if fully_ended:
                log.info("Event %s ended — its only remaining ongoing parts were its "
                         "now-ended pause(s)", sp['event_group_id'])
        changed = True
        log.info("Scheduled pause '%s' ended — resumed devices", sp.get('name') or sp['id'])

    if changed:
        nodes = db.get_nodes()
        app.config['DEVICES'] = nodes['devices']
        app.config['SWITCHES'] = nodes['switches']
    db.refresh_open_pauses()          # recompute the cached flag (some may be done)


def _iso_to_dt(v):
    return datetime.fromisoformat(v) if isinstance(v, str) else v


# Per-site "silence already alerted" flag. Kept in memory (so a healthy fleet's
# watchdog needn't read the DB every tick) but WRITE-THROUGH to app_flags and
# SEEDED from it at startup — so a restart mid-outage still knows it already sent a
# "down" notice and the "resumed" message isn't swallowed.
_silence_alerted: dict[str, bool] = {}
_SILENCE_FLAG = 'silence_alerted'   # per-site app_flags key prefix


def _silence_flag_key(site: str) -> str:
    return f'{_SILENCE_FLAG}:{site}'


def load_silence_alerted(app):
    """Seed the in-memory silence-alerted flags from the DB. Called once at startup so
    a restart during an agent-silence outage still delivers the recovery message."""
    for s in app.config['SITES']:
        try:
            if db.get_flag(_silence_flag_key(s['key'])) == '1':
                _silence_alerted[s['key']] = True
        except Exception:       # noqa: BLE001
            log.warning("Could not load silence-alerted flag for %s", s['key'])


def _persist_silence(site: str, alerted: bool):
    """Best-effort write-through of the silence-alerted flag (a DB blip must not break
    the watchdog — the in-memory flag is already updated by the caller)."""
    try:
        db.set_flag(_silence_flag_key(site), '1' if alerted else '0')
    except Exception:           # noqa: BLE001
        log.warning("Could not persist silence-alerted flag for %s", site)


def check_agent_silence(app):
    """For each site that has ever reported (in-memory watermark), compare it to
    now; alert once when it crosses the stale threshold and once when it recovers.
    Reads sites + watermark from memory so a quiet, healthy fleet doesn't wake the
    DB every tick; the alerted flag is persisted (see above) only when it flips."""
    settings = app.config['SETTINGS']
    now = datetime.utcnow()
    threshold = db.get_setting('watchdog_stale_minutes') * 60
    for s in app.config['SITES']:
        key, name = s['key'], s['name']
        last = db.get_last_report(key)
        if not last:
            continue                    # no agent has reported since startup
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            continue
        stale = (now - last_dt).total_seconds() > threshold
        alerted = _silence_alerted.get(key, False)
        if stale and not alerted:
            minutes = int((now - last_dt).total_seconds() // 60)
            if notifier.send_monitoring_down_notice(settings, name, minutes, site=key):
                _silence_alerted[key] = True
                _persist_silence(key, True)
        elif not stale and alerted:
            notifier.send_monitoring_recovered_notice(settings, name, site=key)
            _silence_alerted[key] = False
            _persist_silence(key, False)


# One-shot "DB write failure already alerted" flag (in memory), so the watchdog
# Slacks once when writes start failing and once when they recover.
_db_unwritable_alerted = False
# Only alert once writes have been failing for a sustained stretch (not a single
# cold-start blip that the next report retries away).
DB_WRITE_FAIL_ALERT_THRESHOLD = int(os.environ.get('DB_WRITE_FAIL_ALERT_THRESHOLD', '3'))


def check_db_writable(app):
    """Alert once when the database has been rejecting transition writes (statuses are
    still tracked live in memory, but nothing is being persisted), and once when it
    recovers. This is the safety net for the failure mode where the DB is reachable
    for reads but read-only/unwritable — otherwise monitoring would look fine while
    silently saving nothing."""
    global _db_unwritable_alerted
    fails, err = db.db_write_health()
    settings = app.config['SETTINGS']
    if fails >= DB_WRITE_FAIL_ALERT_THRESHOLD and not _db_unwritable_alerted:
        if notifier.send_db_unwritable_notice(settings, fails, err):
            _db_unwritable_alerted = True
    elif fails == 0 and _db_unwritable_alerted:
        notifier.send_db_writable_notice(settings)
        _db_unwritable_alerted = False


def ping_host(ip: str, count: int = 3, timeout: int = 5) -> tuple[bool, float | None]:
    system = platform.system().lower()
    if system == 'windows':
        cmd = ['ping', '-n', str(count), '-w', str(timeout * 1000), ip]
    else:
        cmd = ['ping', '-c', str(count), '-W', str(timeout), ip]

    try:
        start = time.monotonic()
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout * count + 5
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        # Windows `ping` exits 0 even for an unreachable host (a router replies
        # "Destination host unreachable"), so exit code alone reads down as up. A
        # real echo reply always contains "TTL="; require it on Windows.
        if system == 'windows':
            up = result.returncode == 0 and 'ttl=' in (result.stdout or '').lower()
        else:
            up = result.returncode == 0
        if up:
            return True, round(elapsed_ms / count, 2)
        return False, None
    except subprocess.TimeoutExpired:
        return False, None
    except Exception as e:
        log.warning("Ping error for %s: %s", ip, e)
        return False, None


def process_results(results: list[dict], devices: list[dict], switches: list[dict],
                    settings: dict, site: str | None = None,
                    reported_by: str | None = None) -> dict:
    """Record a batch of ping results and send ONE Slack message if any device
    changed state. `results` is a list of {ip, status, response_ms} dicts —
    produced either by the in-process scheduler (check_all_devices) or POSTed by
    the remote ping agent (/api/agent/report). `site` scopes the all-clear flag
    and labels Slack messages, so each location alerts independently.
    """
    # Per-site Slack "last message kind" flag + display name for message labels.
    flag_key = LAST_SLACK_KIND_FLAG + (f':{site}' if site else '')
    site_name = None
    if site:
        site_name = next((s['name'] for s in settings.get('sites', []) if s.get('key') == site), site)

    all_targets = list(switches) + list(devices)
    switch_ips = {s['ip'] for s in switches}
    target_by_ip = {t['ip']: t for t in all_targets}

    # Require this many consecutive down checks before a device counts as down
    # (immediate recovery). Keeps alerts flap-free with one or many agents. Editable
    # live in the settings panel (seeded from the DOWN_CONFIRM_CHECKS env var).
    confirm_checks = db.get_setting('down_confirm_checks')

    # Diagnostic: how many targets the agent reported down at the RAW level this
    # cycle. If this is regularly non-zero but no downtime shows in the log, the
    # confirm-down debounce is filtering them (vs. the agent seeing everything up).
    raw_down = sum(1 for r in results if r.get('status') == 'down')
    if raw_down:
        log.info("process_results[%s]: %d/%d targets reported down (raw), confirm_checks=%d",
                 site or '-', raw_down, len(results), confirm_checks)

    any_changed = False
    changed_ips = []
    recorded = 0

    for r in results:
        ip = r.get('ip')
        if ip is None:
            continue
        status = r.get('status', 'down')
        response_ms = r.get('response_ms')
        changed, _ = db.record_ping(ip, status, response_ms, confirm_checks=confirm_checks,
                                    reported_by=reported_by)
        recorded += 1
        if changed:
            any_changed = True
            changed_ips.append(ip)

    # Refresh this site's "last report" watermark; record_report writes an
    # app_downtime interval only if the gap since the previous report shows the
    # monitoring pipeline had gone quiet (so no per-cycle rows accumulate).
    if recorded:
        sites = settings.get('sites') or []
        hb_site = site or (sites[0]['key'] if sites else 'default')
        db.record_report(hb_site)

    # Suppress all alerting on the FIRST report per site since startup — that report
    # just re-establishes the baseline. Otherwise a restart (or a redeploy) would
    # re-alert every currently-down device as if it were a new outage (see
    # _baseline_ready), which is exactly the "identical Slack message repeats after a
    # restart" symptom.
    site_key = site or 'default'
    first_report = site_key not in _baseline_ready
    _baseline_ready.add(site_key)
    if first_report:
        log.info("process_results[%s]: first report since startup — recording baseline "
                 "(new down alerts suppressed; inaugural 'all up' / recovery still sent)", site_key)

    # Decide what (if anything) to Slack. This runs when a device changed OR on the
    # first report per site since startup (so a channel gets an inaugural "all up",
    # and a recovery that landed mid-restart isn't lost).
    if any_changed or first_report:
        all_ips = list(target_by_ip.keys())
        states = db.get_all_states(all_ips)

        # A device is "alertable" only when it's enabled AND its notifications
        # aren't paused (notify). Notifications-paused devices are still monitored/
        # recorded, just excluded from Slack.
        def _alertable(ip):
            t = target_by_ip[ip]
            return t.get('enabled', True) and t.get('notify', True)

        down_switches = [
            target_by_ip[ip] for ip in all_ips
            if ip in switch_ips and _alertable(ip)
            and states.get(ip, {}).get('current_status') == 'down'
        ]
        down_devices = [
            target_by_ip[ip] for ip in all_ips
            if ip not in switch_ips and _alertable(ip)
            and states.get(ip, {}).get('current_status') == 'down'
        ]
        total_down = len(down_switches) + len(down_devices)

        # The alertable devices that flipped this cycle (name + new status), so the
        # message shows the deltas. Empty on a pure inaugural (nothing changed).
        changes = [(target_by_ip[ip], states.get(ip, {}).get('current_status'))
                   for ip in changed_ips if ip in target_by_ip and target_by_ip[ip].get('notify', True)]

        # The last Slack message kind we actually sent (persisted so it survives
        # restarts). 'allclear' means we've already told this channel everything's up.
        last_kind = db.get_flag(flag_key)

        # Detect whether THIS site's Slack channel is new/changed since we last posted,
        # by comparing a hash of the resolved webhook to what we stored. A changed (or
        # never-seen) channel gets an INAUGURAL "all up" even if the flag already says
        # 'allclear' on the old channel — this is what makes a freshly-wired channel say
        # hello. Only a hash is stored (never the webhook itself). Costs one flag read,
        # but only on a change/first-report cycle — never in steady state.
        webhook = notifier._webhook_for(settings.get('slack', {}), site)
        chan_key = flag_key + ':chan'
        chan_id = hashlib.sha256(webhook.encode('utf-8')).hexdigest()[:16] if webhook else ''
        channel_new = bool(chan_id) and db.get_flag(chan_key) != chan_id

        if total_down == 0:
            # Everything up → an "All Devices Online" message. Fires on (a) a genuine
            # recovery, (b) the first report for a channel, or (c) a newly-wired channel.
            # `last_kind != 'allclear'` keeps it a one-time hello per channel (no repeat
            # on steady-state restarts); a new channel overrides that guard.
            trigger = changes or first_report or channel_new
            allowed = last_kind != 'allclear' or channel_new
            if trigger and allowed and notifier.send_slack_update(
                    settings, [], [], site_name=site_name, changes=changes, site=site):
                db.set_flag(flag_key, 'allclear')
                if chan_id:
                    db.set_flag(chan_key, chan_id)
        elif first_report:
            # Devices are still down on the first report after a restart — don't re-alert
            # them as fresh outages, but record 'down' so a LATER recovery still fires
            # the all-clear above.
            db.set_flag(flag_key, 'down')
        elif changes and notifier.send_slack_update(settings, down_switches, down_devices,
                                                    site_name=site_name, changes=changes, site=site):
            db.set_flag(flag_key, 'down')
            if chan_id:
                db.set_flag(chan_key, chan_id)

    # One-time "still down" escalation: once a device has been down for
    # reminder_hours (default 6), send a single louder reminder, then stay quiet
    # for that outage. Checked every cycle (independent of state changes).
    reminder_hours = db.get_setting('reminder_hours')
    if reminder_hours and not first_report:
        overdue = [ip for ip in db.get_overdue_down(reminder_hours)
                   if ip in target_by_ip and target_by_ip[ip].get('enabled', True)]
        if overdue:
            rem_switches = [target_by_ip[ip] for ip in overdue if ip in switch_ips]
            rem_devices  = [target_by_ip[ip] for ip in overdue if ip not in switch_ips]
            if notifier.send_slack_reminder(settings, rem_switches, rem_devices, reminder_hours,
                                            site_name=site_name, site=site):
                db.mark_reminded(overdue)

    return {'recorded': recorded, 'changed': any_changed}


def check_all_devices(devices: list[dict], switches: list[dict], settings: dict):
    # Skip paused (disabled) devices — they aren't pinged off-season/maintenance.
    all_targets = [t for t in (list(switches) + list(devices)) if t.get('enabled', True)]

    log.info("Checking %d switches + %d APs at %s",
             len(switches), len(devices), datetime.now().strftime('%H:%M:%S'))

    ping_cfg = settings.get('polling', {})
    count = ping_cfg.get('ping_count', 3)
    timeout = ping_cfg.get('ping_timeout', 5)

    results = []
    for target in all_targets:
        ip = target['ip']
        name = target['name']

        success, response_ms = ping_host(ip, count=count, timeout=timeout)
        status = 'up' if success else 'down'
        results.append({'ip': ip, 'status': status, 'response_ms': response_ms})

        log.info("  %-40s %s%s", name, status,
                 f" ({response_ms:.0f}ms)" if response_ms else "")

    process_results(results, devices, switches, settings)


def _scheduler_loop(devices: list[dict], switches: list[dict], settings: dict, interval_seconds: int):
    while not _stop_event.is_set():
        try:
            check_all_devices(devices, switches, settings)
        except Exception as e:
            log.error("Error during device check: %s", e)
        _stop_event.wait(timeout=interval_seconds)


def start_scheduler(devices: list[dict], switches: list[dict], settings: dict):
    global _scheduler_thread
    interval_min = settings.get('polling', {}).get('interval_minutes', 20)
    interval_sec = interval_min * 60

    log.info("Scheduler starting — interval: %d min, %d targets",
             interval_min, len(devices) + len(switches))

    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(devices, switches, settings, interval_sec),
        daemon=True,
        name='ap-monitor-scheduler'
    )
    _scheduler_thread.start()


def stop_scheduler():
    _stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive():
        _scheduler_thread.join(timeout=10)


def run_check_now(devices: list[dict], switches: list[dict], settings: dict):
    thread = threading.Thread(
        target=check_all_devices,
        args=(devices, switches, settings),
        daemon=True,
        name='ap-monitor-manual'
    )
    thread.start()
