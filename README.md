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

It returns a JSON array of tracked alerts whose `alert.time_sent` falls within `[start, end]`, oldest first. Each entry is a `TrackedAlert` with `count`, `sends`, `alert` (`message`, `site_alias`, `alert_alias`, `time_sent`), and `state` (`processed`, `sent`, `acknowledged`, or `muted`). The window is matched against `time_sent`, so an alert is included based on when it was sent, regardless of when it was later acknowledged. Auth is the same bearer token as `/new-alert`; both `start` and `end` are required, so a missing one returns `422`.

Example — all alerts from the last 24 hours (using `date` to build the unix timestamps):

```bash
curl -H "Authorization: Bearer $ALERT_MANAGER_API_TOKEN" \
  "http://localhost:8000/alerts-history?start=$(date -d '24 hours ago' +%s)&end=$(date +%s)"
```

```json
[{ "count": 1, "sends": [], "alert": { "message": "No data in the last 2 hours", "site_alias": "Site 2", "alert_alias": "no_data", "time_sent": 1735700000 }, "state": "acknowledged" }]
```

### `GET /health`

Returns `{"status": "ok", "active_alerts": <n>}`. No auth.

## How it works

**Recipients.** The on-call schedule and contacts live in a Google Sheet that is read on each send. The recipient is whoever is on call for the current weekday and hour.

**Tracking and escalation.** While any alert is active, a background loop runs every `ALERT_MANAGER_CHECK_INTERVAL_SECONDS` (default 30s) to check for acknowledgements quickly. Re-sending is separate and slower: an unacknowledged alert is only re-sent once `ALERT_MANAGER_REMINDER_INTERVAL_SECONDS` (default 5 min) has passed since its last send. Each re-send bumps a per-alert counter:

- from 1 to `ALERT_MANAGER_ESCALATE_AFTER_COUNT` (default 3): on-call recipient only,
- above that: escalates to **all** contacts,
- stops after `ALERT_MANAGER_MAX_ALERT_COUNT` (default 6) sends (the alert stays tracked so a late acknowledgement is still honored).

When no alerts are active the loop still ticks but does nothing (no Telegram calls), so the fast interval is cheap.

**Acknowledgement (👍).** An alert is acknowledged when a recipient reacts with 👍 to the alert message. Acknowledged alerts are dropped from the alert manager completely.

**Mute (👎).** A 👎 reaction also drops the alert and additionally **mutes** its alias: any later alert with the same `site_alias` + `alert_alias` *that day* is ignored. The mute list is cleared every `ALERT_MANAGER_MUTE_CLEAR_INTERVAL_SECONDS` (default 24h) — enough, since the alias includes the date.

**Idempotency.** While an alert is active, a repeat `/new-alert` with the same `site_alias` + `alert_alias` (same day) is ignored. Re-sends and escalation are driven internally, never by repeat posts.

**Audit log.** Every alert is appended as one row to a local CSV (`ALERT_MANAGER_ALERT_LOG_FILE`, default `alerts.csv`) with columns `time_sent`, `site_alias`, `alert_alias`, `message`, `state`. The `state` starts at `processed` when the alert is accepted, becomes `sent` once Telegram delivery succeeds, and is updated to `acknowledged` (👍) or `muted` (👎) when the reaction is read.

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
| `ALERT_MANAGER_MUTE_CLEAR_INTERVAL_SECONDS` | `86400` | How often the 👎-mute list is cleared |
| `ALERT_MANAGER_HOST` / `ALERT_MANAGER_PORT` | `0.0.0.0` / `8000` | Bind address |

## Google Sheets on-call setup

The spreadsheet configured in `ALERT_MANAGER_GOOGLE_SHEETS_SPREADSHEET_ID` must have two worksheets named **`Schedule`** and **`Contacts`**. The service account from `google-credentials.json` needs read-only access to the spreadsheet (Share → add the service-account email as Viewer).

### Add a contact

On the **`Contacts`** worksheet, add a row with these columns (header names are case-insensitive):

| Name | Telegram chat id |
|------|------------------|
| Alice | `123456789` |

- **Name** — short label used in the schedule. Must match exactly in `Schedule` cells (including spelling and capitalization).
- **Telegram chat id** — numeric ID for the person's DM with the alert bot. Before they can receive alerts, they must open Telegram, search for **@GridWorksAlertsBot**, tap **Start**, and send any message to open the chat. To find the ID, forward that message to **@ShowJsonBot** and read the `chat.id` field from the JSON it replies with.

Rows without a chat ID are ignored.

### Assign on-call hours

On the **`Schedule`** worksheet:

- Row 1: day names in columns B onward (`monday`, `tuesday`, … — case-insensitive).
- Column A: hour labels for each row (`0:00`, `1:00`, … `23:00`). Only the hour before the colon is used.
- Each cell: one or more contact names, comma-separated if several people share a slot (e.g. `Alice` or `Alice, Bob`).

On-call lookup uses `ALERT_MANAGER_TIMEZONE` (default `America/New_York`): weekday and hour at alert time determine which name(s) receive the message.

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
