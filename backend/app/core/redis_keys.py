"""Redis key 的唯一来源。

**业务代码禁止手拼 key 字符串**，一律从这里取（docs/14-redis-keys.md §1）。
好处：
- 同一个 Lua 脚本涉及的 key 用相同的 hash tag（``{...}``），
  第一期单实例无所谓，将来切 Cluster 时不用改 key 命名
- 改 key 命名只需改这一个文件
"""

from __future__ import annotations


# ============================================================
# 库存
# ============================================================
def stock_shard(sku_id: int, wh_id: int, index: int) -> str:
    """分片可售库存。同一 `(sku, wh)` 下的分片共享 hash tag。"""
    return f"stock:{{sku:{sku_id}:wh:{wh_id}}}:shard:{index}"


def stock_meta(sku_id: int, wh_id: int) -> str:
    """库存元信息 hash：total/available/locked/frozen/version/syncTs。"""
    return f"stock:{{sku:{sku_id}:wh:{wh_id}}}:meta"


def stock_zero(sku_id: int, wh_id: int) -> str:
    """售罄标记：存在即快速拒绝，避免每次都去查分片。"""
    return f"stock:{{sku:{sku_id}:wh:{wh_id}}}:zero"


def stock_lock(order_sub_no: str, sku_id: int) -> str:
    """单 SKU 预占记录（秒杀消费者等单商品场景）。"""
    return f"stock:lock:{order_sub_no}:{sku_id}"


def stock_batch(request_id: str) -> str:
    """批量预占记录：整单所有 SKU 的预占情况 + 状态，是回补的依据。"""
    return f"stock:batch:{request_id}"


def stock_init_lock(sku_id: int, wh_id: int) -> str:
    """库存回源 DB 初始化时的互斥锁。"""
    return f"lock:stock_init:{sku_id}:{wh_id}"


# ============================================================
# 优惠券
# ============================================================
def coupon_tpl_stock(tpl_id: int) -> str:
    return f"coupon:{{tpl:{tpl_id}}}:stock"


def coupon_tpl_user_count(tpl_id: int) -> str:
    return f"coupon:{{tpl:{tpl_id}}}:user_count"


def coupon_tpl_meta(tpl_id: int) -> str:
    return f"coupon:{{tpl:{tpl_id}}}:meta"


def coupon_tpl_idem(tpl_id: int, idempotency_key: str) -> str:
    """领券幂等键。带上 tpl hash tag，保证与同脚本其他 key 落同一 slot。"""
    return f"coupon:{{tpl:{tpl_id}}}:idem:{idempotency_key}"


def coupon_code(code_id: int) -> str:
    return f"coupon:code:{code_id}"


def coupon_user(user_id: int) -> str:
    """用户券列表缓存：hash {codeId: status}。"""
    return f"coupon:user:{user_id}"


def coupon_ip_count(ip: str, tpl_id: int) -> str:
    return f"coupon:ip:{ip}:{tpl_id}"


# ============================================================
# 幂等与限流
# ============================================================
def idempotency(user_id: int, method: str, path: str, key: str) -> str:
    return f"idem:{user_id}:{method}:{path}:{key}"


def rate_limit_user(user_id: int, route: str) -> str:
    return f"rate:user:{user_id}:{route}"


def rate_limit_ip(ip: str) -> str:
    return f"rate:ip:{ip}"


def login_fail_count(account_key: str) -> str:
    return f"login:fail:{account_key}"


# ============================================================
# 秒杀排队
# ============================================================
def seckill_queue(act_id: int) -> str:
    return f"seckill:{{act:{act_id}}}:queue"


def seckill_uid_set(act_id: int) -> str:
    return f"seckill:{{act:{act_id}}}:uid_set"


def seckill_stock(act_id: int) -> str:
    return f"seckill:{{act:{act_id}}}:stock"


def seckill_result(act_id: int, user_id: int) -> str:
    return f"seckill:result:{act_id}:{user_id}"


def seckill_consumer_lock(act_id: int) -> str:
    """保证同一活动只被一个消费协程消费。"""
    return f"seckill:consumer:{act_id}"


# ============================================================
# 缓存（TTL 都带随机偏移，防雪崩）
# ============================================================
def cache_sku(sku_id: int) -> str:
    return f"cache:sku:{sku_id}"


def cache_spu(spu_id: int) -> str:
    return f"cache:spu:{spu_id}"


def cache_null_sku(sku_id: int) -> str:
    """空值缓存，防穿透。"""
    return f"cache:null:sku:{sku_id}"


def cache_freight_tpl(tpl_id: int) -> str:
    return f"cache:freight_tpl:{tpl_id}"


# ============================================================
# 其他
# ============================================================
def switch(name: str) -> str:
    """降级开关的缓存副本，真源在 ops.switch 表（Redis 故障时仍可读）。"""
    return f"switch:{name}"


def snowflake_worker(worker_id: int) -> str:
    """雪花算法 worker id 的租约（docs/13-schema.md §0.4）。"""
    return f"snowflake:worker:{worker_id}"


def stream(topic: str) -> str:
    return f"stream:{topic}"


def stream_dead(topic: str) -> str:
    return f"stream:dead:{topic}"
