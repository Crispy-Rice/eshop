"""inventory 模块的请求/响应模型。

字段命名约定见 docs/15 §1：Python 侧 snake_case，JSON 侧 camelCase（自动转换），
雪花 ID 序列化成字符串。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel, SnowflakeId

# 库存展示档位的分界线（docs/03 §9）
DISPLAY_PLAIN_THRESHOLD = 100  # 超过它就只显示"有货"
DISPLAY_URGENT_THRESHOLD = 10  # 小于等于它用更紧的档位


def display_stock_text(available: int) -> tuple[str, int | None]:
    """把真实可售量转成**展示档位**。返回 ``(文案, 展示数量)``。

    真实值会**向上取整到有意义的档位**再展示：真实 3 件显示"仅剩 5 件以内"。

    这么做的原因（docs/03 §9）：低库存提示是双刃剑，提醒用户的同时也把真实
    库存暴露给了爬虫和竞品。取整既保留紧迫感，又不泄露精确数字。
    """
    if available <= 0:
        return "已售罄", 0
    if available > DISPLAY_PLAIN_THRESHOLD:
        return "有货", None
    if available <= DISPLAY_URGENT_THRESHOLD:
        # 1..10 → 向上取到 5 的倍数（5 或 10）
        return f"仅剩 {((available + 4) // 5) * 5} 件以内", ((available + 4) // 5) * 5
    # 11..100 → 向上取到 10 的倍数
    return f"仅剩 {((available + 9) // 10) * 10} 件以内", ((available + 9) // 10) * 10


# ============================================================
# 仓库
# ============================================================


class WarehouseCreateRequest(CamelModel):
    name: str = Field(min_length=2, max_length=64)
    region_code: str = Field(default="", max_length=16, description="区划码，运费计算用")


class WarehouseOut(CamelModel):
    id: SnowflakeId
    name: str
    region_code: str
    is_default: bool
    status: int
    created_at: datetime


# ============================================================
# 库存
# ============================================================


class StockItemOut(CamelModel):
    sku_id: SnowflakeId
    warehouse_id: SnowflakeId
    warehouse_name: str
    # 来自 product 模块（只读拼接，不是本模块的表）
    sku_code: str
    spec_text: str
    spu_title: str

    total: int
    available: int
    locked: int
    frozen: int
    updated_at: datetime


class StockListOut(CamelModel):
    items: list[StockItemOut]
    next_cursor: str | None = None
    has_more: bool = False


class StockAdjustRequest(CamelModel):
    """手工调整。``delta`` 可正可负，单位「件」。"""

    sku_id: SnowflakeId
    warehouse_id: SnowflakeId
    delta: int = Field(description="正数入库、负数出库", strict=True)
    remark: str | None = Field(default=None, max_length=255)


class StockAdjustOut(CamelModel):
    sku_id: SnowflakeId
    warehouse_id: SnowflakeId
    before_qty: int
    after_qty: int
    available: int


# ============================================================
# 流水
# ============================================================


class StockFlowOut(CamelModel):
    id: int
    sku_id: SnowflakeId
    warehouse_id: SnowflakeId
    order_no: str | None
    change_type: int
    change_type_text: str
    num: int
    before_qty: int
    after_qty: int
    operator: str | None
    remark: str | None
    created_at: datetime


class StockFlowListOut(CamelModel):
    items: list[StockFlowOut]
    next_cursor: str | None = None
    has_more: bool = False


# ============================================================
# 买家侧：库存展示
# ============================================================


class SkuStockDisplayOut(CamelModel):
    """商品详情页显示的库存档位。**不回传真实库存**。"""

    sku_id: SnowflakeId
    text: str = Field(description="有货 / 仅剩 N 件以内 / 已售罄")
    sold_out: bool
