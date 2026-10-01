"""aftersale 的定时任务：资金退款执行、退款重试、售后超时、资金对账。

注册在 ``app/worker/main.py``。

**退款为什么不是一个事务走到底**（docs/08 §6）：调支付渠道是外部网络请求，
放进数据库事务会长时间持有行锁。所以拆成两段：

    事务 A（售后进"退款中"）：回补库存 → 建资金退款单 → 提交
       ↓ after_commit 即时投递 / cron 兜底扫描
    调渠道（此处无事务、无连接）
       ↓
    事务 B（退款成功）：退券 → 累加各处退款额 → 售后与子单一起转 70 → 提交

渠道退款失败只重试，不会回滚已完成的库存回补；渠道侧按商户退款单号幂等，
重复调用不会重复打款。

**编排放在 aftersale 而不是 payment**：退款是售后的业务流程，payment 只提供
"把一笔钱打回渠道"这件技术动作。这样依赖是单向的（aftersale → payment），
payment 不需要反过来 import aftersale。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.modules.aftersale import service
from app.modules.payment import repository as payment_repo
from app.modules.payment import service as payment_service
from app.modules.payment.models import REFUND_SUCCESS

logger = get_logger(__name__)

# 每次重试扫描最多处理多少笔，避免单轮任务跑太久
RETRY_BATCH = 100


async def execute_refund(ctx: dict[str, Any], refund_no: str) -> dict[str, Any]:
    """执行一笔资金退款（资金退款单号）。

    可以安全重复执行：渠道调用按商户退款单号幂等，本地置成功是 CAS。
    """
    return await _execute_one(ctx["session_factory"], refund_no)


async def retry_refunds(ctx: dict[str, Any]) -> dict[str, Any]:
    """兜底扫描：把"待退款"与"退款失败"的单子重新推一遍。

    为什么需要它：``after_commit`` 的投递可能丢失（进程在提交后、投递前崩溃），
    渠道也可能临时故障。每分钟扫一次就够了 —— 退款晚几分钟到账没有影响。
    """
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        refund_nos = await payment_service.list_retryable_refunds(session, limit=RETRY_BATCH)
        await session.rollback()  # 只读，别占着连接

    done = 0
    for refund_no in refund_nos:
        result = await _execute_one(session_factory, refund_no)
        if result.get("ok"):
            done += 1
    if refund_nos:
        logger.info("退款重试完成", extra={"scanned": len(refund_nos), "succeeded": done})
    return {"scanned": len(refund_nos), "succeeded": done}


async def process_refund_timeouts(ctx: dict[str, Any]) -> dict[str, Any]:
    """处理到点的售后单（docs/08 §7）。每分钟一次。

    四个环节共用一个 ``deadline`` 字段 + 一条扫描 SQL：
    商家审核超时 → 自动同意；用户寄回超时 → 关闭；商家收货超时 → 自动签收；
    质检超时 → 自动通过。走的都是状态机，重复执行无副作用。
    """
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            done = await service.process_timeouts(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return {"processed": done}


async def reconcile_refunds(ctx: dict[str, Any]) -> dict[str, Any]:
    """每日资金对账（docs/08 §10）。有问题写 ``ops.alert``。"""
    session_factory = ctx["session_factory"]
    async with session_factory() as session:
        try:
            results = await service.reconcile_refunds(session)
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return results


# ============================================================
# 内部
# ============================================================
async def _execute_one(session_factory: Any, refund_no: str) -> dict[str, Any]:
    """执行一笔资金退款：只读拿数据 → 调渠道 → 一个事务里落成功并推进售后。"""
    # ① 只读。拿到数据后**立刻结束事务**，不要在持有连接时调渠道
    async with session_factory() as session:
        refund = await payment_repo.get_refund_by_no(session, refund_no)
        if refund is None:
            await session.rollback()
            return {"refundNo": refund_no, "skipped": "not_found"}
        if int(refund.status) == REFUND_SUCCESS:
            await session.rollback()
            return {"refundNo": refund_no, "skipped": "already_succeeded"}
        amount = int(refund.amount)
        refund_biz_no = refund.refund_biz_no
        await session.rollback()

    # ② 调渠道。失败就记失败并排重试（不影响已经回补的库存）
    try:
        channel_refund_no = await payment_service.call_channel_refund(refund_no, amount)
    except Exception as exc:
        logger.warning(
            "渠道退款失败，将重试", extra={"refundNo": refund_no}, exc_info=True
        )
        async with session_factory() as session, session.begin():
            await payment_service.mark_refund_failed(session, refund_no, reason=str(exc))
        return {"refundNo": refund_no, "ok": False, "error": str(exc)}

    # ③ 一个事务：资金退款单置成功 + 售后流转（事务 B）
    async with session_factory() as session, session.begin():
        if not await payment_service.mark_refund_success(
            session, refund_no, channel_refund_no=channel_refund_no
        ):
            # 已经被别的执行者置成功了（并发重试），不重复推进业务
            return {"refundNo": refund_no, "skipped": "already_succeeded"}
        await service.on_refund_success(session, refund_biz_no)

    logger.info("资金退款完成", extra={"refundNo": refund_no, "amount": amount})
    return {"refundNo": refund_no, "ok": True, "amount": amount}
