__version__ = "0.1.0"


def main() -> None:
    import uvicorn

    from alert_manager.app import settings

    uvicorn.run(
        "alert_manager.app:app",
        host=settings.host,
        port=settings.port,
    )
