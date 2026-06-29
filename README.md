# Alert Manager

A small FastAPI service that takes alerts from any site or service, delivers them to the on-call engineer(s) over Telegram, and handles the whole follow-up: who to notify, re-sending, escalation, and acknowledgement.

## API

### `POST /new-alert`

```json
{ "message": "No data in the last 2 hours", "site_alias": "Site 2", "alert_alias": "no_data" }
```

```bash
curl -X POST http://localhost:8000/new-alert \
  -H "Authorization: Bearer $ALERT_MANAGER_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"No data in the last 2 hours","site_alias":"Site 2","alert_alias":"no_data"}'
```

- Sends the alert to the current on-call recipient(s) and starts tracking it.
- Returns `{"status": "ok"}`.

### `GET /alerts-history`

Returns the logged alerts from a time window, read from the CSV audit log. Use it to review what fired and whether it was acknowledged or muted.

Query parameters (both required):

- `start` — start of the window, unix time in seconds (inclusive).
- `end` — end of the window, unix time in seconds (inclusive).

It returns a JSON array of alerts whose `time_received` falls within `[start, end]`, oldest first. Each entry has `time_received`, `alert_alias`, `site_alias`, `message`, and `state` (`notified`, `acknowledged`, or `muted`). The window is matched against `time_received`, so an alert is included based on when it first arrived, regardless of when it was later acknowledged. Auth is the same bearer token as `/new-alert`; both `start` and `end` are required, so a missing one returns `422`.

Example — all alerts from the last 24 hours (using `date` to build the unix timestamps):

```bash
curl -H "Authorization: Bearer $ALERT_MANAGER_API_TOKEN" \
  "http://localhost:8000/alerts-history?start=$(date -d '24 hours ago' +%s)&end=$(date +%s)"
```

```json
[{ "time_received": 1735700000, "alert_alias": "no_data", "site_alias": "Site 2", "message": "No data in the last 2 hours", "state": "acknowledged" }]
```

### `GET /health`

Returns `{"status": "ok", "active_alerts": <n>}`. No auth.

## How it works

**Recipients.** The on-call schedule and contacts live in a Google Sheet (`Schedule` and `Contacts` worksheets), cached locally to `google-sheet.json` and refreshed on each send. The recipient is whoever is on call for the current weekday and hour.

**Tracking and escalation.** While any alert is active, a background loop runs every `ALERT_MANAGER_CHECK_INTERVAL_SECONDS` (default 30s) to check for acknowledgements quickly. Re-sending is separate and slower: an unacknowledged alert is only re-sent once `ALERT_MANAGER_REMINDER_INTERVAL_SECONDS` (default 5 min) has passed since its last send. Each re-send bumps a per-alert counter:

- counts 1–`ALERT_MANAGER_ESCALATE_AFTER_COUNT` (default 3): on-call recipient only,
- above that: escalates to **all** contacts,
- stops after `ALERT_MANAGER_MAX_ALERT_COUNT` (default 6) sends (the alert stays tracked so a late acknowledgement is still honored).

When no alerts are active the loop still ticks but does nothing (no Telegram calls), so the fast interval is cheap.

**Acknowledgement (👍).** An alert is acknowledged when a recipient reacts with 👍 to the alert message. Acknowledged alerts are dropped.

**Mute (👎).** A 👎 reaction also drops the alert and additionally **bans** its alias: any later alert with the same `site_alias` + `alert_alias` *that day* is ignored. The ban list is cleared every `ALERT_MANAGER_BAN_CLEAR_INTERVAL_SECONDS` (default 24h) — enough, since the alias includes the date.

**Idempotency.** While an alert is active, a repeat `/new-alert` with the same `site_alias` + `alert_alias` (same day) is ignored. Re-sends and escalation are driven internally, never by repeat posts.

**Audit log.** Every alert is appended as one row to a local CSV (`ALERT_MANAGER_ALERT_LOG_FILE`, default `alerts.csv`) with columns `time_received`, `alert_alias`, `site_alias`, `message`, `state`. The `state` starts at `notified` and is updated to `acknowledged` (👍) or `muted` (👎) when the reaction is read.

## Configuration

Copy `.env.example` to `.env` and fill it in. All variables use the `ALERT_MANAGER_` prefix.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ALERT_MANAGER_TELEGRAM_BOT_TOKEN` | — | Bot token used to send alerts and poll reactions |
| `ALERT_MANAGER_API_TOKEN` | — | Shared secret for `Authorization: Bearer` on `/new-alert`. If unset, auth is disabled (a warning is logged) |
| `ALERT_MANAGER_GOOGLE_SHEETS_SPREADSHEET_ID` | — | Spreadsheet with the `Schedule` and `Contacts` worksheets |
| `ALERT_MANAGER_GOOGLE_CREDENTIALS_FILE` | `google-credentials.json` | Path to the Google service-account JSON (read-only access to the sheet) |
| `ALERT_MANAGER_SCHEDULE_FILE` | `google-sheet.json` | Local cache of the schedule/contacts |
| `ALERT_MANAGER_ALERT_LOG_FILE` | `alerts.csv` | CSV log of every alert and its state |
| `ALERT_MANAGER_TIMEZONE` | `America/New_York` | Timezone for the date in alias keys and on-call lookup |
| `ALERT_MANAGER_CHECK_INTERVAL_SECONDS` | `30` | How often active alerts are checked for acknowledgement |
| `ALERT_MANAGER_REMINDER_INTERVAL_SECONDS` | `300` | Minimum time between re-sends of an unacknowledged alert |
| `ALERT_MANAGER_MAX_ALERT_COUNT` | `6` | Max sends per alert |
| `ALERT_MANAGER_ESCALATE_AFTER_COUNT` | `3` | Escalate to all contacts once the count exceeds this |
| `ALERT_MANAGER_BAN_CLEAR_INTERVAL_SECONDS` | `86400` | How often the 👎-ban list is cleared |
| `ALERT_MANAGER_HOST` / `ALERT_MANAGER_PORT` | `0.0.0.0` / `8000` | Bind address |

## Run

```bash
uv sync
cp .env.example .env   # then fill in credentials
uv run alert-manager
```

Refresh the cached on-call schedule manually (it is otherwise refreshed on each send):

```bash
uv run alert-manager-sheet
```

## Deploy (systemd)

```ini
[Unit]
Description=Alert manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/alert-manager
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/ubuntu/alert-manager/.venv/bin/alert-manager
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now alert-manager
sudo journalctl -u alert-manager -f
```

## Tests

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
```
