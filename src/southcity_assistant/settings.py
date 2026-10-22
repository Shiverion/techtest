"""Validated configuration from environment / .env. Runtime config never holds the sa password."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .security.roles import Role


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "127.0.0.1"
    db_port: int = 14333
    db_name: str = "SouthCity"
    sales_db_user: str = "southcity_sales"
    sales_db_password: SecretStr
    mgmt_db_user: str = "southcity_mgmt"
    mgmt_db_password: SecretStr
    db_query_timeout_s: int = Field(5, ge=1, le=60)
    db_login_timeout_s: int = Field(5, ge=1, le=60)
    db_connect_retry_s: int = Field(0, ge=0, le=90)  # keep retrying a paused (serverless) database this long

    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.moonshot.ai/v1"
    llm_model: str = "kimi-k2.6"
    llm_timeout_s: float = Field(60, gt=0, le=300)
    llm_temperature: float | None = None
    llm_max_output_tokens: int = Field(1500, ge=256, le=8000)

    auth_mode: Literal["demo", "api_key"] = "demo"
    api_keys: dict[str, str] = {}

    max_rows: int = Field(200, ge=1, le=5000)
    max_result_bytes: int = Field(256_000, ge=1_000)
    max_concurrent_requests: int = Field(4, ge=1, le=64)
    rate_limit_per_minute: int = Field(30, ge=1)
    request_deadline_s: float = Field(120, ge=5, le=600)  # whole request: lookups, model calls, repair
    trust_proxy: bool = False  # take the client address from X-Real-IP / X-Forwarded-For (e.g. on Vercel)

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_keys(cls, value: object) -> object:
        if isinstance(value, str):
            value = json.loads(value or "{}")
        if isinstance(value, dict):
            for role in value.values():
                Role(role)  # reject unknown roles at startup
        return value

    def db_credentials(self, role: Role) -> tuple[str, str]:
        if role is Role.MANAGEMENT:
            return self.mgmt_db_user, self.mgmt_db_password.get_secret_value()
        return self.sales_db_user, self.sales_db_password.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
