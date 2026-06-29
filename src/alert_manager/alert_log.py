"""Append-only-ish CSV log of every alert that comes through /new-alert.

One row per alert. The ``state`` column moves notified -> acknowledged / muted as
the recipient's reaction is read. Updates rewrite the file (fine at this volume).
The file is created lazily on the first write, so importing the app never makes one.
"""

import csv
import threading
from pathlib import Path

from alert_manager.models import AlertRecord

FIELDNAMES = ["time_received", "alert_alias", "site_alias", "message", "state"]
MAX_LOG_ROWS = 100


class AlertLog:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record_notified(
        self, time_received: int, alert_alias: str, site_alias: str, message: str
    ) -> None:
        row = {
            "time_received": time_received,
            "alert_alias": alert_alias,
            "site_alias": site_alias,
            "message": message,
            "state": "notified",
        }
        with self._lock:
            exists = self.path.exists()
            with self.path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
            # Keep only the most recent MAX_LOG_ROWS rows.
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) > MAX_LOG_ROWS:
                with self.path.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    writer.writeheader()
                    writer.writerows(rows[-MAX_LOG_ROWS:])

    def set_state(
        self, time_received: int, alert_alias: str, site_alias: str, state: str
    ) -> None:
        """Update the state of the row matching the given identifying columns."""
        with self._lock:
            if not self.path.exists():
                return
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                if (
                    row["time_received"] == str(time_received)
                    and row["alert_alias"] == alert_alias
                    and row["site_alias"] == site_alias
                ):
                    row["state"] = state
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(rows)

    def read_between(self, start: int, end: int) -> list[AlertRecord]:
        """All rows with start <= time_received <= end, oldest first."""
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        records = []
        for row in rows:
            time_received = int(row["time_received"])
            if start <= time_received <= end:
                records.append(
                    AlertRecord(
                        time_received=time_received,
                        alert_alias=row["alert_alias"],
                        site_alias=row["site_alias"],
                        message=row["message"],
                        state=row["state"],
                    )
                )
        return records
