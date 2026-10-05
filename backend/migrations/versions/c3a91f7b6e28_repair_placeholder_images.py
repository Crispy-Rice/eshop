"""repair placeholder images

把"灰色占位图被当成真实图片存进库"这件事收拾干净。

背景：后台发布页曾经用一段内联 SVG data URI（``DEFAULT_IMAGE``）预填主图和
SKU 封面图来满足"非空"校验，商家没上传图时这段 URI 就被当成图片存了下来。
它渲染出来是一块灰底，看着像"图挂了"，而且因为它是**一张能成功加载的图**，
前端的 ``onImageError`` 兜底根本不会触发 —— 从商品一路传染到购物车、
结算页、下单快照（``order_item.cover_image_snap``），以及从快照派生的
售后单（``refund_item.cover_image_snap``）。

老数据里还有一批订单快照存的是 ``/media/placeholder.svg``，那个文件在仓库里
根本不存在，同样是 404 之后靠前端兜底。

这里做两件事：

1. 把商品表里 ``data:`` 开头的值清成空串 —— 展示层对空值会回落成主图/占位图
2. 把订单与售后快照里的占位值，按**当前**商品图回填（SKU 封面优先，否则主图）

★ 只动明确的占位值（``data:%`` 与那个不存在的文件路径），真实图片 URL 一律不碰。
★ 回填用的是"当前"商品图而不是下单时的图 —— 快照本应冻结在那一刻，但那时存的
  本来就是一张灰块，没有信息量可言；换成当前真图至少是对的。商品已被软删或
  SKU 已不存在的行清空，交给展示层兜底。

Revision ID: c3a91f7b6e28
Revises: e5b91c4a7d38
Create Date: 2026-10-04 21:40:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a91f7b6e28"
down_revision: Union[str, Sequence[str], None] = "e5b91c4a7d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_placeholder(col: str) -> str:
    """判定占位值的条件。写成函数是为了两处回填共用同一份口径。"""
    return f"({col} LIKE 'data:%' OR {col} = '/media/placeholder.svg')"


# 按 sku_id 取"当前该展示的图"：SKU 封面优先，空则退商品主图。
# 已软删的商品不算 —— 与读侧同一口径（product.service.list_deleted_sku_ids）。
_ALIVE_IMAGE = """
    SELECT k.id AS sku_id, COALESCE(NULLIF(k.cover_image, ''), s.main_image) AS img
    FROM product.sku k
    JOIN product.spu s ON s.id = k.spu_id
    WHERE s.deleted = false
"""

_ALIVE_SKU_EXISTS = """
    SELECT 1 FROM product.sku k
    JOIN product.spu s ON s.id = k.spu_id
    WHERE k.id = t.sku_id AND s.deleted = false
"""


def _repair_snapshots(table: str) -> None:
    """把某张表里仍是占位值的封面快照按当前商品图回填。"""
    op.execute(
        f"""
        UPDATE {table} t
        SET cover_image_snap = COALESCE(p.img, '')
        FROM ({_ALIVE_IMAGE}) p
        WHERE p.sku_id = t.sku_id AND {_is_placeholder("t.cover_image_snap")}
        """
    )
    # 商品已软删 / SKU 已不存在：没有可回填的图，清空让展示层兜底。
    # 不清的话这些行会继续带着一个必然 404 的路径。
    op.execute(
        f"""
        UPDATE {table} t
        SET cover_image_snap = ''
        WHERE {_is_placeholder("t.cover_image_snap")}
          AND NOT EXISTS ({_ALIVE_SKU_EXISTS})
        """
    )


def upgrade() -> None:
    op.execute("UPDATE product.spu SET main_image = '' WHERE main_image LIKE 'data:%'")
    op.execute("UPDATE product.sku SET cover_image = '' WHERE cover_image LIKE 'data:%'")

    _repair_snapshots("trade.order_item")
    _repair_snapshots("aftersale.refund_item")


def downgrade() -> None:
    """不可逆。

    清掉的是一段灰块 data URI，回填的是当时的真实商品图 —— 两个原值都没有保留
    的必要，也没有地方保留它们。
    """
