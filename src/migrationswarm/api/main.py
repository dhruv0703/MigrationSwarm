"""FastAPI application and health endpoint."""

from fastapi import FastAPI

from migrationswarm import __version__
from migrationswarm.config.settings import get_settings

settings = get_settings()
app = FastAPI(title=settings.app_name, version=__version__)


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a lightweight service health response."""
    return {"status": "ok", "service": "migrationswarm"}
