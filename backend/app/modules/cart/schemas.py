"""cart 模块的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, Quantity, SnowflakeId
from app.modules.cart.models import MAX_NUM_PER_SKU, MAX_SKUS_PER_CART

# 购物车项状态。**不入库**，每次列表实时算出来（docs/02 §4）
CART_VALID = 1
CART_INVALID = 2  # SKU 或 SPU 已被删除
CART_OFF_SHELF = 3  # 已下架
CART_NO_STOCK = 4  # 可售库存为 0

CART_STATUS_TEXT: dict[int, str] = {
    CART_VALID: "有效",
    CART_INVALID: "失效",
    CART_OFF_SHELF: "已下架",
    CART_NO_STOCK: "无货",
}


# ============================================================
# 请求
# ============================================================
class CartAddRequest(CamelModel):
    sku_id: SnowflakeId
    num: Quantity = 1
    source: int = Field(default=1, ge=1, le=3, description="1详情页 2列表页 3活动页")


class CartNumRequest(CamelModel):
    """修改数量 —— **SET 语义**（设为指定值），不是累加。"""

    num: int = Field(ge=1, le=MAX_NUM_PER_SKU, strict=True)


class CartSkuIdsRequest(CamelModel):
    """批量操作的目标。为空表示"全部"。"""

    sku_ids: list[SnowflakeId] = Field(default_factory=list, max_length=MAX_SKUS_PER_CART)


class CartSelectRequest(CartSkuIdsRequest):
    selected: bool


# ============================================================
# 响应
# ============================================================
class CartItemOut(CamelModel):
    sku_id: SnowflakeId
    spu_id: SnowflakeId
    shop_id: SnowflakeId

    title: str
    spec_text: str
    cover_image: str
    sku_code: str

    num: int
    selected: bool

    # 金额一律是「分」
    price: int = Field(description="**当前**单价，结算以此为准")
    price_snapshot: int = Field(description="加购时的单价，只用于降价提醒")
    price_down: int = Field(
        default=0, description="较加购时降了多少（分）。> 0 才展示"
    )
    item_amount: int = Field(description="当前单价 × 数量")

    status: int
    status_text: str
    available: int = Field(description="可售库存，0 表示无货")


class CartShopGroupOut(CamelModel):
    """按店铺分组。前端按店铺分块渲染，每块一个结算单位。"""

    shop_id: SnowflakeId
    shop_name: str
    items: list[CartItemOut]
    selected_count: int = Field(description="该店已勾选的**件数**（数量之和）")
    selected_amount: int = Field(description="该店已勾选的金额小计（分）")


class CartOut(CamelModel):
    groups: list[CartShopGroupOut]
    invalid_items: list[CartItemOut] = Field(
        description="失效/下架的商品单独放，**不参与合计**"
    )
    total_count: int = Field(description="整车已勾选的件数")
    total_amount: int = Field(description="整车已勾选的金额（分）")
    sku_count: int = Field(description="购物车里有几种 SKU，用于整车上限提示")


class CartCountOut(CamelModel):
    """顶栏角标。单独一个轻接口，不必为了显示数字拉整个购物车。"""

    count: int = Field(description="购物车里有几种 SKU")
