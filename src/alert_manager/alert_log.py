import csv
import threading
from pathlib import Path

from alert_manager.models import Alert, TrackedAlert

FIELDNAMES = ["time_sent", "site_alias", "alert_alias", "message", "state"]
MAX_LOG_ROWS = 100


def _row_time_sent(row: dict[str, str]) -> int | None:
    """Unix seconds from a CSV row (supports legacy ``time_received`` column)."""
    raw = row.get("time_sent") or row.get("time_received")
    if raw is None or str(raw).strip() == "":
        return None
    return int(raw)


def _row_matches_alert(row: dict[str, str], alert: Alert) -> bool:
    time_sent = _row_time_sent(row)
    return (
        time_sent == alert.time_sent
        and row.get("alert_alias") == alert.alert_alias
        and row.get("site_alias") == alert.site_alias
    )


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    time_sent = _row_time_sent(row)
    if time_sent is None:
        raise ValueError(f"CSV row missing time_sent/time_received: {row!r}")
    return {
        "time_sent": str(time_sent),
        "site_alias": row["site_alias"],
        "alert_alias": row["alert_alias"],
        "message": row["message"],
        "state": row["state"],
    }


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
                if _row_matches_alert(row, alert):
                    row["state"] = tracked.state
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerows(_normalize_row(row) for row in rows)

    def read_between(self, start: int, end: int) -> list[TrackedAlert]:
        """All rows with start <= time_sent <= end, oldest first."""
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        records = []
        for index, row in enumerate(rows):
            time_sent = _row_time_sent(row)
            if time_sent is None:
                print(
                    f"[alert-manager] alerts-history: skipping CSV row {index} "
                    f"(missing time_sent/time_received): {row!r}"
                )
                continue
            in_range = start <= time_sent <= end
            print(
                f"[alert-manager] alerts-history: row {index} "
                f"time_sent={time_sent} in_range={in_range} "
                f"site={row.get('site_alias')} alert={row.get('alert_alias')}"
            )
            if in_range:
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
        print(
            f"[alert-manager] alerts-history: {len(records)}/{len(rows)} rows "
            f"in [{start}, {end}] from {self.path}"
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
