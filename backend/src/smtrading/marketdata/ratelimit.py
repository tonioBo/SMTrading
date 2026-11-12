import time

# Atomic token bucket. KEYS[1]=state hash; ARGV: capacity, refill/sec, now (s), ttl (s).
_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local st = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(st[1])
local ts = tonumber(st[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
local ok = 0
if tokens >= 1 then
  tokens = tokens - 1
  ok = 1
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return ok
"""

_KEY_TTL_S = 120


async def acquire(redis, provider: str, per_minute: int, *, now: float | None = None) -> bool:
    """Take one token from the provider's bucket (capacity per_minute, refill per_minute/60 /s)."""
    if per_minute <= 0:
        return False
    script = redis.register_script(_LUA)
    ts = time.time() if now is None else now
    res = await script(
        keys=[f"rl:{provider}"], args=[per_minute, per_minute / 60, ts, _KEY_TTL_S]
    )
    return int(res) == 1
