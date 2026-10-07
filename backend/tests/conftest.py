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
    # AI 助手 / 店小蜜。★ 这几张**必须清**：``bot_turn`` 的幂等闩锁是
    #   ``source_message_id``（跨用例唯一），留着旧行会让"这一轮该不该重投"
    #   变成用例执行顺序的函数；``usage_daily`` 不清则额度和统计会串。
    "assistant.bot_turn",
    "assistant.message",
    "assistant.conversation",
    "assistant.usage_daily",
    "assistant.shop_faq",
    "assistant.shop_setting",
    # 客服会话与站内信（support 的表引用自己的 ticket_no，CASCADE 会处理顺序）
    "support.ticket_state_flow",
    "support.ticket_message",
    "support.ticket",
    "notify.site_message",
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
    # Banner 是运营建的内容，用例会自己造 —— 不清的话会串到下一个用例
    "promotion.banner",
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
    "account.user_state_flow",
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
        # ★ 两张**单行配置**（promotion.site_theme / promotion.site_contact）刻意都不进
        #   _TRUNCATE —— 那两行是迁移插进去的，删掉之后 UPDATE 就会影响 0 行，
        #   "改皮肤"和"存联系方式"都会**静默失效**（接口回 200，库里没变）。
        #   用例之间要的是**复位**，不是清空。
        await session.execute(text("UPDATE promotion.site_theme SET skin = 'neutral'"))
        await session.execute(
            text(
                "UPDATE promotion.site_contact"
                " SET service_email = NULL, service_phone = NULL, service_hours = NULL"
            )
        )
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


async def ensure_freight_template(client: AsyncClient, access_token: str) -> str:
    """给店铺**再加**一条运费模板并返回 id。

    ★ 新店开出来就自带一条默认模板（``account.create_shop`` 会调
      ``freight.ensure_default_template``），所以这个函数只用在"需要第二条模板"
      的用例上 —— 比如验证设为默认会顶掉旧的、或者默认被摘掉后重新配一条。
    """
    resp = await client.post(
        "/api/merchant/freight/templates",
        json={"name": "测试快递模板", "firstPrice": 800, "addPrice": 300},
        headers=auth_header(access_token),
    )
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
