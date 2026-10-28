from collections.abc import AsyncGenerator

from fastapi import FastAPI, HTTPException, status

from app.config import settings
from app.db.session import check_database_connection, engine
from app.errors import register_exception_handlers


async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    description="An event ingestion and webhook delivery platform.",
    version=settings.app_version,
    lifespan=lifespan,
)

register_exception_handlers(app)


@app.get("/health/live", tags=["Health"])
async def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["Health"])
async def health_ready() -> dict[str, str]:
    if not await check_database_connection():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        )

    return {"status": "ready"}
