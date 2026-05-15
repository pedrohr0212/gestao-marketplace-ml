# config.py — Nexora Gestão Marketplace ML
from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    ml_client_id: str
    ml_client_secret: str
    ml_redirect_uri: str
    database_url: str
    redis_url: str
    secret_key: str
    allowed_origins: str = "https://pedrohr0212.github.io"
    environment: str = "production"

    class Config:
        env_file = ".env"

@lru_cache()
def get_settings():
    return Settings()
