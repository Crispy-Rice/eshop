"""product 模块的领域逻辑。

对本模块外只暴露这里定义的函数（docs/01-overview.md §2）。
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.core.logging import get_logger
from app.core.snowflake import next_id
from app.modules.product import repository as repo
from app.modules.product.models import (
    SPU_DRAFT,
    SPU_OFF_SHELF,
    SPU_ON_SHELF,
    SPU_PENDING_AUDIT,
    Category,
    Sku,
    SkuSpec,
    SpecGroup,
    SpecValue,
    Spu,
)
from app.modules.product.schemas import (
    CategoryCreateRequest,
    CategoryOut,
    CategoryTreeOut,
    SkuBriefOut,
    SkuDetailOut,
    SkuUpdateRequest,
    SpecGroupIn,
    SpecGroupOut,
    SpecValueOut,
    SpuAuditRequest,
    SpuCardOut,
    SpuCreateRequest,
    SpuDetailOut,
    SpuListOut,
    SpuUpdateRequest,
)

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 60

# 排序方式 → (排序列, 是否降序)
SORT_SPECS: dict[str, tuple[Any, bool]] = {
    "relevance": (Spu.total_sold, True),  # 一期没有相关度模型，退回销量排序
    "sales": (Spu.total_sold, True),
    "newest": (Spu.created_at, True),
    "price_asc": (Spu.price_min, False),
    "price_desc": (Spu.price_max, True),
}


# ============================================================
# 游标分页
# ============================================================
def _encode_cursor(sort: str, value: Any, row_id: int) -> str:
    raw = f"{sort}|{value}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, expected_sort: str) -> tuple[Any, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        sort, raw_value, raw_id = base64.urlsafe_b64decode(padded).decode().split("|")
        if sort != expected_sort:
            # 换了排序方式，旧游标作废
            raise ValueError("sort mismatch")
        value: Any = datetime.fromisoformat(raw_value) if sort == "newest" else int(raw_value)
        return value, int(raw_id)
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新搜索") from exc


# ============================================================
# 类目
# ============================================================
async def create_category(
    session: AsyncSession, req: CategoryCreateRequest, *, is_admin: bool
) -> CategoryOut:
    if not is_admin:
        raise BizError(ErrorCode.FORBIDDEN, "只有平台可以维护类目")

    parent: Category | None = None
    level = 1
    path_prefix = "/"
    if req.parent_id is not None:
        parent = await repo.get_category(session, req.parent_id)
        if parent is None:
            raise BizError(ErrorCode.NOT_FOUND, "上级类目不存在")
        if parent.level >= 3:
            raise BizError(ErrorCode.VALIDATION_ERROR, "类目最多三级")
        level = parent.level + 1
        path_prefix = parent.path

    if await repo.category_name_exists(session, req.parent_id, req.name):
        raise BizError(ErrorCode.VALIDATION_ERROR, "同级下已存在同名类目")

    category_id = next_id()
    category = Category(
        id=category_id,
        parent_id=req.parent_id,
        name=req.name,
        level=level,
        path=f"{path_prefix}{category_id}/",
        sort=req.sort,
        status=1,
    )
    await repo.insert_category(session, category)
    return CategoryOut(
        id=category.id,
        parent_id=category.parent_id,
        name=category.name,
        level=category.level,
        sort=category.sort,
    )


async def list_category_tree(session: AsyncSession) -> list[CategoryTreeOut]:
    """返回完整类目树。类目总量很小（万级），一次全取再在内存里组树最省事。"""
    categories = await repo.list_categories(session)

    nodes: dict[int, CategoryTreeOut] = {
        c.id: CategoryTreeOut(
            id=c.id, parent_id=c.parent_id, name=c.name, level=c.level, sort=c.sort, children=[]
        )
        for c in categories
    }
    roots: list[CategoryTreeOut] = []
    for c in categories:
        node = nodes[c.id]
        if c.parent_id is not None and c.parent_id in nodes:
            nodes[c.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


async def _resolve_category(session: AsyncSession, category_id: int) -> Category:
    """商品必须挂在**末级**类目下，否则属性模板无从谈起（docs/02 §8）。"""
    category = await repo.get_category(session, category_id)
    if category is None or category.status != 1:
        raise BizError(ErrorCode.VALIDATION_ERROR, "类目不存在或已停用")
    if await repo.count_children(session, category_id) > 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "商品必须挂在末级类目下")
    return category


async def _build_search_text(
    session: AsyncSession, *, title: str, sub_title: str | None, category: Category, spec_texts: list[str]
) -> str:
    """拼出用于 pg_trgm 匹配的搜索文本。

    含：标题 + 副标题 + 类目路径名 + 所有规格值。
    这样搜"黑色 256G"能命中，搜"手机"也能靠类目名命中。
    """
    parts: list[str] = [title]
    if sub_title:
        parts.append(sub_title)

    path_ids = [int(x) for x in category.path.strip("/").split("/") if x]
    if path_ids:
        cats = await repo.list_categories_by_ids(session, path_ids)
        name_by_id = {c.id: c.name for c in cats}
        parts.extend(name_by_id[i] for i in path_ids if i in name_by_id)

    parts.extend(spec_texts)
    return " ".join(p for p in parts if p)[:2000]


# ============================================================
# 商家：发布商品
# ============================================================
async def create_spu(session: AsyncSession, shop_id: int, req: SpuCreateRequest) -> SpuDetailOut:
    category = await _resolve_category(session, req.category_id)

    spu_id = next_id()

    # ---------- 规格组与规格值 ----------
    # 先把 key → 生成好的 ID 映射建起来，SKU 用它引用规格值
    key_to_value_id: dict[str, int] = {}
    key_to_group_id: dict[str, int] = {}
    group_rows: list[SpecGroup] = []
    value_rows: list[SpecValue] = []
    value_text_by_key: dict[str, str] = {}

    for group_index, group_in in enumerate(req.spec_groups):
        group_id = next_id()
        group_rows.append(SpecGroup(id=group_id, spu_id=spu_id, name=group_in.name, sort=group_index))
        for value_index, value_in in enumerate(group_in.values):
            value_id = next_id()
            key_to_value_id[value_in.key] = value_id
            key_to_group_id[value_in.key] = group_id
            value_text_by_key[value_in.key] = value_in.value
            value_rows.append(
                SpecValue(
                    id=value_id,
                    group_id=group_id,
                    value=value_in.value,
                    image=value_in.image,
                    sort=value_index,
                )
            )

    # ---------- SKU ----------
    sku_rows: list[Sku] = []
    sku_spec_rows: list[SkuSpec] = []
    spec_texts: list[str] = []

    for sku_in in req.skus:
        sku_id = next_id()
        # spec_text 按规格组的顺序拼，保证同一个 SPU 下的 SKU 格式一致
        ordered_keys = [
            next(k for k in sku_in.spec_value_keys if key_to_group_id[k] == g.id) for g in group_rows
        ]
        spec_text = ";".join(value_text_by_key[k] for k in ordered_keys)

        sku_rows.append(
            Sku(
                id=sku_id,
                spu_id=spu_id,
                shop_id=shop_id,
                sku_code=sku_in.sku_code,
                spec_text=spec_text,
                price=sku_in.price,
                cover_image=sku_in.cover_image,
                weight_g=sku_in.weight_g,
                status=1,
            )
        )
        for key in ordered_keys:
            sku_spec_rows.append(
                SkuSpec(sku_id=sku_id, spec_group_id=key_to_group_id[key], spec_value_id=key_to_value_id[key])
            )
        spec_texts.extend(value_text_by_key[k] for k in ordered_keys)

    prices = [s.price for s in sku_rows]

    # ---------- SPU ----------
    search_text = await _build_search_text(
        session, title=req.title, sub_title=req.sub_title, category=category, spec_texts=spec_texts
    )
    spu = Spu(
        id=spu_id,
        shop_id=shop_id,
        category_id=req.category_id,
        title=req.title,
        sub_title=req.sub_title,
        main_image=req.main_image,
        price_min=min(prices),
        price_max=max(prices),
        status=SPU_DRAFT,
        search_text=search_text,
    )

    # 全部在同一个事务里写入：SPU、规格、SKU 要么都成功要么都不落库
    await repo.insert_spu(session, spu)
    await repo.insert_spec_groups(session, group_rows)
    await repo.insert_spec_values(session, value_rows)
    await repo.insert_skus(session, sku_rows)
    await repo.insert_sku_specs(session, sku_spec_rows)

    logger.info("商品创建成功", extra={"spuId": spu_id, "shopId": shop_id, "skuCount": len(sku_rows)})
    return await get_spu_detail(session, spu_id, owner_shop_id=shop_id)


async def update_spu(session: AsyncSession, shop_id: int, spu_id: int, req: SpuUpdateRequest) -> SpuDetailOut:
    # 归属校验：不是自己的商品直接 404
    await _get_owned_spu(session, shop_id, spu_id)

    values: dict[str, Any] = {}
    if req.title is not None:
        values["title"] = req.title
    if req.sub_title is not None:
        values["sub_title"] = req.sub_title
    if req.main_image is not None:
        values["main_image"] = req.main_image
    if req.sort_weight is not None:
        values["sort_weight"] = req.sort_weight
    if req.category_id is not None:
        await _resolve_category(session, req.category_id)  # 校验：必须存在且是末级
        values["category_id"] = req.category_id

    if values:
        await repo.update_spu_fields(session, spu_id, values)

        # 标题/类目变了，搜索文本要跟着重建
        if {"title", "sub_title", "category_id"} & values.keys():
            updated = await repo.get_spu(session, spu_id)
            assert updated is not None
            category = await repo.get_category(session, updated.category_id)
            assert category is not None
            skus = await repo.list_skus_by_spu(session, spu_id)
            spec_texts: list[str] = []
            for sku in skus:
                spec_texts.extend(sku.spec_text.split(";"))
            search_text = await _build_search_text(
                session,
                title=updated.title,
                sub_title=updated.sub_title,
                category=category,
                spec_texts=spec_texts,
            )
            await repo.update_search_text(session, spu_id, search_text)

    return await get_spu_detail(session, spu_id, owner_shop_id=shop_id)


async def update_sku(session: AsyncSession, shop_id: int, sku_id: int, req: SkuUpdateRequest) -> None:
    """改单个 SKU。价格变化不影响已下单订单（订单读的是快照）。"""
    sku = await repo.get_sku(session, sku_id)
    if sku is None or sku.shop_id != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")

    values: dict[str, Any] = {}
    if req.price is not None:
        values["price"] = req.price
    if req.cover_image is not None:
        values["cover_image"] = req.cover_image
    if req.weight_g is not None:
        values["weight_g"] = req.weight_g
    if req.status is not None:
        values["status"] = req.status

    if not values:
        return

    await repo.update_sku_fields(session, sku_id, values)

    # 改价或上下架都会影响展示价区间
    if {"price", "status"} & values.keys():
        price_min, price_max = await repo.price_range_of_spu(session, sku.spu_id)
        if price_min > 0:
            await repo.update_spu_fields(
                session, sku.spu_id, {"price_min": price_min, "price_max": price_max}
            )


async def submit_for_audit(session: AsyncSession, shop_id: int, spu_id: int) -> None:
    spu = await _get_owned_spu(session, shop_id, spu_id)
    if spu.status != SPU_DRAFT:
        raise BizError(ErrorCode.VALIDATION_ERROR, "只有草稿状态的商品可以提交审核")
    await repo.update_spu_fields(session, spu_id, {"status": SPU_PENDING_AUDIT})


async def audit_spu(session: AsyncSession, spu_id: int, req: SpuAuditRequest) -> None:
    """平台审核。通过 → 上架；不通过 → 退回草稿，商家修改后可再次提交。"""
    spu = await repo.get_spu(session, spu_id)
    if spu is None:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")
    if spu.status != SPU_PENDING_AUDIT:
        raise BizError(ErrorCode.VALIDATION_ERROR, "该商品不在待审核状态")

    await repo.update_spu_fields(session, spu_id, {"status": SPU_ON_SHELF if req.approved else SPU_DRAFT})
    logger.info("商品审核完成", extra={"spuId": spu_id, "approved": req.approved})


async def on_shelf(session: AsyncSession, shop_id: int, spu_id: int) -> None:
    spu = await _get_owned_spu(session, shop_id, spu_id)
    if spu.status == SPU_ON_SHELF:
        return
    if spu.status in (SPU_DRAFT, SPU_PENDING_AUDIT):
        raise BizError(ErrorCode.VALIDATION_ERROR, "商品尚未通过审核")
    await repo.update_spu_fields(session, spu_id, {"status": SPU_ON_SHELF})
    await repo.update_sku_status_by_spu(session, spu_id, 1)


async def off_shelf(session: AsyncSession, shop_id: int, spu_id: int) -> None:
    """下架。已下单的订单不受影响 —— 订单读的是商品快照。"""
    spu = await _get_owned_spu(session, shop_id, spu_id)
    if spu.status == SPU_OFF_SHELF:
        return
    await repo.update_spu_fields(session, spu_id, {"status": SPU_OFF_SHELF})
    await repo.update_sku_status_by_spu(session, spu_id, 2)


async def _get_owned_spu(session: AsyncSession, shop_id: int, spu_id: int) -> Spu:
    """取自己店铺的商品。不是自己的返回 404，避免被用来探测别家商品是否存在。"""
    spu = await repo.get_spu(session, spu_id)
    if spu is None or spu.shop_id != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")
    return spu


# ============================================================
# 买家：浏览与搜索
# ============================================================
def _to_card(spu: Spu) -> SpuCardOut:
    return SpuCardOut(
        id=spu.id,
        shop_id=spu.shop_id,
        category_id=spu.category_id,
        title=spu.title,
        main_image=spu.main_image,
        price_min=spu.price_min,
        price_max=spu.price_max,
        total_sold=spu.total_sold,
        avg_score=float(spu.avg_score),
        review_count=spu.review_count,
        status=spu.status,
    )


async def search_products(
    session: AsyncSession,
    *,
    keyword: str | None = None,
    category_id: int | None = None,
    price_from: int | None = None,
    price_to: int | None = None,
    sort: str = "relevance",
    cursor: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    shop_id: int | None = None,
    status: int | None = None,
    on_shelf_only: bool = True,
) -> SpuListOut:
    """商品搜索 / 浏览 / 商家自己的商品列表，共用这一条路径。"""
    if sort not in SORT_SPECS:
        raise BizError(ErrorCode.VALIDATION_ERROR, f"不支持的排序方式: {sort}")
    limit = max(1, min(limit, MAX_PAGE_SIZE))

    keywords = [w for w in (keyword or "").split() if w]

    category_ids: list[int] | None = None
    if category_id is not None:
        category = await repo.get_category(session, category_id)
        if category is None:
            raise BizError(ErrorCode.NOT_FOUND, "类目不存在")
        # 选中一级类目时要把整棵子树都算上
        category_ids = await repo.list_descendant_category_ids(session, category.path)

    column, descending = SORT_SPECS[sort]
    order_by = [column.desc() if descending else column.asc(), Spu.id.desc() if descending else Spu.id.asc()]

    cursor_clause = None
    if cursor:
        value, row_id = _decode_cursor(cursor, sort)
        cursor_clause = repo.row_value_cursor(column, value, row_id, descending=descending)

    # 多取一条用来判断还有没有下一页，避免额外跑一次 count
    rows = await repo.search_spus(
        session,
        keywords=keywords,
        category_ids=category_ids,
        price_from=price_from,
        price_to=price_to,
        shop_id=shop_id,
        status=status,
        on_shelf_only=on_shelf_only,
        order_by=order_by,
        cursor_clause=cursor_clause,
        limit=limit + 1,
    )

    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        raw_value = last.created_at.isoformat() if sort == "newest" else getattr(last, column.key)
        next_cursor = _encode_cursor(sort, raw_value, last.id)

    return SpuListOut(items=[_to_card(s) for s in rows], has_more=has_more, next_cursor=next_cursor)


async def get_spu_detail(
    session: AsyncSession, spu_id: int, *, owner_shop_id: int | None = None
) -> SpuDetailOut:
    """商品详情。

    访问控制由 ``owner_shop_id`` 决定：
    - 传了 shop_id：商家看自己的商品，**必须是自己的**，状态不限
    - 没传：买家视角，只能看已上架的

    ★ 不要用 "require_on_shelf=False 就跳过校验" 这种写法 —— 那等于
    给任何登录商家开了查看别家未上架商品的权限。

    返回的 specGroups + skus 就是前端规格选择器需要的全部数据：
    每个 SKU 带着它的 specValueIds，前端据此推导"哪些规格值可选"
    （docs/02 §3 的无效组合问题）。
    """
    spu = await repo.get_spu(session, spu_id)
    if spu is None:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")

    if owner_shop_id is not None:
        if spu.shop_id != owner_shop_id:
            # 一律 404：不区分"不存在"和"不是你的"，避免被用来探测商品
            raise BizError(ErrorCode.NOT_FOUND, "商品不存在")
    elif spu.status != SPU_ON_SHELF:
        raise BizError(ErrorCode.NOT_FOUND, "商品不存在")

    groups = await repo.list_spec_groups(session, spu_id)
    group_ids = [g.id for g in groups]
    values = await repo.list_spec_values(session, group_ids)
    skus = await repo.list_skus_by_spu(session, spu_id)
    sku_specs = await repo.list_sku_specs(session, [s.id for s in skus])

    value_ids_by_sku: dict[int, list[int]] = {}
    for row in sku_specs:
        value_ids_by_sku.setdefault(row.sku_id, []).append(row.spec_value_id)

    values_by_group: dict[int, list[SpecValueOut]] = {}
    for v in values:
        values_by_group.setdefault(v.group_id, []).append(SpecValueOut(id=v.id, value=v.value, image=v.image))

    return SpuDetailOut(
        id=spu.id,
        shop_id=spu.shop_id,
        category_id=spu.category_id,
        title=spu.title,
        sub_title=spu.sub_title,
        main_image=spu.main_image,
        price_min=spu.price_min,
        price_max=spu.price_max,
        total_sold=spu.total_sold,
        status=spu.status,
        spec_groups=[
            SpecGroupOut(id=g.id, name=g.name, values=values_by_group.get(g.id, [])) for g in groups
        ],
        skus=[
            SkuDetailOut(
                id=s.id,
                sku_code=s.sku_code,
                spec_text=s.spec_text,
                price=s.price,
                cover_image=s.cover_image,
                weight_g=s.weight_g,
                status=s.status,
                spec_value_ids=value_ids_by_sku.get(s.id, []),
            )
            for s in skus
        ],
    )


async def batch_get_skus(session: AsyncSession, sku_ids: list[int]) -> list[SkuBriefOut]:
    """购物车/结算页批量取 SKU。**不返回已下架的商品**，由调用方提示用户失效。"""
    skus = await repo.list_skus_by_ids(session, sku_ids)
    if not skus:
        return []

    spus = {s.id: s for s in await repo.list_spus_by_ids(session, [s.spu_id for s in skus])}

    result: list[SkuBriefOut] = []
    for sku in skus:
        spu = spus.get(sku.spu_id)
        # SPU 已删除、或已下架 → 该 SKU 视为失效，不返回
        if spu is None or spu.status != SPU_ON_SHELF or sku.status != 1:
            continue
        result.append(
            SkuBriefOut(
                id=sku.id,
                spu_id=sku.spu_id,
                shop_id=sku.shop_id,
                title=spu.title,
                spec_text=sku.spec_text,
                price=sku.price,
                cover_image=sku.cover_image,
                weight_g=sku.weight_g,
                status=sku.status,
            )
        )
    return result


# ============================================================
# 供其他模块调用
# ============================================================
async def get_sku_for_order(session: AsyncSession, sku_id: int) -> tuple[Sku, Spu]:
    """下单时取 SKU 与它的 SPU，用于生成订单项快照。

    返回 ORM 对象供 trade 模块读取；**不要**直接把它序列化给前端。
    """
    sku = await repo.get_sku(session, sku_id)
    if sku is None:
        raise BizError(ErrorCode.SKU_OFF_SHELF, "商品不存在")
    spu = await repo.get_spu(session, sku.spu_id)
    if spu is None or spu.status != SPU_ON_SHELF or sku.status != 1:
        raise BizError(ErrorCode.SKU_OFF_SHELF, f"商品「{spu.title if spu else sku.sku_code}」已下架")
    return sku, spu


async def list_spec_group_inputs(session: AsyncSession, spu_id: int) -> list[SpecGroupIn]:
    """把库里存的规格还原成输入结构，给"复制商品"之类的场景用。"""
    groups = await repo.list_spec_groups(session, spu_id)
    values = await repo.list_spec_values(session, [g.id for g in groups])
    by_group: dict[int, list[Any]] = {}
    for v in values:
        by_group.setdefault(v.group_id, []).append(v)

    return [
        SpecGroupIn(
            name=g.name,
            values=[{"key": str(v.id), "value": v.value, "image": v.image} for v in by_group.get(g.id, [])],
        )
        for g in groups
    ]
