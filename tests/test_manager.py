"""Tests for recipient resolution, auth and the alert payload model (no network)."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from alert_manager import app as app_module
from alert_manager.config import Settings
from alert_manager.google_sheet_reader import write_schedule_cache
from alert_manager.manager import AlertManager
from alert_manager.models import Alert


def _manager_with_sheet(tmp_path: Path, oncall: dict[str, object]) -> AlertManager:
    sheet = tmp_path / "google-sheet.json"
    sheet.write_text(json.dumps(oncall), encoding="utf-8")
    settings = Settings(schedule_file=str(sheet))
    return AlertManager(settings)


def test_escalation_sends_to_all_contacts(tmp_path: Path) -> None:
    manager = _manager_with_sheet(
        tmp_path,
        {"schedule": {}, "contacts": {"Thomas": "111", "George": "222"}},
    )
    recipients = manager.get_alert_recipients(alert_count=4)
    assert sorted(recipients) == ["111", "222"]


def test_no_oncall_returns_empty(tmp_path: Path) -> None:
    manager = _manager_with_sheet(
        tmp_path,
        {"schedule": {}, "contacts": {"Thomas": "111"}},
    )
    assert manager.get_alert_recipients(alert_count=1) == []


def test_missing_schedule_returns_empty(tmp_path: Path) -> None:
    settings = Settings(schedule_file=str(tmp_path / "does-not-exist.json"))
    manager = AlertManager(settings)
    assert manager.get_alert_recipients(alert_count=1) == []
    assert manager.get_alert_recipients(alert_count=99) == []


def test_write_schedule_cache_creates_missing_file_and_parents(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "google-sheet.json"
    settings = Settings(schedule_file=str(path))
    data = {"schedule": {}, "contacts": {"OnCall": "111"}}

    written = write_schedule_cache(settings, data)

    assert written == path
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_alert_model_fields() -> None:
    alert = Alert(
        message="m", site_alias="beech", alert_alias="no_data", time_sent=1700000000
    )
    assert alert.message == "m"
    assert alert.site_alias == "beech"
    assert alert.alert_alias == "no_data"
    assert alert.time_sent == 1700000000


def test_require_token_enforced_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.settings, "api_token", SecretStr("secret"))
    with pytest.raises(HTTPException):
        app_module.require_token(None)
    with pytest.raises(HTTPException):
        app_module.require_token("Bearer wrong")
    with pytest.raises(HTTPException):
        app_module.require_token("secret")  # missing "Bearer " scheme
    assert app_module.require_token("Bearer secret") is None


def test_require_token_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.settings, "api_token", SecretStr(""))
    assert app_module.require_token(None) is None
