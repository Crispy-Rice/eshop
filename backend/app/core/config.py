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

# PHONE_ENC_KEY 的默认值。它是个**合法的** 32 字节 base64，所以
# ``_validate_phone_enc_key`` 会放行、``DEV_PLACEHOLDER`` 前缀也拦不住 ——
# 只能单独拿它比对。漏配的后果是生产环境用一个公开已知的 AES 密钥加密所有
# 手机号，加密形同虚设（docs/13-schema.md §2 要求手机号加密存储）。
KNOWN_PHONE_ENC_KEY = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


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

    # ---------- 库存（docs/03-inventory.md）----------
    # 分片数。**默认 1，这是对 docs/03 §3.3 的有意偏离**，原因：
    #
    #   分片把可售量拆成 N 份，而 docs/03 §10 明确"不允许跨分片凑、整片扣"。
    #   于是任何超过单片余量的订单都会失败 —— 10 件库存分 4 片（3/3/2/2）时，
    #   买 4 件就下不了单，尽管库存充足。这是正确性问题，不是性能问题。
    #
    #   而分片想换来的收益（降低单个 key 上的冲突）在**单实例 Redis 上并不成立**：
    #   Lua 脚本是串行执行的，两个请求本就不会在脚本内部争用同一个 key。
    #   真正需要分片的是 Redis Cluster（不同分片落在不同节点），那是二期的事。
    #
    # 值 > 1 时上面的限制依然存在，只在"库存远大于单笔最大购买量"时才安全
    # （Quantity 上限 200，见 core/schemas.py）。要用请自行评估。
    inventory_shard_count: int = 1
    # 预占记录 TTL。要覆盖"下单 → 支付"的最长耗时，否则订单还没付记录就被清了
    inventory_lock_ttl_seconds: int = 7200
    # 售罄标记 TTL：存在即快速拒绝，省掉一次分片查询
    inventory_zero_marker_ttl: int = 3600
    # 每轮对账抽样的 SKU 数
    inventory_reconcile_sample: int = 200

    # ---------- 安全 ----------
    jwt_secret: str = DEV_PLACEHOLDER
    jwt_access_ttl_minutes: int = 30
    jwt_refresh_ttl_days: int = 14
    price_token_secret: str = DEV_PLACEHOLDER
    price_token_ttl_minutes: int = 30
    phone_hash_key: str = DEV_PLACEHOLDER
    # 必须是 32 字节的 base64（AES-256-GCM）
    phone_enc_key: str = KNOWN_PHONE_ENC_KEY
    password_min_length: int = 8

    # ---------- 支付 ----------
    payment_mock_enabled: bool = True
    # 演示环境没有真实渠道，却又要跑 production 的那一圈自检（密钥必须换掉、
    # /docs 必须关闭），所以给模拟支付留一个**显式**口子。默认 false：真要对外
    # 收款时忘了关 PAYMENT_MOCK_ENABLED，启动就会失败，而不是悄悄放行假支付。
    allow_mock_payment_in_prod: bool = False
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
        """生产环境的启动自检：密钥必须换掉，模拟支付要有显式授权。

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

        # 单独判 PHONE_ENC_KEY：它的默认值是合法的 32 字节 base64，
        # 上面那圈"前缀 + 长度"检查对它无效（见 KNOWN_PHONE_ENC_KEY 的注释）。
        if self.phone_enc_key == KNOWN_PHONE_ENC_KEY:
            problems.append("PHONE_ENC_KEY 仍是仓库里的公开默认值，加密手机号形同虚设")

        if self.payment_mock_enabled and not self.allow_mock_payment_in_prod:
            problems.append(
                "PAYMENT_MOCK_ENABLED=true 却没授权：真实上线请置为 false，"
                "演示环境请显式设置 ALLOW_MOCK_PAYMENT_IN_PROD=true"
            )

        if problems:
            raise ValueError("生产环境配置检查未通过：\n  - " + "\n  - ".join(problems))

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中可用 get_settings.cache_clear() 重置。"""
    return Settings()
