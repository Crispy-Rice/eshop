-- ============================================================
-- coupon_receive.lua：领券。检查库存 + 限领 + 扣减 + 写幂等，全部原子
--
-- KEYS[1] = coupon:{tpl:X}:stock       剩余可发量
-- KEYS[2] = coupon:{tpl:X}:user_count  hash{userId: 已领张数}
-- KEYS[3] = coupon:{tpl:X}:idem:{key}  幂等键
-- KEYS[4] = coupon:{tpl:X}:meta        模板缓存（预热标记 + 时间 + 状态）
-- ARGV[1] = userId
-- ARGV[2] = perUserLimit
-- ARGV[3] = idempotencyKey
-- ARGV[4] = 幂等结果 TTL（秒）
-- ARGV[5] = 当前时间戳（秒，由应用传入，保证脚本内时间一致）
-- ARGV[6] = 券模板有效期结束时间戳
--
-- 返回 {code, data}
--   0 成功 / 1 已售罄 / 2 超出限领 / 3 幂等命中 / 4 活动未开始或已结束 / 5 未预热
--
-- 这个脚本解决了超发的全部成因（docs/04 §4）：
--   - 检查与扣减非原子 → 全在同一脚本内，Redis 单线程无交错
--   - 多节点各自计数   → 计数在 Redis，所有节点共享
--   - 重试放大         → ① 幂等命中直接返回首次结果
--                      ⑥ 幂等键在扣减**之后**立即写入，重试不会二次扣减
-- ============================================================
local stockKey = KEYS[1]
local ucntKey  = KEYS[2]
local idemKey  = KEYS[3]
local metaKey  = KEYS[4]
local userId   = ARGV[1]
local limit    = tonumber(ARGV[2])
local ttl      = tonumber(ARGV[4])
local nowTs    = tonumber(ARGV[5])
local endTs    = tonumber(ARGV[6])

-- ① 幂等：同一个 Idempotency-Key 已处理过，直接返回上次结果
local prev = redis.call('GET', idemKey)
if prev then
    return {3, prev}
end

-- ② 预热校验：Redis 没预热就不发，避免"看起来没库存"或"绕过限领"
if redis.call('EXISTS', metaKey) == 0 then
    return {5, 'NOT_WARMED'}
end

-- ③ 活动时机校验（在脚本内判断，防止前端改时间）
if nowTs > endTs then
    return {4, 'EXPIRED'}
end
local startTs = tonumber(redis.call('HGET', metaKey, 'startTs') or '0')
if nowTs < startTs then
    return {4, 'NOT_STARTED'}
end
local tplStatus = tonumber(redis.call('HGET', metaKey, 'status') or '0')
if tplStatus ~= 2 then
    return {4, 'STOPPED'}
end

-- ④ 限领校验
local got = tonumber(redis.call('HGET', ucntKey, userId) or '0')
if limit > 0 and got >= limit then
    return {2, got}
end

-- ⑤ 库存校验与扣减。先判后扣，所以不会为负
local stock = tonumber(redis.call('GET', stockKey) or '-1')
if stock < 0 then
    return {5, 'NOT_WARMED'}
end
if stock <= 0 then
    return {1, 0}
end

redis.call('DECR', stockKey)
redis.call('HINCRBY', ucntKey, userId, 1)

-- ⑥ 写幂等结果（券实例在 DB 侧生成，这里返回令牌）
local token = cjson.encode({
    tplId = redis.call('HGET', metaKey, 'tplId'),
    userId = userId,
    ts = nowTs
})
redis.call('SET', idemKey, token, 'EX', ttl)

return {0, token}
