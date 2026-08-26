import json
import logging
import urllib.request
import urllib.error
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import database as db

log = logging.getLogger(__name__)

# Slack timestamps render in US Eastern (tracks EST/EDT automatically) so they
# match the dashboard. On Heroku the server clock is UTC, so format explicitly.
_EASTERN = ZoneInfo('America/New_York')


def _label(node: dict) -> str:
    """Human-facing device label for Slack: the friendly Name (the `location`
    field), falling back to the unique Device ID (`name`) when no Name is set."""
    return (node.get('location') or '').strip() or node['name']


def _addr(node: dict) -> str:
    """Address shown after the label in Slack: the DNS hostname when the device has
    one (that's what's actually pinged), else its IP."""
    return (node.get('hostname') or '').strip() or node['ip']


def _device_url(app_url: str, site: str | None, node: dict) -> str:
    """Deep link to a device's view page in the dashboard (opens its detail panel).
    Keyed by the stable Device ID (`name`), not the IP — a device's IP can change, but
    its Device ID doesn't, so the link keeps working across an IP reassignment."""
    params = {'device': node['name']}
    if site:
        params['site'] = site
    return f"{app_url}/?{urlencode(params)}"


def _addr_link(node: dict, app_url: str | None, site: str | None) -> str:
    """The token shown after a device label in an outage list. When an app base URL is
    configured it's a Slack link reading "View device here" that points to that device's
    VIEW PAGE (the link target is unchanged — only the visible text differs); otherwise,
    with no app URL there's no link, so it falls back to the plain hostname/IP code span."""
    if app_url:
        return f"<{_device_url(app_url, site, node)}|View device here>"
    return f"`{_addr(node)}`"


def _change_label(node: dict) -> str:
    """Label for the changes list: friendly Name + 'AP' suffix for access points
    (switch names already carry 'Switch'; 'other' devices get no suffix)."""
    return f"{_label(node)} AP" if node.get('kind', 'ap') == 'ap' else _label(node)


def _changes_block(changes) -> dict | None:
    """A Slack section listing what changed since the last message — one line per
    device: ✅ '<name> is up' / ❌ '<name> is down'. `changes` is [(node, status)].
    Recoveries first, then new outages. Returns None when there's nothing to show."""
    if not changes:
        return None
    ups = [n for n, s in changes if s == 'up']
    downs = [n for n, s in changes if s == 'down']
    lines = [f"✅ {_change_label(n)} is up" for n in ups] + \
            [f"❌ {_change_label(n)} is down" for n in downs]
    if not lines:
        return None
    return {"type": "section",
            "text": {"type": "mrkdwn", "text": "*Changes since last update:*\n" + "\n".join(lines)}}


def _webhook_for(slack_cfg: dict, site: str | None = None) -> str:
    """Resolve the Slack webhook for a location: its per-site override
    (settings['slack']['webhooks'][<key>], from SLACK_WEBHOOK_URL_<KEY>) if set,
    otherwise the default webhook_url (SLACK_WEBHOOK_URL). App-wide messages pass
    site=None and always use the default channel."""
    if site:
        url = (slack_cfg.get('webhooks') or {}).get(site)
        if url:
            return url
    return slack_cfg.get('webhook_url', '')


def send_slack_update(settings: dict, down_switches: list[dict], down_devices: list[dict],
                      site_name: str | None = None, changes=None, site: str | None = None) -> bool:
    """Post a Slack message. Returns True only if a message was actually sent
    (False when Slack is disabled/unconfigured, the payload is suppressed, or the
    POST fails) so callers can track what the last sent message was."""
    slack_cfg = settings.get('slack', {})
    if not slack_cfg.get('enabled', False):
        return False

    webhook_url = _webhook_for(slack_cfg, site)
    if not webhook_url or 'YOUR/WEBHOOK' in webhook_url:
        log.warning("Slack webhook_url not configured — skipping notification")
        return False

    payload = _build_payload(down_switches, down_devices, slack_cfg, site_name, changes,
                             app_url=settings.get('app_url'), site=site)
    if not payload:
        return False  # e.g. a recovery suppressed by alert_on_recovery

    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            webhook_url, data=data,
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=10)
        log.info("Slack notification sent (%d switches, %d APs down)",
                 len(down_switches), len(down_devices))
        return True
    except urllib.error.HTTPError as e:
        log.error("Slack HTTP error %s: %s", e.code, e.read().decode())
    except Exception as e:
        log.error("Failed to send Slack notification: %s", e)
    return False


def send_site_deletion_notice(settings: dict, location_name: str,
                              app_name: str = 'Network Monitor', app_url: str | None = None,
                              site: str | None = None) -> bool:
    """Post the time-sensitive 'a user requested to delete <location>' notice to
    that location's channel. Returns True if actually sent."""
    slack_cfg = settings.get('slack', {})
    if not slack_cfg.get('enabled', False):
        return False
    webhook_url = _webhook_for(slack_cfg, site)
    if not webhook_url or 'YOUR/WEBHOOK' in webhook_url:
        log.warning("Slack webhook_url not configured — skipping deletion notice")
        return False

    app_label = f"<{app_url}|{app_name}>" if app_url else app_name
    text = (
        ":rotating_light: *TIME-SENSITIVE*\n"
        f"A user has requested to delete *{location_name}*.\n"
        f"If you would like to revert this change, please navigate to the location "
        f"*{location_name}* on {app_label}.\n"
        f"This change will become *PERMANENT in 24 HOURS*."
    )
    payload = {
        "text": f"TIME-SENSITIVE: deletion requested for {location_name}",
        "attachments": [{"color": "#b45309",
                         "blocks": [{"type": "section",
                                     "text": {"type": "mrkdwn", "text": text}}]}],
    }
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        log.info("Slack deletion notice sent for location %r", location_name)
        return True
    except Exception as e:
        log.error("Slack deletion notice failed: %s", e)
        return False


def _post_slack(settings: dict, text: str, summary: str, color: str = '#b45309',
                site: str | None = None) -> bool:
    """Post a single mrkdwn section to a location's webhook (site=None → default
    channel, for app-wide messages). Returns True if sent."""
    slack_cfg = settings.get('slack', {})
    if not slack_cfg.get('enabled', False):
        return False
    webhook_url = _webhook_for(slack_cfg, site)
    if not webhook_url or 'YOUR/WEBHOOK' in webhook_url:
        log.warning("Slack webhook_url not configured — skipping message")
        return False
    payload = {"text": summary, "attachments": [{"color": color,
               "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}]}
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.error("Slack post failed: %s", e)
        return False


def send_monitoring_down_notice(settings: dict, site_name: str, minutes: int,
                                site: str | None = None) -> bool:
    """Alert that a site's ping agent has gone silent (no reports for `minutes`)."""
    text = (":red_circle: *Monitoring alert*\n"
            f"No pings have been received from the *{site_name}* agent in over "
            f"*{minutes} minutes*. Device statuses may be stale — check the ping agent.")
    ok = _post_slack(settings, text, f"Monitoring alert: {site_name} agent is silent", "#7b0000", site=site)
    if ok:
        log.info("Watchdog: sent agent-silent alert for %r", site_name)
    return ok


def send_monitoring_recovered_notice(settings: dict, site_name: str,
                                     site: str | None = None) -> bool:
    """Notify that a site's ping agent is reporting again after a silence."""
    text = (f":large_green_circle: Monitoring for *{site_name}* has resumed — "
            "agent pings are being received again.")
    return _post_slack(settings, text, f"Monitoring resumed: {site_name}", "#2d7a2d", site=site)


def send_db_unwritable_notice(settings: dict, fail_count: int, error: str | None) -> bool:
    """Alert that the database is rejecting writes — device statuses are still tracked
    live in memory and Slack alerts still fire, but nothing is being persisted (logs,
    history, restart-durable state). Sent once when the condition starts."""
    text = (":warning: *Database write failure*\n"
            f"The app cannot write to its database ({fail_count} consecutive failed "
            f"transitions). Live monitoring and Slack alerts still work, but *nothing "
            f"is being saved* — logs, history, and status won't survive a restart. "
            "This often means the database is in read-only mode or a connection/limit "
            f"issue.\nLast error: `{error or 'unknown'}`")
    ok = _post_slack(settings, text, "Database write failure — nothing is being saved", "#7b0000")
    if ok:
        log.info("Watchdog: sent DB-unwritable alert (%d fails)", fail_count)
    return ok


def send_db_writable_notice(settings: dict) -> bool:
    """Notify that database writes have recovered after a write-failure period."""
    text = (":large_green_circle: *Database writes recovered* — the app can save to "
            "its database again; logs and history are being persisted normally.")
    return _post_slack(settings, text, "Database writes recovered", "#2d7a2d")


def send_slack_reminder(settings: dict, down_switches: list[dict],
                        down_devices: list[dict], hours: float,
                        site_name: str | None = None, site: str | None = None) -> bool:
    """A distinct, one-time escalation sent when a device has stayed down for
    `hours`. Styled differently from the routine change alerts — a red attachment
    bar and a 🔴 header — so it breaks through the noise. Returns True if sent."""
    slack_cfg = settings.get('slack', {})
    if not slack_cfg.get('enabled', False):
        return False
    webhook_url = _webhook_for(slack_cfg, site)
    if not webhook_url or 'YOUR/WEBHOOK' in webhook_url:
        log.warning("Slack webhook_url not configured — skipping reminder")
        return False
    if not down_switches and not down_devices:
        return False

    now = datetime.now(_EASTERN).strftime('%b %-d %Y, %-I:%M %p %Z')
    loc = f" — {site_name}" if site_name else ""
    total = len(down_switches) + len(down_devices)
    hrs = int(hours) if float(hours).is_integer() else hours
    app_url = settings.get('app_url')
    lines = [f"• *{_label(s)}*  {_addr_link(s, app_url, site)}" for s in down_switches]
    lines += [f"• {_label(d)}  {_addr_link(d, app_url, site)}" for d in down_devices]
    body = "\n".join(lines)

    payload = {
        "text": f"🔴 Still down after {hrs}h{loc} — {total} device(s) need attention",
        "attachments": [{
            "color": "#7b0000",
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": f"🔴 Still Down{loc} — {hrs}+ Hours", "emoji": True}},
                {"type": "section",
                 "text": {"type": "mrkdwn",
                          "text": (f"*{total} device{'s' if total != 1 else ''}* "
                                   f"{'have' if total != 1 else 'has'} now been offline for over "
                                   f"{hrs} hours and need attention:\n{body}")}},
                {"type": "context",
                 "elements": [{"type": "mrkdwn",
                               "text": f"⏱️ One-time escalation — no further reminders · {now} · Network Monitor"}]},
            ]
        }]
    }
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(webhook_url, data=data,
                                     headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=10)
        log.info("Slack reminder sent (%d switches, %d APs still down)",
                 len(down_switches), len(down_devices))
        return True
    except urllib.error.HTTPError as e:
        log.error("Slack reminder HTTP %s: %s", e.code, e.read().decode())
    except Exception as e:
        log.error("Failed to send Slack reminder: %s", e)
    return False


def _build_payload(down_switches: list, down_devices: list, slack_cfg: dict,
                   site_name: str | None = None, changes=None,
                   app_url: str | None = None, site: str | None = None) -> dict:
    now = datetime.now(_EASTERN).strftime('%b %-d %Y, %-I:%M %p %Z')
    loc = f" — {site_name}" if site_name else ""   # location label for multi-site
    total_down = len(down_switches) + len(down_devices)
    alert_on_recovery = bool(db.get_setting('alert_on_recovery'))

    if total_down == 0:
        if not alert_on_recovery:
            return {}
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"✅  All Devices Online{loc}", "emoji": True}
            }
        ]
        ch = _changes_block(changes)
        if ch:
            blocks.append(ch)
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"All APs and switches are responding · {now}"}]
        })
        return {
            "text": f"✅ All devices online{loc} — Network Monitor",
            "blocks": blocks
        }

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"⚠️  {total_down} Device{'s' if total_down != 1 else ''} Offline{loc}",
                "emoji": True
            }
        }
    ]

    if total_down > 5:
        # Too many to list individually — just give the counts by type.
        ns, nd = len(down_switches), len(down_devices)
        parts = []
        if ns:
            parts.append(f"*{ns}* switch{'es' if ns != 1 else ''}")
        if nd:
            parts.append(f"*{nd}* AP{'s' if nd != 1 else ''}")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f":rotating_light: {' and '.join(parts)} currently down."}
        })
    else:
        if down_switches:
            lines = "\n".join(
                f"• *{_label(s)}*  {_addr_link(s, app_url, site)}" for s in down_switches
            )
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f":rotating_light: *{len(down_switches)} Switch{'es' if len(down_switches) != 1 else ''} down:*\n{lines}"}
            })

        if down_devices:
            lines = "\n".join(
                f"• {_label(d)}  {_addr_link(d, app_url, site)}" for d in down_devices
            )
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f":wifi: *{len(down_devices)} AP{'s' if len(down_devices) != 1 else ''} down:*\n{lines}"}
            })

    if down_switches:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "⚡ APs connected to a down switch will also appear offline."}]
        })

    # What changed since the last message — keeps a long, count-summarized alert
    # actionable (you can see the deltas even when the full list is suppressed).
    ch = _changes_block(changes)
    if ch:
        blocks.append(ch)

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Checked · {now} · Network Monitor{loc}"}]
    })

    return {
        "text": f"⚠️ {total_down} device(s) offline{loc} — Network Monitor",
        "blocks": blocks
    }
