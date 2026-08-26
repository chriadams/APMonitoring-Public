# AP Monitor — Windows Server ping agent

This is the half of the system that runs **on a network that can reach the devices**.
The web dashboard and Slack alerting run on Heroku, but Heroku can't reach the private
device addresses (`192.0.2.x` / `198.51.100.x`). This agent does the actual pinging
locally and reports results up to the server.

It's stateless and stdlib-only — no database, no `pip install`. It pulls the target
list and ping cadence from the server every cycle. It runs on **Windows Server** (the
production setup); `ping_agent.py` branches on `platform.system()` for the Windows
`ping` flags, so no code changes are needed.

## What you download from the dashboard

The **Download agent** button (hamburger menu) gives you `ap-monitor-agent.zip`
containing:
- `ping_agent.py` — the agent (the only file that matters).
- `README.md` — this file.
- `.env` — pre-filled with your `SERVER_URL` and `SITE=all`; you just fill in the token.

The bundle always uses **`SITE=all`** (one agent covers every location) with the
**master `AGENT_TOKEN`**, so it's identical no matter which location you're viewing.

## One agent, all locations (`SITE=all`)

The agent runs as `SITE=all`: it pulls every location's devices, pings them all, and
reports back; the server maps each device to its location **by IP** and alerts each
location independently. Use the **master `AGENT_TOKEN`** (the plain `AGENT_TOKEN`
config var on Heroku), which must exactly match the token in the agent's `.env`.

> Because reports are routed to a location by IP, device IPs must be unique across all
> locations for this mode.

## Setup on Windows Server (Task Scheduler as `SYSTEM`)

The production agent runs via **Windows Task Scheduler under the built-in `SYSTEM`
account** — no third-party tools, and because SYSTEM has no stored password, a Windows
**password / credential rotation can never break it** (the failure mode that took the
agent down before). This is the recommended way.

1. Install Python 3 for Windows; put `ping_agent.py` in `C:\ap-monitor-agent\`.
2. Create `C:\ap-monitor-agent\run.bat` — it sets the env vars, then launches the agent
   headless with `pythonw.exe` (no console window). Keep `LOG_FILE` so you still get
   logs (with `pythonw` there's no console to capture):
   ```bat
   @echo off
   set SERVER_URL=https://your-app.herokuapp.com
   set AGENT_TOKEN=<master token>
   set SITE=all
   set AGENT_NAME=windows-server
   set LOG_FILE=C:\ap-monitor-agent\agent.log
   "C:\Path\to\pythonw.exe" "C:\ap-monitor-agent\ping_agent.py"
   ```
3. Create the task (Task Scheduler → **Create Task…**, *not* "Basic Task"):
   - **General:** click *Change User* → type **`SYSTEM`** → OK. That makes it "run
     whether a user is logged on or not" with **no password**. Tick **Run with highest
     privileges**.
   - **Triggers:** New → **At startup** (survives reboots with no login). To also
     auto-restart a *dead* process, on that trigger tick **Repeat task every 5 minutes**
     for a duration of **Indefinitely**.
   - **Actions:** Start a program → `C:\ap-monitor-agent\run.bat` (Start in:
     `C:\ap-monitor-agent`).
   - **Settings:** **untick "Stop the task if it runs longer than …"** — Windows sets a
     limit by default (3 days in the GUI; an imported task can show `72:00:00`), and the
     agent is meant to run forever, so this **must be off** or it gets killed every few
     days. Also tick **"Run task as soon as possible after a scheduled start is missed"**
     and set **"If the task is already running… Do not start a new instance."** (paired
     with the repeating trigger, that restarts a dead agent within 5 min but never
     double-launches a healthy one).

   Or script the whole thing (SYSTEM = SID `S-1-5-18`; `ExecutionTimeLimit PT0S` = no
   limit; `RestartOnFailure` for non-zero exits) via a task XML:
   ```cmd
   schtasks /Create /TN "ap-monitor-agent" /XML "C:\ap-monitor-agent\task.xml" /F
   schtasks /Run   /TN "ap-monitor-agent"
   ```

**Verify it's running *and working under SYSTEM*** — a task can launch fine and then
fail on its first real operation, so check both the identity and the data:
```powershell
(Get-ScheduledTask -TaskName "ap-monitor-agent").State       # -> Running
(Get-ScheduledTask -TaskName "ap-monitor-agent").Principal   # UserId -> SYSTEM, LogonType -> ServiceAccount
Get-Content C:\ap-monitor-agent\agent.log -Tail 20 -Wait     # -> "Reported N results" every ~2 min
```
The dashboard's stale banner should also clear and device "last seen" times go current.
**SYSTEM gotchas** if the log shows errors: SYSTEM has **no mapped drive letters** (use
UNC paths), can't read files under a user profile, and has no access to your Windows
credential vault — so keep the agent's files and token out of user-profile paths.

**Diagnosing a failed run** (`Status: Ready` with a non-zero `Last Result`): dump the
task's last result + config, and the Task Scheduler event log:
```cmd
schtasks /query /tn "ap-monitor-agent" /v /fo LIST > C:\temp\task_info.txt
```
```powershell
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-TaskScheduler/Operational'} |
  Where-Object { $_.Message -like "*ap-monitor-agent*" } |
  Select TimeCreated, Id, LevelDisplayName, Message | Format-List | Out-File C:\temp\task_log.txt
```
> Task Scheduler's built-in **"restart on failure" only fires on a non-zero exit code** —
> it does **not** restart a process that's hung-but-alive, exited `0`, or was killed by a
> session ending. The combination above (**SYSTEM account + at-startup trigger + a
> repeating trigger + no time limit**) is what makes it reliable through logoffs,
> reboots, and password changes.

### Try it in the foreground first

Before wiring up the task, sanity-check the config in a console window:
```cmd
cd C:\ap-monitor-agent
set SERVER_URL=https://your-app.herokuapp.com
set AGENT_TOKEN=<master token>
set SITE=all
python ping_agent.py
```
You should see `Agent starting…`, then `Pinging N targets`, then `Reported N results`.
Ctrl-C to stop, then set up the scheduled task above.

<details>
<summary>Alternative: NSSM (a real service wrapper, needs a download)</summary>

If you'd rather use a service wrapper, [NSSM](https://nssm.cc/) also works:
```
nssm install ap-monitor-agent "C:\Path\to\python.exe" "C:\ap-monitor-agent\ping_agent.py"
nssm set ap-monitor-agent AppDirectory C:\ap-monitor-agent
nssm set ap-monitor-agent AppEnvironmentExtra SERVER_URL=https://your-app.herokuapp.com AGENT_TOKEN=<master> SITE=all LOG_FILE=C:\ap-monitor-agent\agent.log
nssm set ap-monitor-agent Start SERVICE_AUTO_START
nssm start ap-monitor-agent
```
NSSM auto-restarts on crash and starts on boot. Prefer the built-in rotating `LOG_FILE`
over `AppStdout`; don't point both at the same file.
</details>

## Multiple agents pinging the same devices

Running two agents against the same devices (e.g. during a server migration) is safe:
the server confirms a `down` over `monitoring.down_confirm_checks` consecutive checks
(immediate recovery) and de-dupes redundant reports, so you won't get alert storms or
doubled history — and if one agent dies the other keeps things covered.

## Logs

The agent logs to the console by default. To keep a persistent log **without it
growing forever**, set `LOG_FILE`: it writes a size-capped, auto-rotating file — at
`LOG_MAX_MB` (default 5 MB) it rolls over and keeps only `LOG_BACKUPS` old files
(default 3), so the total on disk is bounded (**~20 MB by default**). Tune with
`LOG_MAX_MB` / `LOG_BACKUPS`.

If you set `LOG_FILE`, do **not** also redirect output to the same file (e.g.
`python ping_agent.py >> agent.log`) — that writes it twice and defeats the cap.

## How it works

- Every `FLAG_POLL_INTERVAL` seconds (default 20s) the agent calls
  `GET /api/agent/targets`. It pings all targets when a full `POLL_INTERVAL`
  (default 60s) has elapsed, **or** immediately if the server reports a
  "Check Now" was requested from the dashboard.
- Each sweep pings devices **concurrently** (a thread pool, `PING_WORKERS`, default
  40), so it finishes in ~one ping timeout regardless of fleet size — down hosts
  (which each block for the full timeout) no longer serialize the sweep. Raise
  `PING_WORKERS` for a very large fleet, or lower it to cap CPU.
- The dashboard's **single-device "Check now"** sends a *targeted* request: the
  server returns a `check_ips` list and the agent pings **only those devices**
  (reporting just them), without disturbing the regular full-sweep cadence. The
  header "Check Now" button still triggers a full sweep. (Agents older than this
  feature ignore `check_ips` and do a full sweep — still correct, just not
  targeted — so **redeploy the agent** to get single-device checks.)
- Results are POSTed to `/api/agent/report`, where the server records them and
  sends a Slack alert if any device changed state.

## Tuning

Edit the env vars in `run.bat` (or the task/NSSM config) and restart the task
(`schtasks /End` then `/Run`, or restart the service):
- Lower `POLL_INTERVAL` for fresher data (more pings/CPU).
- Lower `FLAG_POLL_INTERVAL` to make the "Check Now" button respond faster.
