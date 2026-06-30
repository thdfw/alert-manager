"""Sync on-call schedule and contacts from Google Sheets to a local JSON cache."""

import json
from pathlib import Path
from typing import TypedDict

import gspread
from google.oauth2.service_account import Credentials

from alert_manager.config import Settings

SCHEDULE_WORKSHEET = "Schedule"
CONTACTS_WORKSHEET = "Contacts"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)

DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DAY_TO_INDEX = {day: index for index, day in enumerate(DAYS)}

Schedule = dict[int, dict[int, list[str]]]
Contacts = dict[str, str]


class OncallData(TypedDict):
    schedule: Schedule
    contacts: Contacts


def schedule_json_path(settings: Settings) -> Path:
    return Path(settings.schedule_file)


def write_schedule_cache(settings: Settings, data: OncallData) -> Path:
    """Write schedule/contacts to the local JSON cache, creating the file and parent dirs if needed."""
    path = schedule_json_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def parse_hour_start(value: str) -> int:
    hour_part = value.strip().split(":", maxsplit=1)[0]
    return int(hour_part)


def parse_names(value: str) -> list[str]:
    if not value.strip():
        return []
    return [name.strip() for name in value.split(",") if name.strip()]


def parse_schedule_rows(rows: list[list[str]]) -> Schedule:
    if not rows:
        return {}

    header = rows[0]
    day_columns: list[tuple[int, int]] = []
    for col_idx, label in enumerate(header[1:], start=1):
        day = label.strip().lower()
        if day in DAY_TO_INDEX:
            day_columns.append((col_idx, DAY_TO_INDEX[day]))

    schedule: Schedule = {day_index: {} for _, day_index in day_columns}

    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        hour_start = parse_hour_start(row[0])
        for col_idx, day_index in day_columns:
            cell = row[col_idx] if col_idx < len(row) else ""
            schedule[day_index][hour_start] = parse_names(cell)

    return schedule


def parse_contacts_rows(rows: list[list[str]]) -> Contacts:
    if not rows:
        return {}

    header = [label.strip().lower() for label in rows[0]]
    try:
        name_idx = header.index("name")
        chat_id_idx = header.index("telegram chat id")
    except ValueError:
        return {}

    contacts: Contacts = {}
    for row in rows[1:]:
        if name_idx >= len(row) or not row[name_idx].strip():
            continue
        name = row[name_idx].strip()
        chat_id = row[chat_id_idx].strip() if chat_id_idx < len(row) else ""
        if chat_id:
            contacts[name] = chat_id

    return contacts


def get_client(credentials_path: Path) -> gspread.Client:
    credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        str(credentials_path),
        scopes=SCOPES,
    )
    return gspread.authorize(credentials)


def read_worksheet(
    client: gspread.Client,
    spreadsheet_id: str,
    *,
    worksheet: str,
) -> list[list[str]]:
    spreadsheet = client.open_by_key(spreadsheet_id)
    return spreadsheet.worksheet(worksheet).get_all_values()


def read_google_sheet(settings: Settings) -> OncallData:
    """Fetch schedule and contacts from Google Sheets and write the JSON cache."""
    print("Reading schedule and contacts from Google Sheets")
    spreadsheet_id = settings.google_sheets_spreadsheet_id
    if not spreadsheet_id:
        raise ValueError("Set ALERT_MANAGER_GOOGLE_SHEETS_SPREADSHEET_ID in .env")

    credentials_path = Path(settings.google_credentials_file)
    client = get_client(credentials_path)
    data: OncallData = {
        "schedule": parse_schedule_rows(
            read_worksheet(client, spreadsheet_id, worksheet=SCHEDULE_WORKSHEET)
        ),
        "contacts": parse_contacts_rows(
            read_worksheet(client, spreadsheet_id, worksheet=CONTACTS_WORKSHEET)
        ),
    }

    path = write_schedule_cache(settings, data)
    print(f"Saved schedule and {len(data['contacts'])} contacts to {path}")
    return data


def main() -> None:
    settings = Settings()
    read_google_sheet(settings)


if __name__ == "__main__":
    main()
