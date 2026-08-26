#!/usr/bin/env python3
"""AP Monitor — remote ping agent.

Runs on a Windows Server that can reach the private device network
addresses (the only place that can). It is fully stateless: it pulls the target
list and ping config from the server, pings every target locally, and POSTs the
results back. All state-change detection and Slack alerting live on the server
(see app/routes.py /api/agent/report).

stdlib only — no pip install needed beyond Python 3. See agent/README.md for the
Windows Task Scheduler (SYSTEM account) setup.

Config via environment variables (see .env.example):
  SERVER_URL          base URL of the Heroku app, e.g. https://your-app.herokuapp.com
  AGENT_TOKEN         shared secret; for SITE=all use the master AGENT_TOKEN
  SITE                which location(s) this agent serves ("all" = every location)
  POLL_INTERVAL       seconds between full ping sweeps (default 60)
  FLAG_POLL_INTERVAL  seconds between target/flag polls (default 20)
  PING_WORKERS        max concurrent pings per sweep (default 40)
  AGENT_NAME          friendly name for this agent, recorded on the outages it
                      reports (default: this machine's hostname)
"""
import concurrent.futures
import json
import logging
import logging.handlers
import os
import platform
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

# Log to the console always. If LOG_FILE is set (recommended on Windows, where
# there's no journald to cap the log), ALSO write to a size-capped, auto-rotating
# file so it can't grow forever: when it reaches LOG_MAX_MB it rolls over and only
# LOG_BACKUPS old files are kept (default 5 MB × 3 = 20 MB max on disk).
_handlers = [logging.StreamHandler()]
_log_file = os.environ.get('LOG_FILE')
if _log_file:
    _handlers.append(logging.handlers.RotatingFileHandler(
        _log_file,
        maxBytes=int(float(os.environ.get('LOG_MAX_MB', '5')) * 1024 * 1024),
        backupCount=int(os.environ.get('LOG_BACKUPS', '3')),
        encoding='utf-8',
    ))
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    handlers=_handlers,
)
log = logging.getLogger('ping-agent')

# Agent version — bump this whenever ping_agent.py changes so the startup log tells
# you at a glance which build is running after a redeploy. (Server-independent; just
# for the agent host.)
AGENT_VERSION = '1.2.0'

SERVER_URL = os.environ.get('SERVER_URL', 'http://127.0.0.1:5000').rstrip('/')
AGENT_TOKEN = os.environ.get('AGENT_TOKEN', '')
# Which location this agent serves. Defaults to the first location (location-a).

SITE = os.environ.get('SITE', 'location-a')
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '60'))
FLAG_POLL_INTERVAL = int(os.environ.get('FLAG_POLL_INTERVAL', '20'))
# Devices are pinged concurrently (a thread pool), so a sweep finishes in ~one ping
# timeout instead of summing every host's ping time — down hosts (which each block for
# the full timeout) no longer serialize the sweep. Bounded so we don't spawn a thread
# per device on a huge fleet; capped to the target count at run time.
MAX_PING_WORKERS = int(os.environ.get('PING_WORKERS', '40'))
# A friendly name for THIS agent (e.g. windows-server, raspberry-pi). Sent with each
# report so the server can record which agent observed a given outage/recovery.
# Defaults to the machine's hostname if unset.
AGENT_NAME = os.environ.get('AGENT_NAME') or platform.node() or ''

_SITE_Q = '?site=' + urllib.parse.quote(SITE)


def ping_host(ip: str, count: int = 3, timeout: int = 5) -> tuple[bool, float | None]:
    """Ping a host via the OS ping binary. Copied verbatim from app/monitor.py
    so behavior matches the original on-server implementation."""
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
        # Windows `ping` exits 0 even when a router answers "Destination host
        # unreachable" (e.g. an address this host has no route to) — so exit code
        # alone reads a DOWN host as UP. A genuine echo reply always contains
        # "TTL="; unreachable / timed-out replies never do. Require it on Windows.
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


def _headers() -> dict:
    h = {'Content-Type': 'application/json'}
    if AGENT_TOKEN:
        h['X-Agent-Token'] = AGENT_TOKEN
    if AGENT_NAME:
        h['X-Agent-Name'] = AGENT_NAME
    return h


def fetch_targets() -> dict | None:
    req = urllib.request.Request(
        f'{SERVER_URL}/api/agent/targets{_SITE_Q}', headers=_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.error("targets HTTP %s: %s", e.code, e.read().decode()[:200])
    except Exception as e:
        log.error("Failed to fetch targets: %s", e)
    return None


def post_results(results: list[dict]) -> bool:
    data = json.dumps({'results': results}).encode()
    req = urllib.request.Request(
        f'{SERVER_URL}/api/agent/report{_SITE_Q}', data=data,
        headers=_headers(), method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        log.info("Reported %d results (changed=%s)",
                 body.get('recorded', 0), body.get('changed'))
        return True
    except urllib.error.HTTPError as e:
        log.error("report HTTP %s: %s", e.code, e.read().decode()[:200])
    except Exception as e:
        log.error("Failed to post results: %s", e)
    return False


def run_sweep(targets: dict, only_ips=None):
    """Ping targets and report the results. `only_ips` (a set/list of IPs) restricts the
    sweep to just those devices — used for a targeted single-device "Check now" so the
    agent pings and reports only that device instead of the whole list."""
    polling = targets.get('polling', {})
    count = polling.get('ping_count', 3)
    timeout = polling.get('ping_timeout', 5)

    all_targets = (targets.get('switches', []) or []) + (targets.get('devices', []) or [])
    if only_ips:
        wanted = set(only_ips)
        all_targets = [t for t in all_targets if t.get('ip') in wanted]
    if not all_targets:
        return

    workers = max(1, min(MAX_PING_WORKERS, len(all_targets)))
    if only_ips:
        log.info("Targeted check: pinging %d device(s) (count=%d, timeout=%ds, workers=%d)",
                 len(all_targets), count, timeout, workers)
    else:
        log.info("Pinging %d targets (count=%d, timeout=%ds, workers=%d)",
                 len(all_targets), count, timeout, workers)

    def check(t):
        ip = t['ip']
        # Ping the DNS hostname when the server provides one (`host`), else the IP.
        # Either way the result is reported under `ip` — the server's identity key.
        target = t.get('host') or ip
        try:
            success, response_ms = ping_host(target, count=count, timeout=timeout)
        except Exception as e:
            # A failing ping (bad host, OS hiccup) counts the host as down rather than
            # aborting the whole sweep.
            log.warning("ping %s (%s) failed: %s", target, ip, e)
            success, response_ms = False, None
        return {'ip': ip, 'status': 'up' if success else 'down', 'response_ms': response_ms}

    # Ping all targets concurrently; the sweep is bounded by the slowest single ping
    # (~timeout for a down host), not the sum across hosts.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(check, all_targets))

    if results:
        post_results(results)


def main():
    log.info("Agent v%s starting — server=%s, site=%s, interval=%ds, flag-poll=%ds%s",
             AGENT_VERSION, SERVER_URL, SITE, POLL_INTERVAL, FLAG_POLL_INTERVAL,
             "" if AGENT_TOKEN else " (no AGENT_TOKEN set!)")

    last_sweep = 0.0
    while True:
        try:
            targets = fetch_targets()
            if targets is not None:
                now = time.monotonic()
                due = (now - last_sweep) >= POLL_INTERVAL
                forced = targets.get('check_requested', False)
                check_ips = targets.get('check_ips') or []
                if forced and check_ips:
                    # Targeted single-device check: ping only those devices and report
                    # them, WITHOUT resetting the full-sweep cadence.
                    log.info("Targeted check requested by server for %d device(s)", len(check_ips))
                    run_sweep(targets, only_ips=check_ips)
                if due or (forced and not check_ips):
                    if forced and not check_ips:
                        log.info("Forced full check requested by server")
                    run_sweep(targets)
                    last_sweep = time.monotonic()
        except Exception as e:
            # Never let one bad cycle kill the loop.
            log.error("Cycle error: %s", e)

        time.sleep(FLAG_POLL_INTERVAL)


if __name__ == '__main__':
    main()
