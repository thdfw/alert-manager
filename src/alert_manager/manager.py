"""In-memory alert tracking, Telegram delivery, acknowledgement and escalation."""

import json
import threading
import time

import pendulum
import requests

from alert_manager.config import Settings
from alert_manager.google_sheet_reader import read_google_sheet, schedule_json_path
from alert_manager.models import Alert, AlertSend


class AlertManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timezone_str = settings.timezone
        self.max_alert_count = settings.max_alert_count
        self.escalate_after_count = settings.escalate_after_count
        self.active_telegram_alerts: dict[str, Alert] = {}
        # full_aliases a recipient muted with 👎; never re-triggered while listed.
        # Entries embed today's date, so the set is cleared every 24h (it would
        # otherwise grow forever with stale, unmatchable aliases).
        self.banned_alerts: set[str] = set()
        self.telegram_update_offset = 0
        # Guards active_telegram_alerts/banned_alerts against concurrent access
        # by the /new-alert request threads and the background check loop.
        self._lock = threading.Lock()

    def send_alert(
        self, message: str, site_alias: str, alert_alias: str, reminder: bool = False
    ) -> None:
        full_alias = (
            f"{pendulum.now(tz=self.timezone_str).format('YYYY-MM-DD')}"
            f"-{site_alias}-{alert_alias}"
        )

        with self._lock:
            # A 👎 banned this alias; never re-trigger it (until the daily clear).
            if full_alias in self.banned_alerts:
                print(f"Alert {full_alias} is banned; ignoring")
                return
            # A non-reminder (external) alert for an alias that is still active
            # is a duplicate; reject it cheaply before any Google/Telegram work.
            if not reminder and full_alias in self.active_telegram_alerts:
                print(f"Duplicate alert {full_alias} still active; ignoring")
                return
            if full_alias not in self.active_telegram_alerts:
                self.active_telegram_alerts[full_alias] = Alert(
                    message=message,
                    site_alias=site_alias,
                    alert_alias=alert_alias,
                )
            elif self.active_telegram_alerts[full_alias].count < self.max_alert_count:
                self.active_telegram_alerts[full_alias].count += 1
            else:
                return

            count = self.active_telegram_alerts[full_alias].count

        # Refresh the on-call schedule/contacts cache. A transient Google
        # failure should not drop the alert, so fall back to the cached JSON.
        print(f"\n[SENDING ALERT] {message}")
        try:
            read_google_sheet(self.settings)
        except Exception as e:
            print(f"Could not refresh Google Sheet, using cached schedule: {e}")

        recipients = self.get_alert_recipients(alert_count=count)
        if not recipients:
            print(f"Skipping telegram alert {full_alias}: no recipients resolved")
            return

        token = self.settings.telegram_bot_token.get_secret_value()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        telegram_message = f"[{site_alias.capitalize()}] {message}"
        sent_message_ids: dict[str, int] = {}
        for chat_id in recipients:
            response = requests.post(
                url, json={"chat_id": chat_id, "text": telegram_message}
            )
            if response.status_code == 200:
                message_id = response.json().get("result", {}).get("message_id")
                if message_id is not None:
                    sent_message_ids[chat_id] = message_id
            else:
                print(
                    f"Failed to send telegram alert to {chat_id}: "
                    f"{response.status_code}, {response.text}"
                )

        if sent_message_ids:
            with self._lock:
                alert = self.active_telegram_alerts.get(full_alias)
                if alert is not None:
                    alert.sends.append(
                        AlertSend(
                            sent_at=int(time.time()), message_ids=sent_message_ids
                        )
                    )
            count_sent = len(sent_message_ids)
            print(f"Telegram alert {full_alias} sent to {count_sent} recipient(s)")

    def get_alert_recipients(self, alert_count: int) -> list[str]:
        alert_time = pendulum.now(tz=self.timezone_str)

        schedule_path = schedule_json_path(self.settings)
        try:
            with schedule_path.open(encoding="utf-8") as schedule_file:
                oncall_data = json.load(schedule_file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"Could not read on-call schedule at {schedule_path}: {e}")
            return []
        schedule = oncall_data.get("schedule", {})
        contacts = oncall_data.get("contacts", {})

        if alert_count > self.escalate_after_count:
            recipients = [str(chat_id) for chat_id in contacts.values() if chat_id]
            print(f"Escalation: sending to all {len(recipients)} contact(s)")
            return recipients

        names = schedule.get(str(alert_time.weekday()), {}).get(
            str(alert_time.hour), []
        )

        if not names:
            return []

        recipients = []
        for name in names:
            chat_id = contacts.get(name, "")
            if chat_id:
                recipients.append(str(chat_id))
            else:
                print(f"No Telegram chat ID configured for {name}")

        print(f"On-call: {', '.join(names)} -> {len(recipients)} recipient(s)")
        return recipients

    def check_telegram_alerts(self) -> None:
        print("\nChecking active alerts...")
        with self._lock:
            active = list(self.active_telegram_alerts.items())
        print(f"There are {len(active)} active alerts.")
        if not active:
            return

        token = self.settings.telegram_bot_token.get_secret_value()
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        # message_reaction updates are excluded by default; opt in explicitly.
        params: dict[str, str | int] = {
            "offset": self.telegram_update_offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message_reaction"]),
        }
        response = requests.get(url, params=params)
        # (chat_id, message_id) pairs that currently carry 👍 / 👎 reactions.
        thumbed_up: set[tuple[str, int]] = set()
        thumbed_down: set[tuple[str, int]] = set()
        if response.status_code == 200:
            for update in response.json().get("result", []):
                self.telegram_update_offset = update["update_id"] + 1
                reaction = update.get("message_reaction")
                if not reaction:
                    continue
                emojis = {
                    r.get("emoji")
                    for r in reaction.get("new_reaction", [])
                    if r.get("type") == "emoji"
                }
                key = (str(reaction["chat"]["id"]), reaction["message_id"])
                if "👍" in emojis:
                    thumbed_up.add(key)
                if "👎" in emojis:
                    thumbed_down.add(key)

        for full_alert_alias, alert in active:
            sent_keys = [
                (chat_id, message_id)
                for sent in alert.sends
                for chat_id, message_id in sent.message_ids.items()
            ]
            banned = any(key in thumbed_down for key in sent_keys)
            acknowledged = banned or any(key in thumbed_up for key in sent_keys)

            if banned:
                print(f"Telegram alert {full_alert_alias} banned (👎)")
                with self._lock:
                    self.active_telegram_alerts.pop(full_alert_alias, None)
                    self.banned_alerts.add(full_alert_alias)
            elif acknowledged:
                print(f"Telegram alert {full_alert_alias} acknowledged")
                with self._lock:
                    self.active_telegram_alerts.pop(full_alert_alias, None)
            else:
                self.send_alert(
                    alert.message,
                    alert.site_alias,
                    alert.alert_alias,
                    reminder=True,
                )

    def clear_banned_alerts(self) -> None:
        """Drop all banned aliases. Run every 24h: full_aliases embed the date,
        so yesterday's entries can never match again and would only accumulate."""
        with self._lock:
            count = len(self.banned_alerts)
            self.banned_alerts.clear()
        print(f"Cleared {count} banned alert alias(es)")
