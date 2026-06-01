from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Home Collection Mobile API"
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 2010
    app_debug: bool = False
    app_workers: int = 1

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str
    mysql_password: str
    mysql_db: str = "lead_management"
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 20
    mysql_pool_timeout: int = 30
    mysql_pool_recycle: int = 1800
    catalog_mysql_host: str | None = None
    catalog_mysql_port: int | None = None
    catalog_mysql_user: str | None = None
    catalog_mysql_password: str | None = None
    catalog_mysql_db: str = "bhasin_7001_new"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 720
    jwt_access_token_expire_seconds: int | None = None

    cors_origins: List[str] = Field(default_factory=lambda: ["*"])

    ssl_cert_file: str = "./certs/server.crt"
    ssl_key_file: str = "./certs/server.key"
    patient_documents_upload_base: str = "/home/arpraandhc/Desktop/arpra neo/arpra-neo/app/static/uploads/patient_documents"
    web_upload_mirror_root: str | None = None
    main_web_domain: str = "https://labmate.bhasinpathlabs.com:4672"
    apk_public_domain: str = "https://labmate.bhasinpathlabs.com:2010"
    booking_completion_lock_wait_sec: int = 3
    pepipost_wa_enabled: bool = True
    pepipost_wa_token: str | None = None
    pepipost_wa_timeout: int = 8
    pepipost_service_note_template: str = "newhomec"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @field_validator("mysql_db")
    @classmethod
    def validate_mysql_db(cls, value: str) -> str:
        if value != "lead_management":
            raise ValueError("Only lead_management database is allowed")
        return value

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}?charset=utf8mb4"
        )

    @property
    def catalog_mysql_url(self) -> str:
        host = self.catalog_mysql_host or self.mysql_host
        port = self.catalog_mysql_port or self.mysql_port
        user = self.catalog_mysql_user or self.mysql_user
        password = self.catalog_mysql_password or self.mysql_password
        return (
            f"mysql+pymysql://{user}:{password}"
            f"@{host}:{port}/{self.catalog_mysql_db}?charset=utf8mb4"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
