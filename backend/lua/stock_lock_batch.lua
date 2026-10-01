-- ============================================================
-- stock_lock_batch.lua：一次下单的所有 SKU 整批预占
--
-- docs/10 §9 说的"整批原子"：**先检查全部，全部满足后再统一扣减**。
-- Redis 单线程执行脚本，所以不存在"前两个扣了、第三个不够"的部分成功。
-- 这个性质让下单主路径不必写补偿逻辑。
--
-- KEYS[1]       = stock:batch:{requestId}   批量预占记录（幂等依据 + 回补依据）
-- KEYS[2..n+1]  = 每个 SKU 选中的分片 key（调用方按 (whId, skuId) 排序并按 userId 选片）
-- ARGV[1]       = n（SKU 个数）
-- ARGV[2]       = 预占记录 TTL 秒
-- ARGV[3]       = 当前时间戳
-- ARGV[4..n+3]  = 每个 SKU 的数量，与 KEYS[2..] 一一对应
--
-- 返回 {code, idx, remain}
--   1  成功
--   2  幂等命中
--   0  第 idx 个 SKU 库存不足（remain 为该分片余量）
--  -1  第 idx 个 SKU 未初始化
-- ============================================================
local batchKey = KEYS[1]
local n = tonumber(ARGV[1])

-- ① 幂等：这张单已经预占过了
if redis.call('EXISTS', batchKey) == 1 then
    return {2, 0, 0}
end

-- ② 先检查全部。任何一个不足就整体失败，此时还没有任何写操作
for i = 1, n do
    local stock = redis.call('GET', KEYS[i + 1])
    if not stock then
        return {-1, i, 0}
    end
    if tonumber(stock) < tonumber(ARGV[i + 3]) then
        return {0, i, tonumber(stock)}
    end
end

-- ③ 全部满足，统一扣减并记录
for i = 1, n do
    local qty = tonumber(ARGV[i + 3])
    redis.call('DECRBY', KEYS[i + 1], qty)
    redis.call('HSET', batchKey, KEYS[i + 1], qty)
end
redis.call('HSET', batchKey, '__status', 'LOCKED', '__ts', ARGV[3])
redis.call('EXPIRE', batchKey, tonumber(ARGV[2]))

return {1, 0, 0}
