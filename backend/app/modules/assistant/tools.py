"""工具注册表 —— 助手能做的**全部**事情，也是**权限面本身**（docs/20 §4）。

三条硬规则，按重要性排：

1. **身份只能来自服务端。** 每个 handler 用的 ``shop_id`` 一律取自
   ``ctx.caller``（提交时从数据库解析、冻结在消息行上的快照），
   **工具的参数 schema 里根本没有 ``shop_id``** —— 模型想指定也无处可指定，
   幻觉出来的额外键会被 ``rules.parse_tool_args`` 丢掉。
2. **按角色决定"注册哪些工具"。** 商家与运营看到的是**两份不同的工具清单**，
   模型看不见它无权调用的工具。这比"看见了但被拒"更安全：越权不是被拦下的，
   而是**不可能被提出**。
3. **写操作一个都没有。** 即使注入成功，后果上限是"答错话"而不是"改数据"。
   唯一有副作用的动作是"转人工"，它由前端按钮触发、走普通 HTTP 端点，
   **不由模型调用**（模型没有这个工具）。

⛔ **禁止清单（不要往里加）**：

- ``trade.service.get_order_detail(order_main_no)`` —— 裸查询，**完全不做归属校验**，
  做成工具等于开放全站订单读取。
- ``trade.service.get_shop_order`` / ``list_shop_orders`` —— **商家视角**，返回的是
  **全店所有买家的**订单。买家侧的 AI 用它 = 把别人的订单交出去。
- ``trade.repo.get_main_by_no`` / ``aftersale.repo.get_by_no`` / ``product.repo.get_spu``
  —— 裸查询（不带归属条件），一律不碰。
- ``product.service.get_sku_for_order`` —— 返回 ORM 行，注释里自己写着"不要序列化给前端"。
- ``inventory.service.batch_available``（**真实库存**）—— 买家侧只给 ``sku_display``
  的档位文案；把真实件数喂给模型等于喂给爬虫。
- ``inventory.service.list_stock_out`` 的默认参数（``sync_missing=True``）——
  它是个**会写库的"读"函数**（顺手给缺行补 0 库存）。真要查库存明细必须显式
  传 ``sync_missing=False``。

★ 表述铁律：**金额字段一律以 ``_cent`` 结尾**（单位：分）。模型看到键名就知道单位，
  不必靠提示词反复叮嘱（那句叮嘱在 ``rules`` 的 system prompt 里有一句）。

★ **买家侧的结果一律过白名单投影**（``project_order`` / ``project_refund`` …），
  **绝不用 ``model_dump()`` 兜出去**：订单详情里有收货人姓名、手机号、详细地址，
  而商家侧的 UI 刻意只显示**打码**手机号 —— 让模型把完整号码背进会话
  （商家看得到那条会话）等于绕过那个设计。新增字段默认**不进**上下文。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.modules.account import service as account_service
from app.modules.aftersale import service as aftersale_service
from app.modules.assistant import rules
from app.modules.freight import service as freight_service
from app.modules.inventory import service as inventory_service
from app.modules.product import service as product_service
from app.modules.product.models import SPU_STATUS_TEXT
from app.modules.support import service as support_service
from app.modules.trade import service as trade_service

# 工具作用域 = 谁能用。商家锁死自己的店；运营可以跨店（那是它的合法能力）
SCOPE_MERCHANT = "merchant"
SCOPE_ADMIN = "admin"
# ★ 第三个作用域：**买家**。店小蜜以「这家店」的身份回答「这个买家」——
#   它的工具面是"本店目录 × 本人订单"的**交集**，比商家面窄得多：
#   商家能看全店订单，买家只能看自己的那一单（见 docs/20 §14）。
SCOPE_BUYER = "buyer"
ALL_SCOPES = frozenset({SCOPE_MERCHANT, SCOPE_ADMIN, SCOPE_BUYER})

# 工具返回的行数上限。★ 助手要的是"看一眼"，不是导出 —— 20 行足够说明问题，
# 而把 200 行塞进上下文既贵又会让模型忽略真正的重点
MAX_TOOL_ROWS = 20


@dataclass(frozen=True, slots=True)
class ToolContext:
    session: AsyncSession
    caller: rules.Caller
    # 店小蜜专用：这条会话的上下文（**服务端注入**，模型看不到也改不了）。
    # ``None`` = 后台助手那条路径。
    ticket: rules.ShopbotTicket | None = None

    def require_ticket(self) -> rules.ShopbotTicket:
        """买家侧工具的作用域。没有会话上下文就是编程错误 —— 直接炸，
        不要退化成"没有约束"（那正是越权的来源）。"""
        if self.ticket is None:
            raise BizError(ErrorCode.FORBIDDEN, "这个工具只在买家会话里可用")
        return self.ticket


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    roles: frozenset[str]
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolDenied(BizError):
    """模型调了一个它这个身份没有的工具。

    正常情况下到不了这里（``tools_for`` 根本不会把它交出去），留作**纵深防御**：
    万一模型被注入后硬编一个运营工具名，也必须 fail-closed 而不是放行。
    """

    def __init__(self, name: str) -> None:
        super().__init__(ErrorCode.FORBIDDEN, f"当前身份不能使用工具 {name}")


# ==================================================================
# 共用小工具
# ==================================================================
def _limit(args: Mapping[str, Any]) -> int:
    """把模型给的 limit 夹到 [1, MAX_TOOL_ROWS]。"""
    raw = args.get("limit", 10)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(value, MAX_TOOL_ROWS))


def _dt(value: Any) -> str | None:
    """时间统一成 ``YYYY-MM-DD HH:MM`` 的字符串。

    给模型看的东西不需要毫秒和时区后缀，短一点更省钱也更好读。
    """
    return value.strftime("%Y-%m-%d %H:%M") if value else None


# ==================================================================
# 商家工具
# ==================================================================
async def _get_shop_info(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop = await account_service.get_my_shop(ctx.session, ctx.caller.user_id)
    return {
        "shop_id": str(shop.id),
        "name": shop.name,
        "status": shop.status,
        "status_text": account_service.shop_status_text(shop.status),
        "description": shop.description,
    }


async def _shop_overview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """店铺概览：问"我店铺现在什么情况"时唯一该调的工具。

    ★ 每个数字都由**拥有该概念的模块**给出，这里只做编排 ——
      例如"待发货"是 trade 的业务概念，它自己知道那是哪个状态码，
      助手不去猜一个整数（那正是跨模块铁律要防的事）。

    ★ ``products`` 是按**状态**分的商品数（``on_shelf`` 才是买家看得到的）。
      这一项是补出来的：原来只有 ``list_products``（一页 20 条、不带状态），
      于是"我有多少商品上架了"只能答"工具没给总数"（用户报过这个）。
      计数走 product 自己那条过滤条件，和「商品管理」页的数字同源。
    """
    shop_id = ctx.caller.require_shop()
    return {
        "pending_ship_orders": await trade_service.count_shop_pending_ship(ctx.session, shop_id),
        "pending_aftersales": await aftersale_service.count_pending_for_shop(ctx.session, shop_id),
        "pending_tickets": (
            await support_service.pending_count(ctx.session, shop_id=shop_id)
        ).count,
        "out_of_stock_rows": await inventory_service.count_out_of_stock(
            ctx.session, shop_id=shop_id
        ),
        "products": await product_service.count_spu_by_status_for_shop(ctx.session, shop_id),
        "note": (
            "out_of_stock_rows 数的是库存行（一个规格在一个仓），不是商品数；"
            "products 里的 on_shelf 才等于买家在商城里能看到的商品数"
        ),
    }


async def _list_orders(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop_id = ctx.caller.require_shop()
    out = await trade_service.list_shop_orders(
        ctx.session, shop_id, limit=_limit(args)
    )
    return {
        "orders": [
            {
                "order_sub_no": o.order_sub_no,
                "status_text": o.status_text,
                "delivery_status_text": o.delivery_status_text,
                "payable_amount_cent": o.payable_amount,
                "item_kind_count": o.item_kind_count,
                "total_num": o.total_num,
                "titles": o.preview_titles[:3],
                "warehouse_name": o.warehouse_name,
                "create_time": _dt(o.create_time),
            }
            for o in out.items
        ],
        "has_more": out.has_more,
    }


async def _get_order(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """订单详情。归属校验在 ``trade.get_shop_order`` 内部（不是本店的单直接 404）。"""
    shop_id = ctx.caller.require_shop()
    o = await trade_service.get_shop_order(
        ctx.session, shop_id=shop_id, order_sub_no=str(args.get("order_sub_no") or "")
    )
    return {
        "order_sub_no": o.order_sub_no,
        "order_main_no": o.order_main_no,
        "status_text": o.status_text,
        "delivery_status_text": o.delivery_status_text,
        "warehouse_name": o.warehouse_name,
        "total_amount_cent": o.total_amount,
        "discount_amount_cent": o.discount_amount,
        "freight_amount_cent": o.freight_amount,
        "payable_amount_cent": o.payable_amount,
        "item_kind_count": o.item_kind_count,
        "total_num": o.total_num,
        "titles": o.preview_titles,
        "receiver_full": o.receiver_full,
        "create_time": _dt(o.create_time),
        "pay_time": _dt(o.pay_time),
        "deliver_time": _dt(o.deliver_time),
        "can_ship": o.can_ship,
    }


async def _list_aftersales(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop_id = ctx.caller.require_shop()
    out = await aftersale_service.list_shop_refunds(
        ctx.session,
        shop_id,
        pending_only=bool(args.get("pending_only", False)),
        limit=_limit(args),
    )
    return {
        "refunds": [
            {
                "refund_no": r.refund_no,
                "order_sub_no": r.order_sub_no,
                "refund_type_text": r.refund_type_text,
                "status_text": r.status_text,
                "total_refund_cent": r.total_refund,
                "preview_title": r.preview_title,
                "item_count": r.item_count,
                "apply_time": _dt(r.apply_time),
                "deadline": _dt(r.deadline),
            }
            for r in out.items
        ],
        "has_more": out.has_more,
    }


async def _get_aftersale(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop_id = ctx.caller.require_shop()
    r = await aftersale_service.get_detail_for_shop(
        ctx.session, shop_id=shop_id, refund_no=str(args.get("refund_no") or "")
    )
    return {
        "refund_no": r.refund_no,
        "order_sub_no": r.order_sub_no,
        "refund_type_text": r.refund_type_text,
        "reason_type_text": r.reason_type_text,
        "status_text": r.status_text,
        "refund_amount_cent": r.refund_amount,
        "refund_freight_cent": r.refund_freight,
        "total_refund_cent": r.total_refund,
        "freight_bearer_text": r.freight_bearer_text,
        "merchant_remark": r.merchant_remark,
        "reject_reason": r.reject_reason,
        "quality_result_text": r.quality_result_text,
        "apply_time": _dt(r.apply_time),
        "deadline": _dt(r.deadline),
        "items": [
            {
                "title": i.title,
                "spec_text": i.spec_text,
                "refund_num": i.refund_num,
                "refund_amount_cent": i.refund_amount,
            }
            for i in r.items
        ],
    }


async def _list_warehouses(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop_id = ctx.caller.require_shop()
    warehouses = await inventory_service.list_warehouses(ctx.session, shop_id)
    return {
        "warehouses": [
            {
                "name": w.name,
                "is_default": w.is_default,
                "status": w.status,
                "address": " ".join(
                    p for p in (w.province, w.city, w.district, w.detail) if p
                ),
                "region_count": len(w.rules),
            }
            for w in warehouses
        ],
        "note": "is_default 的仓是兜底仓，不能停用。规则仓没货时会自动按"
        "「规则仓 → 默认仓 → 其余启用仓」的顺序换仓发货",
    }


async def _check_freight_config(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """运费配置体检：**哪些规格一旦卖出就算不出运费**。

    这是"商品为什么发不了 / 上不了架"最常见的根因，也是 ``freight`` 模块
    已经在用的判据（与结算 ``estimate`` 的报错条件逐条对齐）。
    """
    shop_id = ctx.caller.require_shop()
    sku_ids = await product_service.list_shop_sku_ids(ctx.session, shop_id)
    if not sku_ids:
        return {"missing_count": 0, "missing_titles": [], "note": "本店还没有商品"}
    missing = await freight_service.skus_without_freight(
        ctx.session, shop_id=shop_id, sku_ids=sku_ids
    )
    skus = {
        s.id: s
        for s in await product_service.batch_get_skus(
            ctx.session, missing[:MAX_TOOL_ROWS], only_on_shelf=False
        )
    }
    return {
        "total_skus": len(sku_ids),
        "missing_count": len(missing),
        "missing_titles": [skus[sid].title for sid in missing if sid in skus],
        "note": "这些规格既没绑定运费模板、店铺也没有可回落的默认模板，"
        "买家结算时会直接报「该商品暂时无法配送」",
    }


async def _list_products(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shop_id = ctx.caller.require_shop()
    status = args.get("status")
    # ★ 状态不合法时**明说**，不要静默当成"没筛"：那会答出一份不含筛选的结果，
    #   而模型不知道自己答的是另一件事。回一句可读的话，它可以改对再调一次
    #   （工具报错当成结果回给模型，不让整轮失败 —— 见 service 里的那个 except）
    if status is not None and status not in SPU_STATUS_TEXT:
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            "status 只能是 1 草稿 / 2 已上架 / 3 已下架 / 4 违规下架 / 5 待审核 / 6 已驳回",
        )
    out = await product_service.search_products(
        ctx.session,
        keyword=args.get("keyword") or None,
        shop_id=shop_id,
        status=status,
        # 商家要能看见自己的草稿 / 已下架 / 被驳回的商品，不能只看上架的
        on_shelf_only=False,
        limit=_limit(args),
    )
    return {
        "products": [
            {
                "spu_id": str(p.id),
                "title": p.title,
                # ★ 每行**带上状态**。原来没有它，"这件还在卖吗""哪些是草稿"都答不了 ——
                #   列表里十条商品长得一模一样，模型只能干瞪眼。文案走 product 自己的
                #   那一个函数（与商家在「商品管理」页看到的一致，也与接口里的
                #   ``statusText`` 同一处来源）。
                "status": p.status,
                "status_text": product_service.status_text_of(p.status),
                "price_min_cent": p.price_min,
                "price_max_cent": p.price_max,
                "total_sold": p.total_sold,
            }
            for p in out.items
        ],
        "has_more": out.has_more,
    }


# ==================================================================
# 平台工具（仅运营）
# ==================================================================
async def _platform_overview(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    pending_tickets = await support_service.pending_count(ctx.session, shop_id=None)
    return {
        "pending_platform_tickets": pending_tickets.count,
        "pending_audit_products": await product_service.count_pending_audit(ctx.session),
        "note": "pending_platform_tickets 是发往平台客服的会话数；"
        "pending_audit_products 是待审核商品数",
    }


async def _list_pending_spus(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    out = await product_service.list_pending_audit(ctx.session, limit=_limit(args))
    return {
        "products": [
            {
                "spu_id": str(p.id),
                "shop_id": str(p.shop_id),
                "title": p.title,
                "price_min_cent": p.price_min,
                "price_max_cent": p.price_max,
            }
            for p in out.items
        ],
        "has_more": out.has_more,
    }


# ==================================================================
# 买家工具（店小蜜）—— 一期只有这一个，而且它是**信号不是动作**
# ==================================================================
async def _request_human(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """「我答不了，叫商家本人」—— **模型只能发这个信号，改不了任何状态**。

    ★ 这是全模块唯一一个有副作用的意图，所以刻意做成两段：模型调用它，只是把
      "这一轮要转人工"记进 `bot_turn.tool_calls`；**真正的转人工由服务层在写回复的
      同一个事务里做**（``support_service.flag_need_human``）。
      于是"模型没有任何写状态的能力"这条铁律仍然成立 —— 它最多能把球交给真人，
      而那正是我们要它做的。
    """
    ctx.require_ticket()
    return {
        "ok": True,
        "hint": "已记录转人工请求。现在用**一句话**告诉买家你答不了、已经叫了商家本人，"
        "不要再猜、不要编。",
    }


def was_escalated(audit: Sequence[Mapping[str, Any]]) -> bool:
    """这一轮模型有没有请求转人工（看审计里的工具名）。"""
    return any(record.get("name") == rules.SHOPBOT_ESCALATE_TOOL for record in audit)


# ---------------- 买家工具：数据面（**这家店 × 这个买家**的交集）----------------
# ★ 投影一律**逐字段白名单**，绝不用 ``model_dump()`` 直接兜出去：
#   订单详情里有收货人姓名、手机号、详细地址，而商家侧的 UI 刻意只显示**打码**手机号。
#   把完整号码喂给模型 = 它可能背进会话里（商家看得到那条会话）—— 那等于绕过那个设计。
#   白名单还有个好处：**命名统一**（金额一律 ``*_cent``，见模块 docstring），
#   而底层 schema 的字段名不必迁就模型。
def project_order(order: Any) -> dict[str, Any]:
    """买家订单详情 → 模型能看的字段。**没有收货人、没有地址、没有手机号。**"""
    return {
        "order_main_no": order.order_main_no,
        "status_text": order.status_text,
        "pay_status_text": order.pay_status_text,
        "payable_amount_cent": order.payable_amount,
        "paid_amount_cent": order.paid_amount,
        "freight_amount_cent": order.freight_amount,
        "discount_amount_cent": order.discount_amount,
        "create_time": _dt(order.create_time),
        "pay_time": _dt(order.pay_time),
        "finish_time": _dt(order.finish_time),
        "buyer_remark": order.buyer_remark,
        "subs": [project_sub(sub) for sub in order.subs],
    }


def project_sub(sub: Any) -> dict[str, Any]:
    return {
        "order_sub_no": sub.order_sub_no,
        "shop_name": sub.shop_name,
        "status_text": sub.status_text,
        "delivery_status_text": sub.delivery_status_text,
        "payable_amount_cent": sub.payable_amount,
        "deliver_time": _dt(sub.deliver_time),
        "receive_time": _dt(sub.receive_time),
        "can_aftersale": sub.can_aftersale,
        "items": [
            {
                "title": item.title,
                "spec_text": item.spec_text,
                "num": item.num,
                "unit_price_cent": item.unit_price,
                "item_amount_cent": item.item_amount,
            }
            for item in sub.items
        ],
        "deliveries": [
            {
                "express_company": delivery.express_company,
                "express_no": delivery.express_no,
                "status_text": delivery.status_text,
                "deliver_time": _dt(delivery.deliver_time),
            }
            for delivery in sub.deliveries
        ],
    }


def project_order_brief(order: Any) -> dict[str, Any]:
    """订单列表项 → 模型能看的字段（列表项本身就只有预览信息）。"""
    return {
        "order_main_no": order.order_main_no,
        "status_text": order.status_text,
        "payable_amount_cent": order.payable_amount,
        "total_num": order.total_num,
        "preview_titles": order.preview_titles,
        "create_time": _dt(order.create_time),
    }


def project_refund(refund: Any) -> dict[str, Any]:
    """售后单 → 模型能看的字段（不含图片路径：那是给页面渲染的，对回答没用）。"""
    return {
        "refund_no": refund.refund_no,
        "order_main_no": refund.order_main_no,
        "shop_name": refund.shop_name,
        "refund_type_text": refund.refund_type_text,
        "reason_type_text": refund.reason_type_text,
        "reason_desc": refund.reason_desc,
        "refund_amount_cent": refund.refund_amount,
        "refund_freight_cent": refund.refund_freight,
        "total_refund_cent": refund.total_refund,
        "freight_bearer_text": refund.freight_bearer_text,
        "status_text": refund.status_text,
        "reject_reason": refund.reject_reason,
        "merchant_remark": refund.merchant_remark,
        "quality_result_text": refund.quality_result_text,
        "return_express": refund.return_express,
        "return_express_no": refund.return_express_no,
        "apply_time": _dt(refund.apply_time),
        "merchant_handle_time": _dt(refund.merchant_handle_time),
        "return_time": _dt(refund.return_time),
        "receive_time": _dt(refund.receive_time),
    }


async def _shop_info(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """本店对外可见的信息（买家问"你们店"时用）。"""
    ticket = ctx.require_ticket()
    shop = await account_service.get_public_shop(ctx.session, ticket.shop_id)
    return {
        "name": shop.name,
        "description": shop.description,
        "status_text": account_service.shop_status_text(shop.status),
    }


async def _ticket_product(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """买家**正在问的那件商品**（价格 / 规格 / 库存档位）。

    ★ 模型**看不到 spu_id**，也没有任何"按 id 查商品"的参数：这件商品来自会话
      （服务端注入）。它拿不到别家店的商品，也拿不到本店的其它商品 ——
      这是"身份只能来自服务端"在买家侧的样子。
    ★ 库存走 ``sku_display``（**档位**，不是真实件数）：真实库存是给爬虫的礼物。
    """
    ticket = ctx.require_ticket()
    if ticket.spu_id is None:
        return {"hint": "这条会话没有关联具体商品；如果是问订单里的东西，看 ticket_order"}
    try:
        spu = await product_service.get_spu_detail(ctx.session, ticket.spu_id)
    except BizError:
        # 买家视角只看得到在售商品 —— 下架/删除的在这里就是"不存在"
        return {"visible": False, "hint": "这件商品现在看不到了（可能已下架）"}
    if int(spu.shop_id) != ticket.shop_id:
        # 理论到不了（spu_id 与会话来自同一个店铺）；fail-closed
        return {"visible": False}

    skus: list[dict[str, Any]] = []
    for sku in spu.skus:
        tier, sold_out = await inventory_service.sku_display(ctx.session, int(sku.id))
        skus.append(
            {
                "spec_text": sku.spec_text,
                "price_cent": sku.price,
                "stock_text": tier,
                "sold_out": sold_out,
            }
        )
    return {
        "title": spu.title,
        "sub_title": spu.sub_title,
        "price_min_cent": spu.price_min,
        "price_max_cent": spu.price_max,
        "total_sold": spu.total_sold,
        "spec_groups": [group.name for group in spu.spec_groups],
        "skus": skus,
    }


async def _ticket_order(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """这条会话**关联的那一单**（服务端注入单号，模型不能指定别的单号）。"""
    ticket = ctx.require_ticket()
    if not ticket.order_main_no:
        return {"hint": "这条会话没有关联订单；要看他在这家店的订单，调 my_orders_in_shop"}
    try:
        order = await trade_service.get_my_order_detail(
            ctx.session, user_id=ticket.buyer_user_id, order_main_no=ticket.order_main_no
        )
    except BizError:
        return {"hint": "这个订单查不到"}
    return project_order(order)


async def _my_orders_in_shop(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """买家**在本店**的订单（最近的几条）。

    ★ ``shop_id`` 是服务端注入的、不是参数：买家问"我买过什么"时，AI 该能说的是
      **这家店**的那些（别家店的订单归别家店的客服）。
    """
    ticket = ctx.require_ticket()
    page = await trade_service.list_my_orders(
        ctx.session, ticket.buyer_user_id, shop_id=ticket.shop_id, limit=_limit(args)
    )
    return {
        "orders": [project_order_brief(order) for order in page.items],
        "has_more": page.has_more,
    }


async def _ticket_refund(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """这条会话关联的售后单（服务端注入单号）。"""
    ticket = ctx.require_ticket()
    if not ticket.refund_no:
        return {"hint": "这条会话没有关联售后单；退款进度也可以从订单里看"}
    try:
        refund = await aftersale_service.get_detail_for_user(
            ctx.session, user_id=ticket.buyer_user_id, refund_no=ticket.refund_no
        )
    except BizError:
        return {"hint": "这个售后单查不到"}
    return project_refund(refund)


# ==================================================================
# 注册表
# ==================================================================
TOOLS: tuple[ToolSpec, ...] = (
    # ---------------- 商家 ----------------
    ToolSpec(
        name="get_shop_info",
        description="本店铺的基本信息与状态（名称、是否正常营业/已关闭/审核中）。"
        "用户问「我的店怎么了」「为什么不能上架」时先调它。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_get_shop_info,
    ),
    ToolSpec(
        name="shop_overview",
        description="本店的经营概览：待发货订单数、待处理售后数、待回复工单数、"
        "可售为 0 的库存行数，以及商品**按状态的数量**"
        "（on_shelf 在售 / draft 草稿 / pending_audit 待审核 / off_shelf 已下架 / "
        "banned 违规下架 / rejected 已驳回；只有 on_shelf 是买家能看到的那部分）。"
        "用户问「今天有什么要处理的」「我店铺什么情况」「我有多少商品上架了」时调它。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_shop_overview,
    ),
    ToolSpec(
        name="list_orders",
        description="本店最近的订单列表（按时间倒序）。每行含子单号、状态、"
        "实付金额（分）、商品数与商品标题。要看某一单的细节再用 get_order。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_TOOL_ROWS}，默认 10",
                }
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_list_orders,
    ),
    ToolSpec(
        name="get_order",
        description="单个订单的详情（金额明细、发货仓、收货人、各时间点、能否发货）。"
        "参数是本店的子单号，从 list_orders 里拿。",
        parameters={
            "type": "object",
            "properties": {
                "order_sub_no": {"type": "string", "description": "子单号"}
            },
            "required": ["order_sub_no"],
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_get_order,
    ),
    ToolSpec(
        name="list_aftersales",
        description="本店的售后/退款列表。pending_only=true 时只看待我处理的。"
        "要看某一单的细节再用 get_aftersale。",
        parameters={
            "type": "object",
            "properties": {
                "pending_only": {
                    "type": "boolean",
                    "description": "只看待商家处理的（待审核/待收货/质检中）",
                },
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_TOOL_ROWS}，默认 10",
                },
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_list_aftersales,
    ),
    ToolSpec(
        name="get_aftersale",
        description="单个售后单的详情（退款金额、运费谁承担、质检结论、限时）。"
        "参数是本店的售后单号，从 list_aftersales 里拿。",
        parameters={
            "type": "object",
            "properties": {"refund_no": {"type": "string", "description": "售后单号"}},
            "required": ["refund_no"],
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_get_aftersale,
    ),
    ToolSpec(
        name="list_warehouses",
        description="本店的仓库列表及各自覆盖的区域数量。用户问「从哪个仓发货」"
        "「我有几个仓」时调它。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_list_warehouses,
    ),
    ToolSpec(
        name="check_freight_config",
        description="运费配置体检：本店有多少规格**既没绑运费模板、也没有可回落的默认模板**"
        "（这类商品买家结算时会报「暂时无法配送」，也是上架被拦的常见原因）。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_check_freight_config,
    ),
    ToolSpec(
        name="list_products",
        description="本店的商品列表（含草稿、已下架、被驳回的）。每行带 status 与 "
        "status_text（草稿 / 已上架 / 已下架 / 违规下架 / 待审核 / 已驳回）。"
        "可用 keyword 按标题搜、用 status 只看某一状态。"
        "问「哪些商品还是草稿」「这件还在卖吗」用 status 过滤，别把整页拉回来自己数。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按商品标题搜索"},
                "status": {
                    "type": "integer",
                    "description": "只看这个状态：1 草稿 2 已上架 3 已下架 "
                    "4 违规下架 5 待审核 6 已驳回。不传 = 全部",
                },
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_TOOL_ROWS}，默认 10",
                },
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_MERCHANT}),
        handler=_list_products,
    ),
    # ---------------- 平台运营 ----------------
    ToolSpec(
        name="platform_overview",
        description="全平台概览：待处理的平台客服会话数、待审核商品数。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_ADMIN}),
        handler=_platform_overview,
    ),
    ToolSpec(
        name="list_pending_spus",
        description="跨店铺的待审核商品列表。运营看审核队列时用。",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_TOOL_ROWS}，默认 10",
                }
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_ADMIN}),
        handler=_list_pending_spus,
    ),
    # ---------------- 买家（店小蜜）----------------
    ToolSpec(
        name=rules.SHOPBOT_ESCALATE_TOOL,
        description=(
            "把这条会话转给商家本人处理。只在你不确定、涉及钱（退款/改价/赔偿）、"
            "或需要商家拍板时调；调用后用一句话告诉买家你已经叫了商家。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "为什么需要人工（一句话，商家会看到）",
                }
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_BUYER}),
        handler=_request_human,
    ),
    # ---------------- 买家（店小蜜）：数据面 ----------------
    # ★ 每条的描述都在回答同一个问题："什么时候该用它" —— 模型选工具全靠这句话。
    ToolSpec(
        name="shop_info",
        description="本店铺的公开信息：店名、简介、是否正常营业。买家问「你们店…」时用。",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_BUYER}),
        handler=_shop_info,
    ),
    ToolSpec(
        name="ticket_product",
        description=(
            "买家**正在问的那件商品**：标题、价格区间、规格名、每个规格的价格、"
            "库存档位文案与是否售罄。问「这件多少钱 / 有货吗 / 有哪些规格」时用它。"
            "★ 它**只能查这条会话关联的那一件**（没有商品 id 参数）。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_BUYER}),
        handler=_ticket_product,
    ),
    ToolSpec(
        name="ticket_order",
        description=(
            "这条会话**关联的那一单**：状态、实付金额、各子单的商品明细与物流"
            "（快递公司、运单号、发货/收货时间）。问「我的订单怎么样了 / 包裹到哪了」时用它。"
            "★ 它**只能查这条会话关联的那一单**。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_BUYER}),
        handler=_ticket_order,
    ),
    ToolSpec(
        name="my_orders_in_shop",
        description=(
            "这个买家**在本店**的订单列表（最近几条，含状态、金额、商品名）。"
            "问「我在你们家买过什么 / 最近一单」时用它。要某一单的明细用 ticket_order。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"返回条数，1-{MAX_TOOL_ROWS}，默认 5",
                }
            },
            "additionalProperties": False,
        },
        roles=frozenset({SCOPE_BUYER}),
        handler=_my_orders_in_shop,
    ),
    ToolSpec(
        name="ticket_refund",
        description=(
            "这条会话**关联的售后单**：退款类型、原因、退款金额（商品款/运费）、"
            "当前进度、驳回理由、退货快递单号。问「退款到哪一步了 / 为什么被拒」时用它。"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        roles=frozenset({SCOPE_BUYER}),
        handler=_ticket_refund,
    ),
)

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def tools_for(caller: rules.Caller) -> list[ToolSpec]:
    """这个身份能用的工具。

    ★ 这是**权限的第一道**也是最强的一道：无权用的工具根本不会出现在交给模型的
      清单里。商家看不到 ``list_pending_spus``，所以它不可能去调。
    """
    return [t for t in TOOLS if caller.role in t.roles]


def openai_tools(specs: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """转成 OpenAI 兼容的 tools 参数（百炼同形）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in specs
    ]


async def execute(tool_ctx: ToolContext, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """执行一个工具调用。

    两道校验，缺一不可：
    1. 工具存在，且**当前身份**在它的 roles 里（纵深防御，正常走不到）；
    2. 参数按 schema 过滤 + 补必填校验（``rules.parse_tool_args`` 在 service 里
       已经做过一次，这里再确认一次调用方没有绕过）。
    """
    spec = _BY_NAME.get(name)
    if spec is None:
        raise BizError(ErrorCode.VALIDATION_ERROR, f"没有这个工具：{name}")
    if tool_ctx.caller.role not in spec.roles:
        raise ToolDenied(name)
    return await spec.handler(tool_ctx, dict(args))
