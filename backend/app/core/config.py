"""应用配置：全部来自环境变量，见 deploy/.env.example。

约定（docs/16-deployment.md §8）：
- 变量名 = 字段名的大写形式（不区分大小写）
- 密钥在 ``app_env=production`` 时不允许保留 dev 默认值，启动即失败
"""

from __future__ import annotations

import base64
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 生产环境绝不允许出现的占位值前缀
DEV_PLACEHOLDER = "dev-only"

# 对称密钥的最小字节数（RFC 7518 §3.2：HS256 密钥应 >= 哈希输出长度）
MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 本地开发读 backend/.env；容器里由 env_file 或 compose 的 environment 注入
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_env: Literal["development", "production"] = "development"
    app_version: str = "dev"
    log_level: str = "INFO"
    api_workers: int = 1

    # ---------- PostgreSQL ----------
    database_url: str = "postgresql+asyncpg://eshop_app:dev-only-app-password@127.0.0.1:5432/eshop"
    # 迁移用（需要 DDL 权限，用 owner 角色）；为空时回落到 database_url
    migration_database_url: str | None = None
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_recycle_seconds: int = 1800

    # ---------- Redis ----------
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_max_connections: int = 50
    # 超时要短：Redis 卡住时应用要能快速走降级（docs/14 §5.2）
    redis_socket_timeout: float = 0.5

    # ---------- 安全 ----------
    jwt_secret: str = DEV_PLACEHOLDER
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 14
    price_token_secret: str = DEV_PLACEHOLDER
    price_token_ttl_minutes: int = 30
    phone_hash_key: str = DEV_PLACEHOLDER
    # 必须是 32 字节的 base64（AES-256-GCM）
    phone_enc_key: str = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    password_min_length: int = 8

    # ---------- 支付 ----------
    payment_mock_enabled: bool = True
    mock_pay_secret: str = DEV_PLACEHOLDER
    pay_notify_base_url: str = "http://127.0.0.1:8000"

    # ---------- 存储 ----------
    media_root: str = "./data/media"
    media_url_prefix: str = "/media/"
    media_max_image_mb: int = 5

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @field_validator("phone_enc_key")
    @classmethod
    def _validate_phone_enc_key(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:
            raise ValueError("PHONE_ENC_KEY 不是合法的 base64") from exc
        if len(raw) != 32:
            raise ValueError(f"PHONE_ENC_KEY 解码后必须是 32 字节，当前 {len(raw)} 字节")
        return v

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        level = v.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"LOG_LEVEL 非法: {v}")
        return level

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """生产环境的启动自检：密钥必须换掉，模拟支付必须关闭。

        对应 docs/16-deployment.md §9 上线检查清单的第 3 条。
        """
        if not self.is_production:
            return self

        problems: list[str] = []

        for name, value in (
            ("JWT_SECRET", self.jwt_secret),
            ("PRICE_TOKEN_SECRET", self.price_token_secret),
            ("PHONE_HASH_KEY", self.phone_hash_key),
            ("MOCK_PAY_SECRET", self.mock_pay_secret),
        ):
            if not value or DEV_PLACEHOLDER in value:
                problems.append(f"{name} 仍在使用开发默认值")
            elif len(value.encode()) < MIN_SECRET_BYTES:
                # HS256 的密钥短于 32 字节会明显降低安全性（RFC 7518 §3.2）
                problems.append(
                    f"{name} 太短（{len(value.encode())} 字节），至少需要 {MIN_SECRET_BYTES} 字节"
                )

        if self.payment_mock_enabled:
            problems.append("PAYMENT_MOCK_ENABLED 必须为 false 才能接入真实渠道上线")

        if problems:
            raise ValueError("生产环境配置检查未通过：\n  - " + "\n  - ".join(problems))

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中可用 get_settings.cache_clear() 重置。"""
    return Settings()
