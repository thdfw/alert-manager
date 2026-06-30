from pydantic import BaseModel


class Alert(BaseModel):
    site_alias: str
    alert_alias: str
    time_sent: int
    message: str


class AlertSend(BaseModel):
    sent_at: int
    message_ids: dict[str, int] = {}


class TrackedAlert(BaseModel):
    alert: Alert
    count: int = 1
    sends: list[AlertSend] = []
    state: str = "processed"
