import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException

from alert_manager.config import Settings
from alert_manager.manager import AlertManager
from alert_manager.models import Alert, TrackedAlert

settings = Settings()
manager = AlertManager(settings)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.api_token.get_secret_value()
    if not expected:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=401, detail="Invalid or missing bearer token"
        )


async def _active_alert_check_loop() -> None:
    while True:
        await asyncio.sleep(settings.check_interval_seconds)
        try:
            await asyncio.to_thread(manager.check_telegram_alerts)
        except Exception as e:
            print(f"Error in alert check loop: {e}")


async def _mute_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(settings.mute_clear_interval_seconds)
        try:
            await asyncio.to_thread(manager.clear_muted_alerts)
        except Exception as e:
            print(f"Error in mute cleanup loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not settings.api_token.get_secret_value():
        print("Warning: ALERT_MANAGER_API_TOKEN is not set")
    tasks = [
        asyncio.create_task(_active_alert_check_loop()),
        asyncio.create_task(_mute_cleanup_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="alert-manager", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "active_alerts": len(manager.active_telegram_alerts)}


@app.post("/new-alert")
async def new_alert(alert: Alert, _: None = Depends(require_token)) -> dict[str, str]:
    await asyncio.to_thread(manager.send_alert, alert)
    return {"status": "ok"}


@app.get("/alerts-history")
def alerts_history(start: int, end: int, _: None = Depends(require_token)) -> list[TrackedAlert]:
    return manager.alerts_history(start, end)
