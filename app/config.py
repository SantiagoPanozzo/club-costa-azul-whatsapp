"""Environment-based configuration."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Meta WhatsApp Cloud API
    whatsapp_api_token: str
    whatsapp_phone_number_id: str
    whatsapp_api_version: str = "v20.0"

    # Club Costa Azul services API (socios / actividades / inscripciones)
    services_api_base_url: str

    log_level: str = "INFO"


settings = Settings()
