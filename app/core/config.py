from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://importguard:local-password@127.0.0.1:5439/importguard"
    redis_url: str = "redis://127.0.0.1:6389/0"
    s3_endpoint: str = "http://127.0.0.1:9009"
    s3_access_key: str = "importguard-local"
    s3_secret_key: str = "local-storage-password"
    s3_bucket: str = "importguard"
    cookie_secure: bool = False
    max_upload_bytes: int = Field(10485760, gt=0)
    max_rows: int = Field(50000, gt=0)
    max_columns: int = Field(50, gt=0)
    batch_size: int = Field(500, gt=0)
    lease_seconds: int = Field(120, ge=5)
    max_job_attempts: int = Field(5, ge=1)
    session_hours: int = Field(24, ge=1)
    enable_failure_injection: bool = False
    demo_seed_enabled: bool = False

    @model_validator(mode="after")
    def production_safety(self):
        if self.app_env == "production":
            if not self.cookie_secure or self.demo_seed_enabled or self.enable_failure_injection:
                raise ValueError(
                    "Production requires secure cookies and disabled demo/failure injection"
                )
            if self.s3_secret_key == "local-storage-password":
                raise ValueError("Configure production storage credentials")
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL with Psycopg 3 is required")
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
