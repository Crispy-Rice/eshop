"""测试夹具。

集成测试连的是**真实的 PostgreSQL 与 Redis**（独立库 ``eshop_test``），
不 mock 数据库 —— 唯一约束、行锁、CHECK 约束、并发行为，只有真库才测得出来
（docs/16-deployment.md §3.3）。

准备测试库（只需一次）::

    docker exec eshop-postgres psql -U eshop_owner -d eshop \\
      -c "CREATE DATABASE eshop_test OWNER eshop_owner;"
    docker exec eshop-postgres psql -U eshop_owner -d eshop_test \\
      -c "CREATE EXTENSION IF NOT EXISTS pg_trgm, btree_gin, pg_stat_statements;"

之后跑 ``uv run pytest`` 会自动把测试库迁移到最新版本。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = "postgresql+asyncpg://eshop_owner:dev-only-owner-password@127.0.0.1:5432/eshop_test"

# ★ 必须在导入 app 之前设置。
#   配置是进程级单例（get_settings 有 lru_cache），引擎在首次连接时才创建；
#   环境变量的优先级高于 .env 文件，所以这里能覆盖 backend/.env 里的配置。
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["MIGRATION_DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.crypto import phone_hash  # noqa: E402
from app.core.db import dispose_engine, get_session_factory  # noqa: E402
from app.core.redis import close_redis, get_redis  # noqa: E402
from app.core.snowflake import start_snowflake, stop_snowflake  # noqa: E402
from app.main import app  # noqa: E402

# 每个用例前清空的表（TRUNCATE ... CASCADE 会自动处理外键顺序）
_TRUNCATE = (
    "ops.alert",
    "review.review_reply",
    "review.review",
    "aftersale.refund_item",
    "aftersale.refund_order",
    "payment.payment_refund",
    "payment.mock_channel_trade",
    "payment.payment",
    "trade.order_state_flow",
    "trade.delivery_item",
    "trade.delivery_order",
    "trade.order_discount_snapshot",
    "trade.order_item",
    "trade.order_sub",
    "trade.order_main",
    "promotion.coupon_flow",
    "promotion.coupon_receive_log",
    "promotion.coupon_user_quota",
    "promotion.coupon_code",
    "promotion.coupon_template",
    "promotion.promo_activity",
    "freight.freight_exclude_region",
    "freight.sku_freight_bind",
    "freight.freight_region_rule",
    "freight.freight_template",
    "cart.cart_item",
    "inventory.stock_flow",
    "inventory.stock_biz_key",
    "inventory.sku_stock",
    "inventory.warehouse",
    "product.sku_spec",
    "product.spec_value",
    "product.spec_group",
    "product.sku",
    "product.spu",
    "product.category",
    "account.user_address",
    "account.shop_member",
    "account.shop",
    "account.refresh_token",
    "account.user",
    "core.local_message",
)


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    """把测试库迁移到最新版本。同步 fixture，跑在事件循环之外。"""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
async def app_runtime(migrated_db: None) -> AsyncIterator[None]:
    """初始化雪花 ID 生成器（需要 Redis），结束时释放连接。

    httpx 的 ASGITransport 不会触发 FastAPI 的 lifespan，所以这里手工做。
    """
    await start_snowflake(get_redis())
    yield
    await stop_snowflake()
    await close_redis()
    await dispose_engine()


@pytest.fixture(autouse=True)
async def clean_tables(app_runtime: None) -> AsyncIterator[None]:
    """每个用例前清空数据，保证用例之间互不影响。"""
    async with get_session_factory()() as session, session.begin():
        await session.execute(text(f"TRUNCATE {', '.join(_TRUNCATE)} CASCADE"))
    yield


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """直接打 ASGI 应用的 HTTP 客户端，不走网络。"""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def session() -> AsyncIterator[object]:
    """直接查库用（校验落库结果、加密是否正确等）。"""
    async with get_session_factory()() as s:
        yield s


# ============================================================
# 便捷函数
# ============================================================
TEST_PHONE = "13800138000"
TEST_PASSWORD = "Str0ng-Pass!2026"


async def register(client: AsyncClient, phone: str = TEST_PHONE, password: str = TEST_PASSWORD) -> dict:
    resp = await client.post(
        "/api/auth/register", json={"phone": phone, "password": password, "nickname": "测试用户"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def open_shop(client: AsyncClient, access_token: str, name: str = "测试旗舰店") -> str:
    """开店并返回 shop_id。"""
    resp = await client.post("/api/merchant/shop", json={"name": name}, headers=auth_header(access_token))
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


async def make_admin(client: AsyncClient, session, phone: str = "13900139001") -> dict:
    """造一个平台管理员。

    角色的唯一来源是数据库（token 里带 role），所以改完角色要重新登录
    才能拿到带 admin 的 token。
    """
    await register(client, phone=phone)
    await session.execute(
        text("UPDATE account.user SET role = 'admin' WHERE phone_hash = :h"),
        {"h": phone_hash(phone)},
    )
    await session.commit()

    resp = await client.post("/api/auth/login", json={"phone": phone, "password": TEST_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def api_code(body: dict) -> str:
    return body["code"]
