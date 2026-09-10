"""Environment-based configuration."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Meta WhatsApp Cloud API
    whatsapp_api_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str = "v20.0"

    # Club Costa Azul services API (socios / actividades / inscripciones)
    services_api_base_url: str
    bot_api_key: str = Field(min_length=1)

    # Authenticates requests forwarded by the webhook router.
    router_shared_secret: str = Field(min_length=32)

    # Shared sessions, per-phone locks, and inbound-message deduplication.
    session_redis_url: str = ""
    require_redis: bool = False
    session_ttl_seconds: int = 1800
    member_revalidate_seconds: int = 300

    # MongoDB (message storage)
    mongodb_url: str = ""

    # User-facing destinations.
    frontend_url: str
    club_contact_text: str = "Comunicate con administración del club."

    log_level: str = "INFO"


settings = Settings()
