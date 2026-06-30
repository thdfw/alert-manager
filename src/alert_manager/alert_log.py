import csv
import threading
from pathlib import Path

from alert_manager.models import Alert, TrackedAlert

FIELDNAMES = ["time_sent", "site_alias", "alert_alias", "message", "state"]
MAX_LOG_ROWS = 100


class AlertLog:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(self, tracked: TrackedAlert) -> None:
        row = self._row_from_tracked(tracked)
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

    def set_state(self, tracked: TrackedAlert) -> None:
        """Update the state of the row matching the tracked alert's identity."""
        alert = tracked.alert
        with self._lock:
            if not self.path.exists():
                return
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                if (
                    row["time_sent"] == str(alert.time_sent)
                    and row["alert_alias"] == alert.alert_alias
                    and row["site_alias"] == alert.site_alias
                ):
                    row["state"] = tracked.state
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(rows)

    def read_between(self, start: int, end: int) -> list[TrackedAlert]:
        """All rows with start <= time_sent <= end, oldest first."""
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        records = []
        for row in rows:
            time_sent = int(row["time_sent"])
            if start <= time_sent <= end:
                records.append(
                    TrackedAlert(
                        alert=Alert(
                            message=row["message"],
                            site_alias=row["site_alias"],
                            alert_alias=row["alert_alias"],
                            time_sent=time_sent,
                        ),
                        state=row["state"],
                    )
                )
        return records

    def _row_from_tracked(self, tracked: TrackedAlert) -> dict[str, str]:
        alert = tracked.alert
        return {
            "time_sent": str(alert.time_sent),
            "site_alias": alert.site_alias,
            "alert_alias": alert.alert_alias,
            "message": alert.message,
            "state": tracked.state,
        }
