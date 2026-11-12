import time
import weakref

# Atomic token bucket. KEYS[1]=state hash; ARGV: capacity, refill/sec, now (s), ttl (s), cost.
_LUA = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local cost = tonumber(ARGV[5])
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
if tokens >= cost - 1e-9 then
  tokens = tokens - cost
  ok = 1
end
if tokens < 0 then tokens = 0 end
if ts > now then now = ts end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], ttl)
return ok
"""

_KEY_TTL_S = 120

# One registered Script per redis client (keeps the SHA, avoids re-registering per call).
_scripts: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _script(redis):
    sc = _scripts.get(redis)
    if sc is None:
        sc = redis.register_script(_LUA)
        _scripts[redis] = sc
    return sc


async def acquire(
    redis, provider: str, per_minute: int, *, cost: int = 1, now: float | None = None
) -> bool:
    """Take `cost` tokens from the provider's bucket (capacity per_minute, refill
    per_minute/60 /s). All or nothing: a refused request consumes no tokens. A cost above
    the capacity is capped to it (a full bucket always admits one batch)."""
    if per_minute <= 0:
        return False
    cost = min(max(int(cost), 1), per_minute)
    script = _script(redis)
    ts = time.time() if now is None else now
    res = await script(
        keys=[f"rl:{provider}"], args=[per_minute, per_minute / 60, ts, _KEY_TTL_S, cost]
    )
    return int(res) == 1
