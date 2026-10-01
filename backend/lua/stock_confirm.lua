-- ============================================================
-- stock_confirm.lua：支付成功后预占转实扣
--
-- 库存数在**预占时就已经扣过了**，这一步只是把记录状态从 LOCKED 改成
-- CONFIRMED，防止之后被误当成"未支付"而回补。
--
-- KEYS[1] = stock:lock:{orderSubNo}:{skuId}  或  stock:batch:{requestId}
-- ARGV[1] = 状态字段名（单 SKU 记录为 'status'，批量记录为 '__status'）
--
-- 返回 {code}
--   1  成功
--   2  幂等（已是 CONFIRMED）
--   0  无记录
--  -1  已回补 —— **时序错乱，需要告警**
--
-- 关于 -1：支付成功时发现 Redis 预占已被回补（比如超时关单与晚到的支付
-- 同时发生）。DB 才是账本，此时走 docs/09 §7 的晚付处理，
-- Redis 的计数交给对账任务按 DB 修正。
-- ============================================================
local st = redis.call('HGET', KEYS[1], ARGV[1])
if not st then return {0} end
if st == 'CONFIRMED' then return {2} end
if st == 'RELEASED' then return {-1} end

redis.call('HSET', KEYS[1], ARGV[1], 'CONFIRMED')

return {1}
