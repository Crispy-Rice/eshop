"""inventory 模块的请求/响应模型。

字段命名约定见 docs/15 §1：Python 侧 snake_case，JSON 侧 camelCase（自动转换），
雪花 ID 序列化成字符串。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel, SnowflakeId
from app.modules.inventory.models import WAREHOUSE_DISABLED, WAREHOUSE_ENABLED

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

# 区划码的合法形态：2 位省 / 4 位市 / 6 位区。仓自己的地址允许留空（= 还没填）
_ADDRESS_CODE_PATTERN = r"^(\d{2}|\d{4}|\d{6})?$"
# 覆盖区划多一个 "0"（全国兜底），且**不允许留空** —— 空串会匹配一切，
# 那比"没配"危险得多（见 routing.pick_rule）
_RULE_CODE_PATTERN = r"^(0|\d{2}|\d{4}|\d{6})$"


class WarehouseCreateRequest(CamelModel):
    name: str = Field(min_length=2, max_length=64)
    region_code: str = Field(
        default="", max_length=16, pattern=_ADDRESS_CODE_PATTERN, description="最细一级的区划码"
    )
    province: str = Field(default="", max_length=32)
    city: str = Field(default="", max_length=32)
    district: str = Field(default="", max_length=32)
    detail: str = Field(default="", max_length=255, description="详细地址（街道门牌）")
    contact_name: str = Field(default="", max_length=64)
    contact_phone: str = Field(default="", max_length=20)


class WarehouseUpdateRequest(CamelModel):
    """部分更新：只写传了的字段（照 ``account.ShopUpdateRequest`` 的取舍）。

    地址字段传空串表示**清空**，所以不能拿 ``None`` 当"不改"以外的东西 ——
    ``None`` 就是"没传"（``model_fields_set`` 判）。
    """

    name: str | None = Field(default=None, min_length=2, max_length=64)
    region_code: str | None = Field(default=None, max_length=16, pattern=_ADDRESS_CODE_PATTERN)
    province: str | None = Field(default=None, max_length=32)
    city: str | None = Field(default=None, max_length=32)
    district: str | None = Field(default=None, max_length=32)
    detail: str | None = Field(default=None, max_length=255)
    contact_name: str | None = Field(default=None, max_length=64)
    contact_phone: str | None = Field(default=None, max_length=20)


class RegionRuleOut(CamelModel):
    id: SnowflakeId
    region_code: str
    region_level: int = Field(description="1省 2市 3区，由码长推导，仅展示用")


class WarehouseOut(CamelModel):
    id: SnowflakeId
    name: str
    region_code: str
    province: str
    city: str
    district: str
    detail: str
    contact_name: str
    contact_phone: str
    is_default: bool
    status: int
    created_at: datetime
    # 该仓覆盖的区划。列表接口一并给全（一家店没几个仓、也没几条规则），
    # 省得后台列表为了展示"覆盖区域"再逐个拉一遍
    rules: list[RegionRuleOut] = Field(default_factory=list)


class WarehouseCreatedOut(WarehouseOut):
    """建仓结果。多带一个"顺手预建了多少条库存行"。

    ★ 建仓会调 ``ensure_stock_rows`` 给该店全部 SKU 在这个仓补 0 库存行 ——
      否则商家得先想到"去库存页给新仓铺货"，不然路由过来的订单会整单失败。
      返回这个数字是为了告诉他**下一步该去库存页填数量**。
    """

    stocked_skus: int = 0


class RegionRulesReplaceRequest(CamelModel):
    """**整体替换**这个仓的覆盖区划（照 freight 的 region rules 编辑方式）。

    整体替换而不是增删改：后台那一屏就是一个多选，全量提交最不容易出错。
    """

    rules: list[str] = Field(
        default_factory=list,
        max_length=200,
        description='区划码列表；"0" = 全国兜底',
    )


class WarehouseStatusRequest(CamelModel):
    """启用 / 停用。默认仓不允许停用（路由的兜底靠它）。"""

    status: int = Field(ge=WAREHOUSE_ENABLED, le=WAREHOUSE_DISABLED)


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
