-- ============================================================
-- coupon_lock.lua：锁券（下单时占用）
--
-- KEYS[1] = coupon:code:{codeId}
-- ARGV[1] = orderMainNo
-- ARGV[2] = 锁定时长（秒）
-- ARGV[3] = 当前时间戳（秒）
--
-- 返回 {code}
--   1 成功 / 2 已使用 / 3 被其他订单锁定 / 4 已过期 / 0 券不存在（缓存未命中，回源 DB）
--
-- ★ Redis 侧只是**缓存**，让结算页能快速判断；锁券的权威记录是 DB 的条件更新
--   （docs/04 §6）。所以这里失败不代表一定不能用（可能是缓存没预热），
--   返回 0 时调用方应当回源 DB 再判断。
--
-- ★ 不修改券的 TTL：券缓存的生命周期始终跟随券有效期。原方案在锁券时把 TTL
--   改成 30 分钟，订单关闭后 key 过期消失，下次查询再回源 —— 那样会让
--   "券到底还在不在"变得难以推理。解锁由关单流程显式执行（docs/14 §3.6）。
-- ============================================================
local codeKey = KEYS[1]
if redis.call('EXISTS', codeKey) == 0 then
    return {0}
end

local status   = redis.call('HGET', codeKey, 'status')
local orderNo  = redis.call('HGET', codeKey, 'orderNo')
local validEnd = tonumber(redis.call('HGET', codeKey, 'validEnd') or '0')
local nowTs    = tonumber(ARGV[3])

if status == 'USED' then
    return {2}
end

if status == 'LOCKED' then
    if orderNo == ARGV[1] then
        return {1}                    -- 同一订单重复锁，幂等成功
    end
    return {3}
end

if validEnd > 0 and nowTs > validEnd then
    redis.call('HSET', codeKey, 'status', 'EXPIRED')
    return {4}
end

redis.call('HSET', codeKey, 'status', 'LOCKED', 'orderNo', ARGV[1], 'lockTs', ARGV[3])

return {1}
