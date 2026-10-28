import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import APIKey
from app.security.api_keys import generate_api_key, hash_api_key


async def create_api_key(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    name: str,
) -> tuple[APIKey, str]:
    """Create an API key record and return it with its plaintext secret."""
    raw_key = generate_api_key()
    api_key = APIKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=raw_key[:16],
        key_hash=hash_api_key(raw_key),
    )
    db.add(api_key)
    await db.flush()
    return api_key, raw_key
