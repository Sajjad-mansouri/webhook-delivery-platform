from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from app.api.v1.endpoints import router as endpoint_router
from app.api.v1.events import router as event_router
from app.config import settings
from app.db.session import check_database_connection, engine
from app.errors import register_exception_handlers
from app.models.tenant import Tenant
from app.schemas.tenant import TenantRead
from app.security.dependencies import get_current_tenant


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
app.include_router(endpoint_router)
app.include_router(event_router)


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


@app.get(
    "/api/v1/auth/me",
    response_model=TenantRead,
    tags=["Authentication"],
)
async def read_authenticated_tenant(
    current_tenant: Annotated[
        Tenant,
        Depends(get_current_tenant),
    ],
) -> Tenant:
    return current_tenant
