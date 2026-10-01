-- ============================================================
-- stock_release.lua：单个 SKU 预占回补
--
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}
-- KEYS[2] = 该预占记录中的分片 key（调用方先 HGET shard 再传入，
--           保证 Cluster 兼容 —— 见 docs/14 §3.4）
-- KEYS[3] = stock:{sku:X:wh:Y}:zero
-- ARGV[1] = 当前时间戳
--
-- 返回 {code, qty}
--   1  回补成功
--   2  幂等（已回补或已实扣）
--   0  无预占记录
-- ============================================================
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return {0, 0} end
if status ~= 'LOCKED' then return {2, 0} end

local qty = tonumber(redis.call('HGET', KEYS[1], 'num'))
redis.call('INCRBY', KEYS[2], qty)
redis.call('HSET', KEYS[1], 'status', 'RELEASED', 'releaseTs', ARGV[1])
-- 有货了，清掉售罄标记，否则该 SKU 会被一直快速拒绝
redis.call('DEL', KEYS[3])

return {1, qty}
