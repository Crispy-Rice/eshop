"""cart 模块的领域逻辑。

本模块对外的唯一入口，其他模块只允许 import 这个文件里的函数
（docs/01 §2）。

**列表接口是唯一的难点**：docs/02 §4 专门写了一节讲它的 N+1 问题。
50 个 SKU 绝不能循环查商品和库存，这里严格按文档的四步做：

1. 批量查 ``cart_item``（1 次）
2. 收集 skuIds → ``product.batch_get_skus``（1 次）
3. 收集 skuIds → ``inventory.batch_available``（1 次）
4. 收集 shopIds → ``account.list_shop_names``（1 次）

**状态不入库**：有效/无货/下架/失效每次实时算出来。入库就要维护失效化，
而那正是我们决定不做的事（失效项由用户自己清）。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.modules.account import service as account_service
from app.modules.cart import repository as repo
from app.modules.cart.models import MAX_SKUS_PER_CART, CartItem
from app.modules.cart.schemas import (
    CART_INVALID,
    CART_NO_STOCK,
    CART_OFF_SHELF,
    CART_STATUS_TEXT,
    CART_VALID,
    CartAddRequest,
    CartItemOut,
    CartOut,
    CartShopGroupOut,
)
from app.modules.inventory import service as inventory_service
from app.modules.product import service as product_service
from app.modules.product.models import SPU_ON_SHELF
from app.modules.product.schemas import SkuBriefOut

# SKU 自身的启用状态（product 模块的约定）
SKU_ENABLED = 1


def _resolve_status(sku: SkuBriefOut | None, available: int) -> int:
    """实时判定购物车项状态。

    顺序有讲究：**先判存在性，再判上下架，最后看库存**。
    一个已删除的商品谈不上"无货"，状态该是"失效"。
    """
    if sku is None:
        return CART_INVALID
    if sku.spu_status != SPU_ON_SHELF or sku.status != SKU_ENABLED:
        return CART_OFF_SHELF
    if available <= 0:
        return CART_NO_STOCK
    return CART_VALID


def _to_item_out(row: CartItem, sku: SkuBriefOut | None, available: int) -> CartItemOut:
    status = _resolve_status(sku, available)
    # SKU 已删除时没有当前价可查，退回快照价——否则整行金额会变成 0，
    # 让用户以为这东西不要钱
    price = sku.price if sku is not None else row.price_snapshot
    return CartItemOut(
        sku_id=row.sku_id,
        spu_id=row.spu_id,
        shop_id=row.shop_id,
        title=sku.title if sku else "（商品已下架）",
        spec_text=sku.spec_text if sku else "",
        cover_image=sku.cover_image if sku else "",
        sku_code=sku.sku_code if sku else "",
        num=row.num,
        selected=row.selected,
        price=price,
        price_snapshot=row.price_snapshot,
        # 只在降价时给正数：涨价不提示（用户不需要被提醒自己买亏了，
        # 当前价已经如实展示结算也不会用快照价）
        price_down=max(0, row.price_snapshot - price),
        item_amount=price * row.num,
        status=status,
        status_text=CART_STATUS_TEXT[status],
        available=available,
    )


# ============================================================
# 加购
# ============================================================
async def add_item(session: AsyncSession, user_id: int, req: CartAddRequest) -> None:
    """加购。同一 SKU 累加数量。

    **刻意不幂等**（docs/15 §2.2）：加购的语义就是累加，
    重复提交应该变成 2 件而不是被幂等拦掉。
    """
    skus = await product_service.batch_get_skus(session, [req.sku_id])
    if not skus:
        # batch_get_skus 默认只返回在售商品，所以这里同时覆盖了"不存在"和"已下架"
        raise BizError(ErrorCode.CART_ITEM_INVALID, "该商品已下架或不存在")
    sku = skus[0]

    # 整车上限：只在"新增一种 SKU"时校验，累加已存在的 SKU 不受影响
    is_new_sku = await repo.get_item(session, user_id, req.sku_id) is None
    if is_new_sku and await repo.count_items(session, user_id) >= MAX_SKUS_PER_CART:
        raise BizError(ErrorCode.CART_FULL)

    await repo.upsert_item(
        session,
        user_id=user_id,
        shop_id=sku.shop_id,
        sku_id=sku.id,
        spu_id=sku.spu_id,
        price=sku.price,
        num=req.num,
        source=req.source,
    )


# ============================================================
# 列表
# ============================================================
async def list_cart(session: AsyncSession, user_id: int) -> CartOut:
    """购物车列表，按店铺分组。

    四次批量查询搞定，不做任何循环查库（docs/02 §4）。
    """
    rows = await repo.list_items(session, user_id)
    if not rows:
        return CartOut(
            groups=[], invalid_items=[], total_count=0, total_amount=0, sku_count=0
        )

    sku_ids = [r.sku_id for r in rows]
    # only_on_shelf=False：已下架的商品也要返回信息，否则前端只能显示"未知商品"，
    # 用户根本认不出是哪件东西
    skus = {
        s.id: s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }
    available = await inventory_service.batch_available(session, sku_ids)
    shop_names = await account_service.list_shop_names(session, {r.shop_id for r in rows})

    items = [
        _to_item_out(row, skus.get(row.sku_id), available.get(row.sku_id, 0)) for row in rows
    ]

    # ★ 金额只算**有效且已勾选**的项。无货/下架/失效的商品就摆在眼前却算进合计，
    #   会让用户以为能买——那是最糟糕的一种"数字对不上"。
    #
    # "失效"和"已下架"都进 invalid_items：两者都是买了也没用的，
    # 放在主列表里只会让用户误点。**"无货"留在原分组**——它是正常商品，
    # 补货后还能买，而且用户需要看到它在哪个店铺。
    groups: dict[int, list[CartItemOut]] = {}
    invalid: list[CartItemOut] = []
    for item in items:
        if item.status in (CART_INVALID, CART_OFF_SHELF):
            invalid.append(item)
        else:
            groups.setdefault(item.shop_id, []).append(item)

    group_outs: list[CartShopGroupOut] = []
    total_count = 0
    total_amount = 0
    for shop_id, group_items in groups.items():
        payable = [i for i in group_items if i.status == CART_VALID and i.selected]
        count = sum(i.num for i in payable)
        amount = sum(i.item_amount for i in payable)
        total_count += count
        total_amount += amount
        group_outs.append(
            CartShopGroupOut(
                shop_id=shop_id,
                shop_name=shop_names.get(shop_id, "未知店铺"),
                items=group_items,
                selected_count=count,
                selected_amount=amount,
            )
        )

    return CartOut(
        groups=group_outs,
        invalid_items=invalid,
        total_count=total_count,
        total_amount=total_amount,
        sku_count=len(rows),
    )


async def count_skus(session: AsyncSession, user_id: int) -> int:
    """购物车里有几种 SKU。顶栏角标用。"""
    return await repo.count_items(session, user_id)


# ============================================================
# 改数量 / 勾选 / 删除
# ============================================================
async def update_num(session: AsyncSession, user_id: int, sku_id: int, num: int) -> None:
    """把数量**设为**指定值（SET 语义）。重复调用结果相同，天然幂等。"""
    if not await repo.update_num(session, user_id, sku_id, num):
        raise BizError(ErrorCode.CART_ITEM_NOT_FOUND)


async def delete_items(session: AsyncSession, user_id: int, sku_ids: Sequence[int]) -> int:
    """批量删除。返回删掉的条数（已不存在的不算错，删除天然幂等）。"""
    return await repo.delete_items(session, user_id, list(sku_ids))


async def set_selected(
    session: AsyncSession, user_id: int, sku_ids: Sequence[int], selected: bool
) -> int:
    """批量勾选/取消。``sku_ids`` 为空表示全选/全不选。"""
    return await repo.set_selected(session, user_id, list(sku_ids), selected)


async def clear_invalid(session: AsyncSession, user_id: int) -> int:
    """清除失效商品。

    哪些算失效由**实时状态**决定，所以先算一遍再删——不能只看"商品查不到"，
    已下架的商品同样是买了也没用的。
    """
    rows = await repo.list_items(session, user_id)
    if not rows:
        return 0
    sku_ids = [r.sku_id for r in rows]
    skus = {
        s.id: s
        for s in await product_service.batch_get_skus(session, sku_ids, only_on_shelf=False)
    }
    available = await inventory_service.batch_available(session, sku_ids)

    dead = [
        r.sku_id
        for r in rows
        if _resolve_status(skus.get(r.sku_id), available.get(r.sku_id, 0))
        in (CART_INVALID, CART_OFF_SHELF)
    ]
    return await repo.delete_items(session, user_id, dead)
