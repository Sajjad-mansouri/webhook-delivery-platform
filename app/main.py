from fastapi import FastAPI

from app.config import settings

app = FastAPI(
    title=settings.app_name,
    description="An event ingestion and webhook delivery platform.",
    version=settings.app_version,
)


@app.get("/health/live", tags=["Health"])
async def health_live() -> dict[str, str]:
    return {"status": "alive"}
