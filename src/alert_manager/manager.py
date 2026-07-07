"""In-memory alert tracking, Telegram delivery, acknowledgement and escalation."""

import json
import threading
import time

import pendulum
import requests

from alert_manager.alert_log import AlertLog
from alert_manager.config import Settings
from alert_manager.google_sheet_reader import read_google_sheet, schedule_json_path
from alert_manager.models import Alert, AlertSend, TrackedAlert


class AlertManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.timezone_str = settings.timezone
        self.max_alert_count = settings.max_alert_count
        self.escalate_after_count = settings.escalate_after_count
        self.reminder_interval_seconds = settings.reminder_interval_seconds
        self.alert_log = AlertLog(settings.alert_log_file)
        self.active_telegram_alerts: dict[str, TrackedAlert] = {}
        self.muted_alerts: set[str] = set()
        self.telegram_update_offset = 0
        self._lock = threading.Lock()

    def send_alert(self, alert: Alert, reminder: bool = False) -> None:
        date_str = pendulum.from_timestamp(
            alert.time_sent, tz=self.timezone_str
        ).format("YYYY-MM-DD")
        full_alias = f"{date_str}-{alert.site_alias}-{alert.alert_alias}"

        with self._lock:
            if full_alias in self.muted_alerts:
                print(f"Alert {full_alias} is muted; ignoring")
                return

            if not reminder and full_alias in self.active_telegram_alerts:
                print(f"Duplicate alert {full_alias} still active; ignoring")
                return

            if full_alias not in self.active_telegram_alerts:
                tracked = TrackedAlert(alert=alert)
                self.active_telegram_alerts[full_alias] = tracked

                self.alert_log.record(tracked)
            elif self.active_telegram_alerts[full_alias].count < self.max_alert_count:
                self.active_telegram_alerts[full_alias].count += 1
            else:
                return

            tracked = self.active_telegram_alerts[full_alias]
            count = tracked.count

        print(f"\n[SENDING ALERT] {alert.message}")
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
        telegram_message = f"[{alert.site_alias.capitalize()}] {alert.message}"
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
                tracked = self.active_telegram_alerts.get(full_alias)
                if tracked is not None:
                    tracked.sends.append(
                        AlertSend(
                            sent_at=int(time.time()), message_ids=sent_message_ids
                        )
                    )
                    tracked.state = "sent"
                    self.alert_log.set_state(tracked)
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
        with self._lock:
            active = list(self.active_telegram_alerts.items())
        if not active:
            return
        print(f"\nChecking {len(active)} active alert(s)...")

        token = self.settings.telegram_bot_token.get_secret_value()
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        params: dict[str, str | int] = {
            "offset": self.telegram_update_offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message_reaction"]),
        }
        response = requests.get(url, params=params)
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

        for full_alert_alias, tracked in active:
            sent_keys = [
                (chat_id, message_id)
                for sent in tracked.sends
                for chat_id, message_id in sent.message_ids.items()
            ]
            muted = any(key in thumbed_down for key in sent_keys)
            acknowledged = muted or any(key in thumbed_up for key in sent_keys)
            alert = tracked.alert

            if muted:
                print(f"Telegram alert {full_alert_alias} muted (👎)")
                tracked.state = "muted"
                with self._lock:
                    self.active_telegram_alerts.pop(full_alert_alias, None)
                    self.muted_alerts.add(full_alert_alias)
                self.alert_log.set_state(tracked)
            elif acknowledged:
                print(f"Telegram alert {full_alert_alias} acknowledged")
                tracked.state = "acknowledged"
                with self._lock:
                    self.active_telegram_alerts.pop(full_alert_alias, None)
                self.alert_log.set_state(tracked)
            else:
                last_activity = (
                    tracked.sends[-1].sent_at if tracked.sends else alert.time_sent
                )
                print(
                    f"Time since last activity: {int(time.time()) - last_activity} seconds, "
                    f"reminder interval: {self.reminder_interval_seconds} seconds"
                )
                if int(time.time()) - last_activity >= self.reminder_interval_seconds:
                    self.send_alert(alert, reminder=True)

    def alerts_history(self, start: int, end: int) -> list[TrackedAlert]:
        """Logged alerts whose time_sent falls within [start, end]."""
        return self.alert_log.read_between(start, end)

    def clear_muted_alerts(self) -> None:
        """Drop all muted aliases. Run every 24h: full_aliases embed the date,
        so yesterday's entries can never match again and would only accumulate."""
        with self._lock:
            count = len(self.muted_alerts)
            self.muted_alerts.clear()
        print(f"Cleared {count} muted alert alias(es)")
