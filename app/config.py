from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Webhook Delivery Platform"
    app_version: str = "0.1.0"
    debug: bool = False
    database_url: str
    webhook_encryption_key: SecretStr | None = None
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
