"""Unit tests for the core alert lifecycle: dedup, escalation, cap, ack.

All Telegram and Google calls are mocked, so these exercise the ported logic
(``send_alert`` / ``check_telegram_alerts``) without touching the network.
"""

import csv
import json
from typing import Any

import pytest
from pydantic import SecretStr

from alert_manager import manager as manager_module
from alert_manager.config import Settings
from alert_manager.manager import AlertManager


class FakeResponse:
    def __init__(
        self, status_code: int = 200, payload: Any = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self) -> Any:
        return self._payload


def _full_schedule(name: str) -> dict[str, dict[str, list[str]]]:
    """On-call schedule with ``name`` covering every weekday and hour."""
    return {str(d): {str(h): [name] for h in range(24)} for d in range(7)}


def _build_manager(
    tmp_path: Any,
    contacts: dict[str, str],
    schedule: dict[str, dict[str, list[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> AlertManager:
    sheet = tmp_path / "google-sheet.json"
    sheet.write_text(json.dumps({"schedule": schedule, "contacts": contacts}))
    settings = Settings(
        schedule_file=str(sheet),
        alert_log_file=str(tmp_path / "alerts.csv"),
        telegram_bot_token=SecretStr("test-token"),
    )
    # Don't hit Google on each send; the cached sheet written above is enough.
    monkeypatch.setattr(manager_module, "read_google_sheet", lambda s: None)
    return AlertManager(settings)


def _recording_post(posts: list[dict[str, Any]]) -> Any:
    """Fake sendMessage that records each post and returns a unique message_id."""
    state = {"next_id": 1}

    def fake_post(url: str, json: dict[str, Any]) -> FakeResponse:
        message_id = state["next_id"]
        state["next_id"] += 1
        posts.append(
            {"chat_id": json["chat_id"], "message_id": message_id, "text": json["text"]}
        )
        return FakeResponse(200, {"result": {"message_id": message_id}})

    return fake_post


def _reaction_update(
    chat_id: int, message_id: int, emoji: str = "👍", update_id: int = 1
) -> dict[str, Any]:
    return {
        "result": [
            {
                "update_id": update_id,
                "message_reaction": {
                    "chat": {"id": chat_id},
                    "message_id": message_id,
                    "user": {"id": chat_id, "is_bot": False},
                    "date": 1,
                    "old_reaction": [],
                    "new_reaction": [{"type": "emoji", "emoji": emoji}],
                },
            }
        ]
    }


def test_duplicate_external_alert_while_active_is_rejected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"Thomas": "111"}, _full_schedule("Thomas"), monkeypatch
    )

    # First external alert is sent; later identical ones are rejected while active.
    for _ in range(5):
        m.send_alert("zone cold", "beech", "no_data")

    assert len(m.active_telegram_alerts) == 1
    alert = next(iter(m.active_telegram_alerts.values()))
    assert alert.count == 1  # duplicates did not bump the count
    assert len(posts) == 1  # only the first send went out


def test_reminders_increment_count_and_cap_at_six(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"Thomas": "111"}, _full_schedule("Thomas"), monkeypatch
    )

    m.send_alert("zone cold", "beech", "no_data")  # external: count 1
    for _ in range(10):
        m.send_alert("zone cold", "beech", "no_data", reminder=True)

    assert len(m.active_telegram_alerts) == 1
    alert = next(iter(m.active_telegram_alerts.values()))
    assert alert.count == 6  # capped at max_alert_count
    assert len(posts) == 6  # initial send + 5 reminders, then no more
    assert len(alert.sends) == 6


def test_reminders_escalate_to_all_contacts_after_three(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path,
        {"OnCall": "111", "Other": "222"},
        _full_schedule("OnCall"),
        monkeypatch,
    )

    m.send_alert("zone cold", "beech", "no_data")  # count 1: on-call
    for _ in range(3):  # reminders -> counts 2, 3, 4
        m.send_alert("zone cold", "beech", "no_data", reminder=True)

    chat_ids = [p["chat_id"] for p in posts]
    assert chat_ids[:3] == ["111", "111", "111"]  # counts 1-3: on-call only
    assert sorted(chat_ids[3:]) == ["111", "222"]  # count 4: escalate to all


def test_thumbs_up_reaction_acknowledges(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.send_alert("zone cold", "beech", "no_data")
    send = next(iter(m.active_telegram_alerts.values())).sends[0]
    chat_id, message_id = next(iter(send.message_ids.items()))

    updates = _reaction_update(chat_id=int(chat_id), message_id=message_id, emoji="👍")
    monkeypatch.setattr(
        manager_module.requests, "get", lambda url, params: FakeResponse(200, updates)
    )

    m.check_telegram_alerts()
    assert m.active_telegram_alerts == {}  # 👍 on the alert message -> dropped


def test_non_thumbs_up_reaction_does_not_acknowledge(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.reminder_interval_seconds = 0  # don't rate-limit the re-send for this test
    m.send_alert("zone cold", "beech", "no_data")
    send = next(iter(m.active_telegram_alerts.values())).sends[0]
    chat_id, message_id = next(iter(send.message_ids.items()))

    # A neutral reaction (neither 👍 nor 👎) is not an acknowledgement.
    updates = _reaction_update(chat_id=int(chat_id), message_id=message_id, emoji="❤")
    monkeypatch.setattr(
        manager_module.requests, "get", lambda url, params: FakeResponse(200, updates)
    )

    m.check_telegram_alerts()
    assert len(m.active_telegram_alerts) == 1  # still active
    assert next(iter(m.active_telegram_alerts.values())).count == 2  # re-sent
    assert len(posts) == 2  # initial send + one re-send


def test_reminder_is_rate_limited_within_interval(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    monkeypatch.setattr(
        manager_module.requests,
        "get",
        lambda url, params: FakeResponse(200, {"result": []}),
    )
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.reminder_interval_seconds = 300  # default cadence
    m.send_alert("zone cold", "beech", "no_data")

    # A check moments later (no reaction) detects acks but must NOT re-send yet.
    m.check_telegram_alerts()

    assert len(m.active_telegram_alerts) == 1
    assert next(iter(m.active_telegram_alerts.values())).count == 1  # no reminder
    assert len(posts) == 1  # only the initial send


def test_thumbs_up_on_different_message_does_not_acknowledge(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.send_alert("zone cold", "beech", "no_data")
    send = next(iter(m.active_telegram_alerts.values())).sends[0]
    chat_id, message_id = next(iter(send.message_ids.items()))

    # 👍 on an unrelated message_id must not acknowledge this alert.
    updates = _reaction_update(
        chat_id=int(chat_id), message_id=message_id + 999, emoji="👍"
    )
    monkeypatch.setattr(
        manager_module.requests, "get", lambda url, params: FakeResponse(200, updates)
    )

    m.check_telegram_alerts()
    assert len(m.active_telegram_alerts) == 1  # still active


def _ban_via_thumbs_down(
    m: AlertManager, posts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> str:
    """Send an alert, 👎 it, run a check, and return the banned full_alias."""
    m.send_alert("zone cold", "beech", "no_data")
    full_alias = next(iter(m.active_telegram_alerts.keys()))
    send = m.active_telegram_alerts[full_alias].sends[0]
    chat_id, message_id = next(iter(send.message_ids.items()))
    updates = _reaction_update(chat_id=int(chat_id), message_id=message_id, emoji="👎")
    monkeypatch.setattr(
        manager_module.requests, "get", lambda url, params: FakeResponse(200, updates)
    )
    m.check_telegram_alerts()
    return full_alias


def test_thumbs_down_bans_and_blocks_future_alerts(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )

    full_alias = _ban_via_thumbs_down(m, posts, monkeypatch)

    assert m.active_telegram_alerts == {}  # 👎 cleared it
    assert full_alias in m.banned_alerts  # and banned the alias

    posts_before = len(posts)
    m.send_alert("zone cold", "beech", "no_data")  # new external attempt
    assert len(posts) == posts_before  # nothing sent
    assert m.active_telegram_alerts == {}  # not re-tracked


def test_clear_banned_alerts_allows_retrigger(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )

    _ban_via_thumbs_down(m, posts, monkeypatch)
    assert m.banned_alerts

    m.clear_banned_alerts()
    assert m.banned_alerts == set()

    m.send_alert("zone cold", "beech", "no_data")  # allowed again
    assert len(m.active_telegram_alerts) == 1


def _read_log(m: AlertManager) -> list[dict[str, str]]:
    with m.alert_log.path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_csv_logs_notified_row_once_per_alert(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )

    m.send_alert("zone cold", "Site 2", "no_data")

    rows = _read_log(m)
    assert len(rows) == 1
    assert rows[0]["alert_alias"] == "no_data"
    assert rows[0]["site_alias"] == "Site 2"
    assert rows[0]["message"] == "zone cold"
    assert rows[0]["state"] == "notified"
    assert rows[0]["time_received"].isdigit()

    # Duplicates while active do not add new rows.
    m.send_alert("zone cold", "Site 2", "no_data")
    assert len(_read_log(m)) == 1


def test_csv_marks_acknowledged_on_thumbs_up(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.send_alert("zone cold", "beech", "no_data")
    send = next(iter(m.active_telegram_alerts.values())).sends[0]
    chat_id, message_id = next(iter(send.message_ids.items()))

    updates = _reaction_update(chat_id=int(chat_id), message_id=message_id, emoji="👍")
    monkeypatch.setattr(
        manager_module.requests, "get", lambda url, params: FakeResponse(200, updates)
    )
    m.check_telegram_alerts()

    rows = _read_log(m)
    assert len(rows) == 1
    assert rows[0]["state"] == "acknowledged"


def test_csv_marks_muted_on_thumbs_down(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    posts: list[dict[str, Any]] = []
    monkeypatch.setattr(manager_module.requests, "post", _recording_post(posts))
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )

    _ban_via_thumbs_down(m, posts, monkeypatch)

    rows = _read_log(m)
    assert len(rows) == 1
    assert rows[0]["state"] == "muted"


def test_alerts_history_filters_by_time_range(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    m.alert_log.record_notified(100, "a1", "s1", "m1")
    m.alert_log.record_notified(200, "a2", "s2", "m2")
    m.alert_log.record_notified(300, "a3", "s3", "m3")

    in_range = m.alerts_history(150, 300)
    assert [r.time_received for r in in_range] == [200, 300]  # inclusive bounds
    assert all(isinstance(r.time_received, int) for r in in_range)

    assert len(m.alerts_history(100, 300)) == 3
    assert m.alerts_history(0, 50) == []  # nothing in range


def test_alerts_history_empty_when_no_log(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    assert m.alerts_history(0, 9999999999) == []  # no file written yet


def test_csv_keeps_at_most_100_rows(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _build_manager(
        tmp_path, {"OnCall": "111"}, _full_schedule("OnCall"), monkeypatch
    )
    for i in range(150):
        m.alert_log.record_notified(i, "a", "s", f"m{i}")

    rows = _read_log(m)
    assert len(rows) == 100  # capped
    assert rows[0]["time_received"] == "50"  # oldest kept (the first 50 dropped)
    assert rows[-1]["time_received"] == "149"  # newest kept
