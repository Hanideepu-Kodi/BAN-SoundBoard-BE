from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    supabase_url: str
    supabase_service_role_key: str
    supabase_jwt_secret: str
    database_url: str
    supabase_storage_bucket: str = "sounds"
    signed_url_ttl_seconds: int = 3600
    frontend_url: str = "http://localhost:3000"

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
