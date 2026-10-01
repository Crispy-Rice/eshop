-- ============================================================
-- stock_release_batch.lua：取消 / 超时 / 下单事务失败时回补整批预占
--
-- KEYS[1] = stock:batch:{requestId}
-- ARGV[1] = 当前时间戳
--
-- 返回 {code}
--   1  回补成功
--   2  幂等（已回补或已实扣）
--   0  无预占记录
--
-- 幂等的关键：回补依据是预占记录里的 __status，而不是调用方的判断。
-- 超时任务与用户主动取消同时触发，第二次也会看到 RELEASED 而直接返回 2。
--
-- ★ 注意：回补的分片 key 是从记录里 HGETALL 出来的，属于"脚本内动态 key"。
--   单实例 Redis 完全可用；将来切 Redis Cluster 时，动态 key 可能落在别的
--   slot 上而报 CROSSSLOT。届时要改为由调用方先 HGETALL、把分片 key 一并
--   放进 KEYS 传进来（docs/14 §3.3）。
-- ============================================================
local batchKey = KEYS[1]
local status = redis.call('HGET', batchKey, '__status')
if not status then
    return {0}
end
if status ~= 'LOCKED' then
    return {2}                         -- RELEASED 或 CONFIRMED，幂等
end

local fields = redis.call('HGETALL', batchKey)
for i = 1, #fields, 2 do
    local k = fields[i]
    if string.sub(k, 1, 2) ~= '__' then
        redis.call('INCRBY', k, tonumber(fields[i + 1]))
    end
end

redis.call('HSET', batchKey, '__status', 'RELEASED', '__releaseTs', ARGV[1])
-- 预占记录延长保留 1 天，便于排查
redis.call('EXPIRE', batchKey, 86400)

return {1}
