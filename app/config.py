"""Environment-backed application configuration."""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings intentionally keep secrets out of MongoDB and backup data."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(min_length=20)
    owner_user_ids: str
    mongodb_uri: str
    mongodb_db_name: str = "telegram_campaign_orchestrator"
    run_mode: Literal["webhook", "polling"] = "polling"
    public_base_url: str | None = None
    koyeb_public_domain: str | None = None
    webhook_path_secret: str | None = None
    webhook_secret_token: str | None = None
    broadcast_send_rps: float = Field(default=20, gt=0)
    broadcast_global_api_rps: float = Field(default=25, gt=0)
    broadcast_workers: int = Field(default=20, ge=1, le=100)
    delivery_lease_seconds: int = Field(default=90, ge=15)
    scheduler_lease_seconds: int = Field(default=30, ge=10)
    scheduler_tick_seconds: float = Field(default=2, gt=0)
    telegram_request_timeout_seconds: float = Field(default=20, gt=0)
    max_transient_attempts: int = Field(default=3, ge=1, le=10)
    # The operator and primary deployment are based in Nairobi.  Individual
    # owners can still choose another IANA timezone in Settings.
    default_timezone: str = "Africa/Nairobi"
    allow_paid_broadcast: bool = False
    auto_backup_enabled: bool = True
    auto_backup_every_new_channels: int = Field(default=100, ge=1)
    auto_backup_interval_hours: int = Field(default=168, ge=1)
    log_level: str = "INFO"
    live_test_enabled: bool = False
    live_test_channel_ids: str = ""

    @field_validator("owner_user_ids")
    @classmethod
    def valid_owner_ids(cls, value: str) -> str:
        ids = [item.strip() for item in value.split(",") if item.strip()]
        if not ids or any(not item.lstrip("-").isdigit() for item in ids):
            raise ValueError("OWNER_USER_IDS must be one or more comma-separated numeric Telegram IDs")
        return ",".join(ids)

    @model_validator(mode="after")
    def validate_broadcast_limits(self) -> Settings:
        if not self.allow_paid_broadcast and self.broadcast_send_rps > 30:
            raise ValueError("BROADCAST_SEND_RPS cannot exceed 30 without ALLOW_PAID_BROADCAST=true")
        if self.run_mode == "webhook":
            if not self.resolved_public_base_url or not self.webhook_path_secret or not self.webhook_secret_token:
                raise ValueError(
                    "webhook mode requires PUBLIC_BASE_URL (or KOYEB_PUBLIC_DOMAIN), "
                    "WEBHOOK_PATH_SECRET, and WEBHOOK_SECRET_TOKEN"
                )
        return self

    @cached_property
    def owner_ids(self) -> frozenset[int]:
        return frozenset(int(value) for value in self.owner_user_ids.split(","))

    @property
    def resolved_public_base_url(self) -> str | None:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.koyeb_public_domain:
            return f"https://{self.koyeb_public_domain}".rstrip("/")
        return None

    @property
    def webhook_url(self) -> str | None:
        if not self.resolved_public_base_url or not self.webhook_path_secret:
            return None
        return f"{self.resolved_public_base_url}/telegram/webhook/{self.webhook_path_secret}"
