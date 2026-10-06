"""aftersale 模块的领域逻辑。

**所有售后单的状态变更走 ``transit_refund()`` 这一个入口**（行锁 → 状态机查表 →
CAS → 写审计流水），与 trade 的子单状态机同一套做法。

**与子单的联动永远在同一个事务里**：售后单转 60 退款中时子单也转 60，
售后单到终态时子单恢复 ``source_status``。两边要么都变、要么都不变 ——
不会出现"售后说退款了、订单还显示待收货"。

三条容易做错、这里刻意钉住的规则：

1. **库存回补只在"入库质检合格"**（未发货的仅退款除外）。在申请或同意时回补会
   造成"用户不寄回 → 库存虚增 → 超卖"（docs/08 §3.3）。
2. **金额在申请时冻结**（差额法定稿），退款成功时只累加、不回算。
3. **资金退款单独一个事务**：调渠道是网络动作，不能放进 DB 事务持锁。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import after_commit
from app.core.enums import OrderEvent, RefundStatus, SubOrderStatus
from app.core.errors import BizError, ErrorCode, PriceInvariantError
from app.core.logging import get_logger
from app.core.snowflake import next_id
from app.modules.aftersale import calc
from app.modules.aftersale import repository as repo
from app.modules.aftersale.models import (
    NO_REASON_WINDOW_DAYS,
    QUALITY_DEADLINE_HOURS,
    QUALITY_FAIL,
    QUALITY_PASS,
    RECEIVE_DEADLINE_DAYS,
    REFUND_TYPE_TEXT,
    RETURN_DEADLINE_DAYS,
    RETURN_REFUND,
    REVIEW_DEADLINE_HOURS,
    RefundItem,
    RefundOrder,
)
from app.modules.aftersale.schemas import (
    RefundableItemOut,
    RefundCheckOut,
    RefundListOut,
    RefundOut,
    to_list_item_out,
    to_refund_out,
)
from app.modules.aftersale.state_machine import (
    E,
    RefundEvent,
    apply_event_for,
    is_terminal,
    next_status,
    refund_type_for,
    timeout_event,
)
from app.modules.core import outbox
from app.modules.inventory import service as inventory_service
from app.modules.inventory.service import StockItem
from app.modules.payment import service as payment_service
from app.modules.promotion import service as promotion_service
from app.modules.trade import repository as trade_repo
from app.modules.trade import service as trade_service
from app.modules.trade.models import (
    OPERATOR_MERCHANT,
    OPERATOR_SYSTEM,
    OPERATOR_USER,
    ORDER_TYPE_REFUND,
    OrderStateFlow,
)
from app.modules.trade.order_no import build_refund_no
from app.worker.enqueue import enqueue_at

logger = get_logger(__name__)

# 执行资金退款的延迟任务名。tasks.py 里的函数必须叫这个名字（ARQ 按名查找）。
# 放在这里而不是 tasks.py：service 要投递它，反过来 import 会成环
JOB_EXECUTE_REFUND = "execute_refund"


@dataclass(slots=True)
class ApplyItem:
    """申请售后的一个订单项。"""

    order_item_id: int
    num: int


@dataclass(slots=True)
class ApplyRequest:
    order_sub_no: str
    items: list[ApplyItem]
    reason_type: int
    refund_type: int
    reason_desc: str | None = None
    images: list[str] = field(default_factory=list)


# ============================================================
# 状态流转的唯一入口
# ============================================================
async def transit_refund(
    session: AsyncSession,
    refund_no: str,
    event: RefundEvent,
    *,
    operator_type: int = OPERATOR_SYSTEM,
    operator_id: str | None = None,
    remark: str | None = None,
    values: dict | None = None,
) -> RefundStatus:
    """在**调用方的事务内**执行售后单状态流转。

    与 ``trade.transit`` 同样的三层保护：行锁 → 状态机查表 → CAS。
    ``values`` 用来把"与状态同生共死"的字段（deadline、各环节时间、质检结果）
    一起写进去，避免出现"状态变了但 deadline 没更新"的中间态。
    """
    refund = await repo.get_for_update(session, refund_no)
    if refund is None:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")

    from_status = RefundStatus(refund.status)
    to_status = next_status(from_status, event)

    if not await repo.cas_status(
        session,
        refund_no,
        from_status=int(from_status),
        to_status=int(to_status),
        values=values,
    ):
        raise BizError(ErrorCode.AFTERSALE_STATUS_INVALID, "售后单状态已变化，请刷新后重试")

    # 审计流水。order_type = 3 表示售后单（trade 那边只用 1 母单 / 2 子单）
    await trade_repo.insert_state_flow(
        session,
        OrderStateFlow(
            order_type=ORDER_TYPE_REFUND,
            order_no=refund_no,
            from_status=int(from_status),
            to_status=int(to_status),
            event=str(event),
            operator_type=operator_type,
            operator_id=operator_id,
            remark=remark,
        ),
    )

    await outbox.add(
        session,
        topic=outbox.TOPIC_AFTERSALE_STATUS_CHANGED,
        biz_key=f"{refund_no}:{int(to_status)}",
        payload={
            "refundNo": refund_no,
            "orderMainNo": refund.order_main_no,
            "orderSubNo": refund.order_sub_no,
            "userId": refund.user_id,
            "shopId": refund.shop_id,
            "from": int(from_status),
            "to": int(to_status),
            "event": str(event),
        },
    )

    logger.info(
        "售后状态流转",
        extra={"refundNo": refund_no, "from": int(from_status), "to": int(to_status)},
    )
    return to_status


# ============================================================
# 申请售后
# ============================================================
async def apply(
    session: AsyncSession,
    *,
    user_id: int,
    req: ApplyRequest,
    request_id: str,
) -> RefundOrder:
    """用户申请售后。**整段是一个事务**。

    ``request_id`` 是幂等键：同一个键重放直接返回原单；唯一索引是最后一道防线。
    """
    existing = await repo.get_by_request(session, user_id, request_id)
    if existing is not None:
        return existing

    # ① 锁子单。**必须先拿锁再查进行中的售后** —— 锁把并发申请串行化，
    #    第二个请求拿到锁时前一个已经提交，这次查询才看得见它的售后单
    sub = await trade_repo.get_sub_for_update(session, req.order_sub_no)
    if sub is None or int(sub.user_id) != user_id:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")

    active = await repo.get_active_by_sub(session, req.order_sub_no)
    if active is not None:
        raise BizError(
            ErrorCode.AFTERSALE_IN_PROGRESS, "该订单已有进行中的售后，请先处理完再申请"
        )

    # ② 窗口与状态校验
    calc.assert_within_window(sub, req.reason_type)
    source_status = int(sub.status)
    apply_event = apply_event_for(source_status)
    # 售后类型由系统按子单状态强制推导 —— 不信客户端（docs/08 §3.4）
    refund_type = refund_type_for(source_status)
    if req.refund_type != refund_type:
        logger.info(
            "客户端传的售后类型与子单状态不符，已按系统推导的类型纠正",
            extra={"orderSubNo": req.order_sub_no, "client": req.refund_type, "used": refund_type},
        )

    # ③ 锁订单项，算可退数量与金额
    items = await trade_repo.list_items_for_update(session, req.order_sub_no)
    by_id = {int(i.id): i for i in items}
    applying: dict[int, int] = {}
    for line in req.items:
        item = by_id.get(line.order_item_id)
        if item is None:
            raise BizError(ErrorCode.REFUND_NUM_EXCEED, "所选商品不属于该订单")
        applying[line.order_item_id] = applying.get(line.order_item_id, 0) + line.num

    refund_amount = 0
    for item_id, num in applying.items():
        refund_amount += calc.calc_item_refund(by_id[item_id], num)

    whole = calc.is_whole_sub_refund(items, applying)
    refund_freight = calc.calc_freight_refund(sub, whole=whole)
    calc.check_refund_limits(sub, items, refund_amount + refund_freight)

    # ④ 落库
    now = datetime.now(UTC)
    refund = await repo.insert_order(
        session,
        RefundOrder(
            id=next_id(),
            refund_no=build_refund_no(next_id()),
            order_sub_no=sub.order_sub_no,
            order_main_no=sub.order_main_no,
            user_id=user_id,
            shop_id=int(sub.shop_id),
            request_id=request_id,
            refund_type=refund_type,
            reason_type=req.reason_type,
            reason_desc=req.reason_desc,
            images=list(req.images),
            refund_amount=refund_amount,
            refund_freight=refund_freight,
            status=int(RefundStatus.APPLYING),
            source_status=source_status,
            deadline=now + timedelta(hours=REVIEW_DEADLINE_HOURS),
        ),
    )
    await repo.insert_items(
        session,
        [
            RefundItem(
                refund_no=refund.refund_no,
                order_item_id=item_id,
                sku_id=int(by_id[item_id].sku_id),
                refund_num=num,
                refund_amount=calc.calc_item_refund(by_id[item_id], num),
                spu_title_snap=by_id[item_id].spu_title_snap,
                sku_spec_snap=by_id[item_id].sku_spec_snap,
                cover_image_snap=by_id[item_id].cover_image_snap,
            )
            for item_id, num in applying.items()
        ],
    )

    # ⑤ 预占件数：挡住并发申请与超退，退款成功时才转成已退
    for item_id, num in applying.items():
        if not await trade_repo.add_item_refunding_num(session, item_id, num):
            raise BizError(
                ErrorCode.REFUND_NUM_EXCEED, "可退数量不足（可能已有其他售后在退这些商品）"
            )

    await trade_repo.update_sub_fields(
        session, sub.order_sub_no, {"has_aftersale": True}
    )

    # ⑥ 子单进 60 退款中（同事务），母单状态由 aggregate_main 自动复位
    await trade_service.transit(
        session,
        sub.order_sub_no,
        apply_event,
        trade_service.TransitContext(
            operator_type=OPERATOR_USER, operator_id=str(user_id), remark="申请售后"
        ),
    )

    logger.info(
        "售后申请已提交",
        extra={"refundNo": refund.refund_no, "orderSubNo": sub.order_sub_no, "amount": refund_amount},
    )
    return refund


# ============================================================
# 商家审核
# ============================================================
async def approve(
    session: AsyncSession, *, shop_id: int, refund_no: str, remark: str | None = None
) -> RefundOrder:
    """商家同意售后。

    - **退货退款**：等用户寄回（转 30），**不动库存**
    - **仅退款**：货没出库，直接回补库存并立刻发起资金退款（转 20 → 60）
    """
    refund = await _load_for_shop(session, shop_id, refund_no)
    now = datetime.now(UTC)

    if refund.refund_type == RETURN_REFUND:
        await transit_refund(
            session,
            refund_no,
            E.APPROVE_RETURN,
            operator_type=OPERATOR_MERCHANT,
            operator_id=str(shop_id),
            remark=remark,
            values={
                "merchant_handle_time": now,
                "merchant_remark": remark,
                # 换成"用户 7 天内寄回"的截止时间
                "deadline": now + timedelta(days=RETURN_DEADLINE_DAYS),
            },
        )
        return await _reload(session, refund_no)

    # ---- 仅退款：货从未出库 ----
    await _restore_unshipped_stock(session, refund)
    await transit_refund(
        session,
        refund_no,
        E.APPROVE_REFUND,
        operator_type=OPERATOR_MERCHANT,
        operator_id=str(shop_id),
        remark=remark,
        values={"merchant_handle_time": now, "merchant_remark": remark, "deadline": None},
    )
    await _mark_refunding(session, refund)
    await _dispatch_refund(session, refund)
    return await _reload(session, refund_no)


async def reject(
    session: AsyncSession, *, shop_id: int, refund_no: str, reason: str
) -> RefundOrder:
    """商家拒绝售后：释放预占，子单恢复到申请前的状态。"""
    refund = await _load_for_shop(session, shop_id, refund_no)
    await _close_refund(
        session,
        refund,
        event=E.MERCHANT_REJECT,
        trade_event=OrderEvent.REFUND_REJECT,
        operator_type=OPERATOR_MERCHANT,
        operator_id=str(shop_id),
        remark=reason,
        values={
            "reject_reason": reason,
            "merchant_handle_time": datetime.now(UTC),
            "deadline": None,
            "close_time": datetime.now(UTC),
        },
    )
    return await _reload(session, refund_no)


# ============================================================
# 用户侧：撤销 / 填退货单号
# ============================================================
async def revoke(session: AsyncSession, *, user_id: int, refund_no: str) -> RefundOrder:
    """用户撤销申请。只有 10/20/30 能撤 —— 已经寄出（40）就不能撤了。"""
    refund = await repo.get_for_update(session, refund_no)
    if refund is None or int(refund.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    if RefundStatus(refund.status) not in _REVOCABLE:
        raise BizError(
            ErrorCode.AFTERSALE_STATUS_INVALID, "商品已寄出，不能撤销申请"
        )

    await _close_refund(
        session,
        refund,
        event=E.USER_REVOKE,
        trade_event=OrderEvent.USER_REVOKE,
        operator_type=OPERATOR_USER,
        operator_id=str(user_id),
        remark="用户撤销申请",
        values={"deadline": None, "close_time": datetime.now(UTC)},
    )
    return await _reload(session, refund_no)


async def fill_return_express(
    session: AsyncSession,
    *,
    user_id: int,
    refund_no: str,
    express_company: str,
    express_no: str,
) -> RefundOrder:
    """用户寄回并填单号（30 → 40）。库存依然不动。"""
    refund = await repo.get_for_update(session, refund_no)
    if refund is None or int(refund.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")

    now = datetime.now(UTC)
    await transit_refund(
        session,
        refund_no,
        E.FILL_RETURN_EXPRESS,
        operator_type=OPERATOR_USER,
        operator_id=str(user_id),
        remark="已寄回",
        values={
            "return_express": express_company,
            "return_express_no": express_no,
            "return_time": now,
            "deadline": now + timedelta(days=RECEIVE_DEADLINE_DAYS),
        },
    )
    return await _reload(session, refund_no)


# ============================================================
# 商家收货与质检
# ============================================================
async def merchant_receive(
    session: AsyncSession, *, shop_id: int, refund_no: str
) -> RefundOrder:
    """商家签收退货（40 → 50 质检中）。**库存依然不动** —— 要等质检合格。"""
    await _load_for_shop(session, shop_id, refund_no)
    now = datetime.now(UTC)
    await transit_refund(
        session,
        refund_no,
        E.MERCHANT_RECEIVE,
        operator_type=OPERATOR_MERCHANT,
        operator_id=str(shop_id),
        remark="已签收退货",
        values={"receive_time": now, "deadline": now + timedelta(hours=QUALITY_DEADLINE_HOURS)},
    )
    return await _reload(session, refund_no)


async def quality(
    session: AsyncSession,
    *,
    shop_id: int,
    refund_no: str,
    passed: bool,
    remark: str | None = None,
    images: list[str] | None = None,
) -> RefundOrder:
    """商家提交质检结果（50 → 60 退款中 / 51 质检不通过）。

    ★ **合格时在这一步回补库存**（docs/08 §3.3）—— 商品真的回到仓库、且能再卖，
    才让账面库存回来。不合格就进残次品池，不动可售库存。
    """
    refund = await _load_for_shop(session, shop_id, refund_no)
    now = datetime.now(UTC)
    common = {
        "quality_result": QUALITY_PASS if passed else QUALITY_FAIL,
        "quality_remark": remark,
        "quality_images": list(images or []),
        "quality_time": now,
    }

    if passed:
        await _restore_returned_stock(session, refund)
        await transit_refund(
            session,
            refund_no,
            E.QUALITY_PASS,
            operator_type=OPERATOR_MERCHANT,
            operator_id=str(shop_id),
            remark=remark,
            values={**common, "deadline": None},
        )
        await _dispatch_refund(session, refund)
        return await _reload(session, refund_no)

    # 不合格：不进可售库存，商品入残次品池；子单恢复到申请前
    await _mark_defective(session, refund)
    await _close_refund(
        session,
        refund,
        event=E.QUALITY_FAIL,
        trade_event=OrderEvent.REFUND_REJECT,
        operator_type=OPERATOR_MERCHANT,
        operator_id=str(shop_id),
        remark=remark or "质检不通过",
        values={**common, "deadline": None, "close_time": now},
        keep_has_aftersale=True,
    )
    return await _reload(session, refund_no)


# ============================================================
# 退款成功（由资金退款链路回调）
# ============================================================
async def on_refund_success(session: AsyncSession, refund_no: str) -> bool:
    """渠道退款成功：退券 → 累加各处退款额 → 售后与子单一起转 70。

    返回是否真的处理了（幂等：已成功的返回 False）。

    调用方是资金退款任务，**必须与资金退款单的置成功在同一个事务里**。
    """
    refund = await repo.get_for_update(session, refund_no)
    if refund is None:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    if RefundStatus(refund.status) is RefundStatus.SUCCESS:
        return False  # 已经处理过
    if RefundStatus(refund.status) is not RefundStatus.REFUNDING:
        raise BizError(
            ErrorCode.AFTERSALE_STATUS_INVALID,
            f"售后单不在「退款中」，收到退款成功回调（当前 {refund.status}）",
        )

    items = await repo.list_items(session, refund_no)
    delta = int(refund.refund_amount) + int(refund.refund_freight)

    # ① 数量与商品款：在退 → 已退
    for item in items:
        if not await trade_repo.settle_item_refund(
            session, int(item.order_item_id), item.refund_num, int(item.refund_amount)
        ):
            raise PriceInvariantError(
                f"售后项 {item.order_item_id} 的退款数量或金额超出可退范围"
            )

    # ② 子单：商品款 + 运费（与 docs/08 §2.1 的口径一致）
    if not await trade_repo.add_sub_refunded_amount(session, refund.order_sub_no, delta):
        raise BizError(
            ErrorCode.REFUND_AMOUNT_EXCEED, f"退款金额超过子单应付（本次 {delta} 分）"
        )
    # ③ 母单
    if not await trade_repo.add_main_refunded_amount(session, refund.order_main_no, delta):
        raise BizError(
            ErrorCode.REFUND_AMOUNT_EXCEED, f"退款金额超过母单实付（本次 {delta} 分）"
        )

    # ④ 售后单 60 → 70，子单 60 → 70（同事务）
    await transit_refund(
        session,
        refund_no,
        E.REFUND_SUCCESS,
        operator_type=OPERATOR_SYSTEM,
        remark="渠道退款成功",
        values={"refund_time": datetime.now(UTC), "deadline": None},
    )
    await trade_service.transit(
        session,
        refund.order_sub_no,
        OrderEvent.REFUND_SUCCESS,
        trade_service.TransitContext(operator_type=OPERATOR_SYSTEM, remark="退款成功"),
    )
    await trade_repo.update_sub_fields(session, refund.order_sub_no, {"has_aftersale": False})

    # ⑤ 母单支付状态（与履约状态正交，要单独修）
    await trade_service.refresh_main_pay_status(session, refund.order_main_no)

    # ⑥ 整单退款才退券 —— 部分退款时券已经为整笔交易产生过价值
    await _refund_coupons_if_whole_order(session, refund)

    await outbox.add(
        session,
        topic=outbox.TOPIC_AFTERSALE_REFUND_SUCCEEDED,
        biz_key=f"REFUNDED:{refund_no}",
        payload={
            "refundNo": refund_no,
            "orderMainNo": refund.order_main_no,
            "userId": refund.user_id,
            "amount": delta,
        },
    )
    logger.info(
        "售后退款成功",
        extra={"refundNo": refund_no, "orderSubNo": refund.order_sub_no, "amount": delta},
    )
    return True


# ============================================================
# 资格预检（给前端的"可退多少"）
# ============================================================
async def check_eligibility(
    session: AsyncSession, *, user_id: int, order_sub_no: str
) -> RefundCheckOut:
    """售后资格预检。**只读**，让前端在用户填表前就知道能退多少、退什么类型。"""
    sub = await trade_repo.get_sub(session, order_sub_no)
    if sub is None or int(sub.user_id) != user_id:
        raise BizError(ErrorCode.ORDER_ITEM_NOT_FOUND, "订单不存在")

    def blocked(reason: str) -> RefundCheckOut:
        return RefundCheckOut(
            refundable=False,
            reason=reason,
            refund_type=0,
            refund_type_text="",
            max_item_amount=0,
            max_freight=0,
        )

    if await repo.get_active_by_sub(session, order_sub_no) is not None:
        return blocked("该订单已有进行中的售后，请先在售后详情里处理")

    try:
        refund_type = refund_type_for(int(sub.status))
    except BizError:
        return blocked("该订单当前状态不支持申请售后")

    items = await trade_repo.list_items_of_subs(session, [order_sub_no])
    lines = [
        RefundableItemOut(
            order_item_id=i.id,
            sku_id=i.sku_id,
            title=i.spu_title_snap,
            spec_text=i.sku_spec_snap,
            cover_image=i.cover_image_snap,
            num=i.num,
            refunded_num=i.refunded_num,
            refunding_num=i.refunding_num,
            max_num=i.num - i.refunded_num - i.refunding_num,
            unit_payable=(
                (i.payable_amount - i.refunded_amount)
                // max(1, i.num - i.refunded_num - i.refunding_num)
            ),
        )
        for i in items
        if i.num - i.refunded_num - i.refunding_num > 0
    ]
    if not lines:
        return blocked("该订单的商品都已退完")

    max_item = sum(i.payable_amount - i.refunded_amount for i in items if i.refunded_num < i.num)
    # 全退时才有运费可退（docs/08 §2.3：运费总在最后一笔售后退）
    max_freight = int(sub.freight_amount) if len(lines) == len(items) else 0

    notices = ["部分退货不退运费"] if max_freight == 0 and sub.freight_amount > 0 else []
    return RefundCheckOut(
        refundable=True,
        refund_type=refund_type,
        refund_type_text=REFUND_TYPE_TEXT.get(refund_type, ""),
        max_item_amount=max_item,
        max_freight=max_freight,
        deadline=_window_deadline(sub),
        items=lines,
        notices=notices,
    )


def _window_deadline(sub) -> datetime | None:
    """售后窗口的截止时间。未签收时没有截止（随时可退）。

    纯函数，**不要写成 async** —— 它没有任何 I/O，写成协程会被调用方
    当成值直接塞进响应模型（pydantic 会报"输入不是 datetime"）。
    """
    if sub.receive_time is None:
        return None
    received = sub.receive_time
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)
    return received + timedelta(days=NO_REASON_WINDOW_DAYS)


# ============================================================
# 内部工具
# ============================================================
_REVOCABLE = frozenset({RefundStatus.APPLYING, RefundStatus.WAIT_REFUND, RefundStatus.WAIT_RETURN})


async def _reload(session: AsyncSession, refund_no: str) -> RefundOrder:
    refund = await repo.get_by_no(session, refund_no)
    if refund is None:  # pragma: no cover - 刚写过
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    return refund


async def _load_for_shop(session: AsyncSession, shop_id: int, refund_no: str) -> RefundOrder:
    refund = await repo.get_for_update(session, refund_no)
    if refund is None or int(refund.shop_id) != shop_id:
        # 不区分"不存在"与"不是你的" —— 别让人遍历探测别人的售后单
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    return refund


async def _close_refund(
    session: AsyncSession,
    refund: RefundOrder,
    *,
    event: RefundEvent,
    trade_event: OrderEvent,
    operator_type: int,
    operator_id: str | None,
    remark: str | None,
    values: dict,
    keep_has_aftersale: bool = False,
) -> None:
    """走到"不退款"的终态时的共同动作：售后终态 → 释放预占 → 子单恢复原状态。

    拒绝、撤销、质检不通过三条路径做的事完全一样，只有事件名与备注不同。
    """
    await transit_refund(
        session,
        refund.refund_no,
        event,
        operator_type=operator_type,
        operator_id=operator_id,
        remark=remark,
        values=values,
    )

    for item in await repo.list_items(session, refund.refund_no):
        await trade_repo.release_item_refunding_num(
            session, int(item.order_item_id), item.refund_num
        )

    # ★ 子单恢复到申请前的状态。REFUND_REJECT / USER_REVOKE 的目标是"原状态"，
    #   必须靠 restore_to 传进去（20→60 与 30→60 要各回各家）
    await trade_service.transit(
        session,
        refund.order_sub_no,
        trade_event,
        trade_service.TransitContext(
            operator_type=operator_type,
            operator_id=operator_id,
            remark=remark,
            restore_to=SubOrderStatus(int(refund.source_status)),
        ),
    )
    if not keep_has_aftersale:
        await trade_repo.update_sub_fields(session, refund.order_sub_no, {"has_aftersale": False})


async def _mark_refunding(session: AsyncSession, refund: RefundOrder) -> None:
    """仅退款分支：20 待退款 → 60 退款中。

    **只有仅退款这一条路需要它** —— 退货退款在质检合格时已经直接进 60 了，
    再转一次会撞上状态机（60 没有 START_REFUND 这条出边）。
    """
    await transit_refund(
        session,
        refund.refund_no,
        E.START_REFUND,
        operator_type=OPERATOR_SYSTEM,
        remark="发起资金退款",
        values={"deadline": None},
    )


async def _dispatch_refund(session: AsyncSession, refund: RefundOrder) -> None:
    """把退款交给资金侧：建资金退款单 + 提交后触发渠道调用。

    调用前售后单必须已经在 60 退款中（仅退款走 ``_mark_refunding``，
    退货退款在质检合格时直接到 60）。
    """
    funding = await payment_service.create_refund(
        session,
        refund_biz_no=refund.refund_no,
        order_main_no=refund.order_main_no,
        amount=int(refund.refund_amount) + int(refund.refund_freight),
    )
    # 渠道调用是网络动作，**必须等事务提交之后**再做。
    # 即使这次投递丢了也不影响正确性：`retry_refunds` cron 会扫到 status=0 的单子。
    #
    # ★ 投递的是**资金退款单号**，不是售后单号 —— 两个号长得很像（都以 R 开头、
    #   同一天、同样的位数），传错的话任务会去查一张不存在的资金退款单。
    after_commit.defer(
        session,
        lambda: enqueue_at(JOB_EXECUTE_REFUND, funding.refund_no, run_at=datetime.now(UTC)),
    )


async def _stock_items_of_refund(session: AsyncSession, refund: RefundOrder) -> list[StockItem]:
    """把售后明细聚合成库存操作要的行（按 SKU 合并，都在**同一个仓**）。

    ★ **不重新路由**：发货仓取自这张售后单所属子单记录的 ``warehouse_id`` ——
      也就是"当初从哪个仓发的"。用路由重算的话，商家事后调整区域规则就会让退货
      入到一个根本没发过货的仓，账面越滚越乱。

    ★ 按 SKU 合并是安全的：``refund_order.order_sub_no`` 非空，**一个售后单必属于一个
      子单**，而一个子单所有商品同仓（见 trade.create_order）。所以合并出来的每一行
      都在同一个仓、也共用同一个幂等键。
      （若将来支持"跨子单售后"，这里要改成按 ``(sku, 仓)`` 聚合、幂等键也要带上仓。）
    """
    items = await repo.list_items(session, refund.refund_no)
    if not items:
        return []
    merged: dict[int, int] = {}
    for item in items:
        merged[int(item.sku_id)] = merged.get(int(item.sku_id), 0) + item.refund_num
    warehouse_id = await trade_service.warehouse_id_of_sub(session, str(refund.order_sub_no))
    return [
        StockItem(sku_id=sku_id, warehouse_id=warehouse_id, num=num)
        for sku_id, num in merged.items()
    ]


async def _restore_unshipped_stock(session: AsyncSession, refund: RefundOrder) -> None:
    """未发货仅退款的库存回补：``frozen → available``（总量不变）。"""
    await inventory_service.unshipped_refund(
        session,
        await _stock_items_of_refund(session, refund),
        f"REFUND_STOCK:{refund.refund_no}",
        order_no=refund.refund_no,
    )
    await repo.mark_items_restored(session, refund.refund_no)


async def _restore_returned_stock(session: AsyncSession, refund: RefundOrder) -> None:
    """退货质检合格的库存回补：``total += n, available += n``（货真的回来了）。"""
    await inventory_service.return_in(
        session,
        await _stock_items_of_refund(session, refund),
        f"RETURN:{refund.refund_no}",
        order_no=refund.refund_no,
    )
    await repo.mark_items_restored(session, refund.refund_no)


async def _mark_defective(session: AsyncSession, refund: RefundOrder) -> None:
    """质检不合格：商品进残次品池，不回补可售库存。"""
    await inventory_service.mark_defective(
        session,
        await _stock_items_of_refund(session, refund),
        f"DEFECTIVE:{refund.refund_no}",
        order_no=refund.refund_no,
    )


async def _refund_coupons_if_whole_order(session: AsyncSession, refund: RefundOrder) -> None:
    """整单退款成功时把券退回；部分退款不退（docs/08 §4.1）。

    判定时机是在**当前子单已经置为已退款之后** —— 所以调用点必须在
    ``trade_service.transit(REFUND_SUCCESS)`` 之后。
    """
    sub_count, refunded_subs = await trade_repo.count_refunded_subs(
        session, refund.order_main_no
    )
    if refunded_subs < sub_count:
        logger.info(
            "部分退款，不退回优惠券",
            extra={"orderMainNo": refund.order_main_no, "refundedSubs": refunded_subs},
        )
        return

    # 券的 id 记在优惠快照的 source_id 上（下单时写入，见 trade._write_discount_snapshots）
    snapshots = await trade_repo.list_discount_snapshots(session, refund.order_main_no)
    for snap in snapshots:
        if not snap.source_type.startswith("COUPON_") or int(snap.source_id) <= 0:
            continue
        await promotion_service.refund(
            session,
            code_id=int(snap.source_id),
            user_id=refund.user_id,
            refund_no=refund.refund_no,
        )


# ============================================================
# 查询
# ============================================================
def _encode_cursor(created_at: datetime, row_id: int) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw_time, raw_id = base64.urlsafe_b64decode(padded).decode().split("|")
        return datetime.fromisoformat(raw_time), int(raw_id)
    except (ValueError, TypeError) as exc:
        raise BizError(ErrorCode.VALIDATION_ERROR, "分页游标无效，请重新加载") from exc


async def _shop_names_of(session: AsyncSession, orders: list[RefundOrder]) -> dict[str, str]:
    """批量取店铺名（快照在子单上）。列表页避免 N+1。"""
    subs = await trade_repo.get_subs_by_nos(
        session, list({o.order_sub_no for o in orders})
    )
    return {no: s.shop_name_snap for no, s in subs.items()}


async def get_detail_for_user(
    session: AsyncSession, *, user_id: int, refund_no: str
) -> RefundOut:
    """买家看自己的售后单。别人的一律返回"不存在"（不泄露单号是否存在）。"""
    order = await repo.get_by_no(session, refund_no)
    if order is None or int(order.user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    return await _detail(session, order)


async def get_detail_for_shop(
    session: AsyncSession, *, shop_id: int, refund_no: str
) -> RefundOut:
    order = await repo.get_by_no(session, refund_no)
    if order is None or int(order.shop_id) != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "售后单不存在")
    return await _detail(session, order)


async def _detail(session: AsyncSession, order: RefundOrder) -> RefundOut:
    items = await repo.list_items(session, order.refund_no)
    names = await _shop_names_of(session, [order])
    return to_refund_out(order, items, shop_name=names.get(order.order_sub_no, ""))


async def list_my_refunds(
    session: AsyncSession,
    user_id: int,
    *,
    status: int | None = None,
    cursor: str | None = None,
    limit: int = 10,
) -> RefundListOut:
    return await _list(
        session,
        await repo.list_for_user(
            session,
            user_id,
            status=status,
            cursor=_decode_cursor(cursor) if cursor else None,
            limit=limit + 1,
        ),
        limit=limit,
        to_out=to_list_item_out,
    )


async def list_shop_refunds(
    session: AsyncSession,
    shop_id: int,
    *,
    status: int | None = None,
    pending_only: bool = False,
    cursor: str | None = None,
    limit: int = 20,
) -> RefundListOut:
    return await _list(
        session,
        await repo.list_for_shop(
            session,
            shop_id,
            status=status,
            # "待我处理"= 待审核 / 待收货 / 质检中
            statuses=(10, 40, 50) if pending_only else None,
            cursor=_decode_cursor(cursor) if cursor else None,
            limit=limit + 1,
        ),
        limit=limit,
        to_out=to_list_item_out,
    )


async def _list(session: AsyncSession, rows: list[RefundOrder], *, limit: int, to_out):
    """列表的公共部分：多取一条判 has_more、批量取明细与店铺名。"""
    has_more = len(rows) > limit
    page = rows[:limit]
    items = await repo.list_items_of_orders(session, [o.refund_no for o in page])
    by_order: dict[str, list[RefundItem]] = {}
    for item in items:
        by_order.setdefault(item.refund_no, []).append(item)
    names = await _shop_names_of(session, page)

    return RefundListOut(
        items=[
            to_out(o, by_order.get(o.refund_no, []), shop_name=names.get(o.order_sub_no, ""))
            for o in page
        ],
        next_cursor=(
            _encode_cursor(page[-1].created_at, page[-1].id) if has_more and page else None
        ),
        has_more=has_more,
    )


# ============================================================
# 超时处理（cron 调用）
# ============================================================
async def process_timeouts(session: AsyncSession, *, limit: int = 200) -> int:
    """处理到点的售后单。返回处理了几单。

    所有环节共用一个 ``deadline`` 字段与一条扫描 SQL（docs/08 §7），
    具体动作由 ``timeout_event`` 按当前状态决定。每条包一个 SAVEPOINT ——
    一个坏单不该让整批扫描失败。
    """
    nos = await repo.list_timeout(session, now=datetime.now(UTC), limit=limit)
    done = 0
    for refund_no in nos:
        try:
            async with session.begin_nested():
                if await _handle_timeout(session, refund_no):
                    done += 1
        except BizError as exc:
            logger.warning(
                "售后超时处理失败，已跳过",
                extra={"refundNo": refund_no, "code": str(exc.code), "reason": exc.message},
            )
    if done:
        logger.info("售后超时处理完成", extra={"scanned": len(nos), "done": done})
    return done


async def _handle_timeout(session: AsyncSession, refund_no: str) -> bool:
    """单个售后单的超时处理。走状态机，所以重复执行无副作用。"""
    refund = await repo.get_for_update(session, refund_no)
    if refund is None or refund.deadline is None:
        return False
    status = RefundStatus(refund.status)
    if is_terminal(status):
        return False

    event = timeout_event(status, int(refund.refund_type))
    now = datetime.now(UTC)

    if event is E.AUTO_APPROVE_RETURN:
        await transit_refund(
            session,
            refund_no,
            event,
            remark="商家审核超时，系统自动同意",
            values={"deadline": now + timedelta(days=RETURN_DEADLINE_DAYS)},
        )
        await _alert_review_timeout(session, refund)
        return True

    if event is E.AUTO_APPROVE_REFUND:
        # 与商家手动同意走同一条路：回补库存 + 发起退款
        await _restore_unshipped_stock(session, refund)
        await transit_refund(
            session,
            refund_no,
            event,
            remark="商家审核超时，系统自动同意",
            values={"deadline": None},
        )
        await _mark_refunding(session, refund)
        await _dispatch_refund(session, refund)
        await _alert_review_timeout(session, refund)
        return True

    if event is E.TIMEOUT_CLOSE:
        # 用户 7 天没寄回 → 关闭，释放预占、子单复位
        await _close_refund(
            session,
            refund,
            event=event,
            trade_event=OrderEvent.REFUND_REJECT,
            operator_type=OPERATOR_SYSTEM,
            operator_id=None,
            remark="用户超时未寄回，售后关闭",
            values={"deadline": None, "close_time": now},
        )
        return True

    if event is E.AUTO_RECEIVE:
        await transit_refund(
            session,
            refund_no,
            event,
            remark="商家超时未收货，系统自动签收",
            values={"receive_time": now, "deadline": now + timedelta(hours=QUALITY_DEADLINE_HOURS)},
        )
        return True

    if event is E.AUTO_QUALITY_PASS:
        # 质检超时视同合格，与人工合格走同一条路
        await _restore_returned_stock(session, refund)
        await transit_refund(
            session,
            refund_no,
            event,
            remark="商家超时未质检，系统自动通过",
            values={"deadline": None},
        )
        await _dispatch_refund(session, refund)
        return True

    return False


async def _alert_review_timeout(session: AsyncSession, refund: RefundOrder) -> None:
    """商家 48 小时没审核，系统替他同意了 —— 记一条告警，运营要看得到。"""
    from app.modules.core.models import OpsAlert

    session.add(
        OpsAlert(
            level=2,
            source="aftersale.timeout",
            title="商家审核售后超时，已自动同意",
            detail={
                "refundNo": refund.refund_no,
                "shopId": str(refund.shop_id),
                "refundType": int(refund.refund_type),
                "amount": int(refund.refund_amount) + int(refund.refund_freight),
            },
        )
    )


# ============================================================
# 退款对账（每日 cron）
# ============================================================
# 这几条 SQL 是**资金安全的体检表**（docs/08 §10）。正常情况永远查不出东西，
# 查出来就是 P0：说明某个环节的钱算错了或写漏了。
#
# 跨 schema 直接查在这里是可接受的例外：对账任务本来就要横跨所有相关表，
# 而且它只读、不改任何模块的数据（发现问题的动作是写 ops.alert）。
_RECONCILE_CHECKS: dict[str, str] = {
    # ① 子单：已退不超过应付（CHECK 约束已保证，这里只是监控约束还在不在）
    "sub_over_refund": """
        SELECT count(*) FROM trade.order_sub WHERE refunded_amount > payable_amount
    """,
    # ② 订单项：退款数量与商品款都守恒
    "item_over_refund": """
        SELECT count(*) FROM trade.order_item
        WHERE refunded_num > num OR refunded_amount > payable_amount
    """,
    # ③ 子单退款额 = Σ订单项商品款 + Σ已成功售后的运费
    "sub_amount_mismatch": """
        SELECT count(*) FROM trade.order_sub s
        WHERE s.refunded_amount > 0
          AND s.refunded_amount <> (
                SELECT COALESCE(SUM(i.refunded_amount), 0) FROM trade.order_item i
                 WHERE i.order_sub_no = s.order_sub_no
              ) + (
                SELECT COALESCE(SUM(r.refund_freight), 0) FROM aftersale.refund_order r
                 WHERE r.order_sub_no = s.order_sub_no AND r.status = 70
              )
    """,
    # ④ 母单退款额 = Σ子单退款额
    "main_amount_mismatch": """
        SELECT count(*) FROM trade.order_main m
        WHERE m.refunded_amount <> (
            SELECT COALESCE(SUM(s.refunded_amount), 0) FROM trade.order_sub s
             WHERE s.order_main_no = m.order_main_no
        )
    """,
    # ⑤ 售后说退款成功、子单却还没退款（docs/07 §12 的状态同步兜底）
    "status_unsynced": """
        SELECT count(*) FROM aftersale.refund_order r
        JOIN trade.order_sub s ON s.order_sub_no = r.order_sub_no
        WHERE r.status = 70 AND s.status <> 70
          AND r.refund_time < now() - interval '10 minutes'
    """,
}


async def reconcile_refunds(session: AsyncSession) -> dict[str, int]:
    """跑一遍资金对账，有异常就写 ``ops.alert``。返回每项检查的问题条数。"""
    from sqlalchemy import text

    from app.modules.core.models import OpsAlert

    results: dict[str, int] = {}
    for name, sql in _RECONCILE_CHECKS.items():
        results[name] = int(await session.scalar(text(sql)) or 0)

    bad = {k: v for k, v in results.items() if v > 0}
    if bad:
        session.add(
            OpsAlert(
                level=1,
                source="aftersale.reconcile",
                title=f"退款对账发现 {len(bad)} 项异常，请立即核查",
                detail=bad,
            )
        )
        logger.error("退款对账异常", extra={"violations": bad})
    return results


async def count_open_refunds(session: AsyncSession, user_id: int) -> int:
    """该用户还有多少笔进行中的售后。

    ★ 给账号注销做前置守卫，理由同 ``trade.service.count_blocking_orders``：
      account 不能 import aftersale（aftersale → trade → …，反向即环），
      所以由这里暴露只读查询、account 的路由层来编排。
    """
    return await repo.count_user_open_refunds(session, user_id)
