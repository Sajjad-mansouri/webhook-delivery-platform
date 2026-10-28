import argparse
import asyncio

from app.db.session import AsyncSessionLocal
from app.models.tenant import Tenant
from app.schemas.tenant import TenantCreate
from app.services.api_keys import create_api_key


async def bootstrap_tenant(
    *,
    name: str,
    slug: str,
    key_name: str,
) -> None:
    tenant_data = TenantCreate(name=name, slug=slug)

    async with AsyncSessionLocal() as db:
        async with db.begin():
            tenant = Tenant(
                name=tenant_data.name,
                slug=tenant_data.slug,
            )
            db.add(tenant)
            await db.flush()

            _, raw_key = await create_api_key(
                db,
                tenant_id=tenant.id,
                name=key_name,
            )

        # The transaction has committed before the secret is displayed.
        print(f"Tenant ID: {tenant.id}")
        print(f"Tenant slug: {tenant.slug}")
        print(f"API key: {raw_key}")
        print("Save this API key securely; it will not be shown again.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a HookRelay tenant and its initial API key."
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--key-name", default="Initial key")
    args = parser.parse_args()

    asyncio.run(
        bootstrap_tenant(
            name=args.name,
            slug=args.slug,
            key_name=args.key_name,
        )
    )


if __name__ == "__main__":
    main()
