"""Pydantic models for alerts."""

from pydantic import BaseModel


class NewAlert(BaseModel):
    """Payload posted to ``/new-alert`` by any alert producer."""

    message: str
    site_alias: str
    alert_alias: str


class AlertRecord(BaseModel):
    """One row of the alert history CSV."""

    time_received: int
    alert_alias: str
    site_alias: str
    message: str
    state: str


class AlertSend(BaseModel):
    """A single delivery of an alert.

    ``message_ids`` maps each recipient chat_id to the Telegram message_id sent
    there, so a 👍 reaction can be matched back to the exact alert message.
    """

    sent_at: int
    message_ids: dict[str, int] = {}


class Alert(BaseModel):
    """An alert being tracked for acknowledgement and escalation."""

    count: int = 1
    sends: list[AlertSend] = []
    message: str
    site_alias: str
    alert_alias: str
    time_received: int
