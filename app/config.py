"""Environment-based configuration."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Meta WhatsApp Cloud API
    whatsapp_api_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str = "v20.0"
    whatsapp_list_body_limit: int = Field(default=1024, ge=1024, le=4096)

    # Club Costa Azul services API (socios / actividades / inscripciones)
    services_api_base_url: str
    bot_api_key: str = ""

    # Shared secret the webhook router must send as X-Router-Secret
    router_secret: str = ""

    # MongoDB (message storage)
    mongodb_url: str

    log_level: str = "INFO"


settings = Settings()
