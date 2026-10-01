-- ============================================================
-- stock_lock.lua：单个 SKU 预占（秒杀消费者、单商品下单使用）
--
-- KEYS[1] = stock:{sku:X:wh:Y}:shard:{i}
-- KEYS[2] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[3] = stock:{sku:X:wh:Y}:zero
-- ARGV[1] = qty
-- ARGV[2] = lock TTL 秒
-- ARGV[3] = orderSubNo
-- ARGV[4] = 当前时间戳（由应用传入，不用 redis.call('TIME')，见 docs/04 §4.1）
--
-- 返回 {code, remain}
--   1  成功
--   0  库存不足
--   2  幂等命中（同一子单+SKU 已预占过）
--  -1  库存未初始化，调用方需回源 DB 后重试（docs/14 §7）
--   3  整体售罄
-- ============================================================
local qty     = tonumber(ARGV[1])
local lockKey = KEYS[2]

-- ① 幂等：同一 (子单, SKU) 已预占过就直接返回，绝不重复扣减
local lockedNum = redis.call('HGET', lockKey, 'num')
if lockedNum then
    return {2, tonumber(lockedNum)}
end

-- ② 快速失败：已售罄。有这个标记就不必再去读分片
if redis.call('EXISTS', KEYS[3]) == 1 then
    return {3, 0}
end

-- ③ 库存检查与扣减。整段在单个脚本内执行，Redis 单线程保证原子
local stock = redis.call('GET', KEYS[1])
if not stock then
    return {-1, 0}
end
stock = tonumber(stock)
if stock < qty then
    return {0, stock}
end
redis.call('DECRBY', KEYS[1], qty)

-- ④ 写预占记录。它是回补的依据，也是幂等判断的依据
redis.call('HSET', lockKey, 'num', qty, 'shard', KEYS[1], 'orderNo', ARGV[3],
           'ts', ARGV[4], 'status', 'LOCKED')
redis.call('EXPIRE', lockKey, tonumber(ARGV[2]))

return {1, stock - qty}
