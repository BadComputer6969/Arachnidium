"""Tests for SpeculativeCache: bounded LRU, expiry, consume-once."""

import asyncio

import addon


async def _completed(value="ok"):
    return value


def _task(value="ok"):
    async def _coro():
        await asyncio.sleep(0)
        return value

    return asyncio.ensure_future(_coro())


async def test_put_get_pop_lru_order():
    c = addon.SpeculativeCache(max_entries=3, ttl=60)
    c.put("u1", _task(1))
    c.put("u2", _task(2))
    c.put("u3", _task(3))
    assert len(c) == 3
    # touch u1 -> u2 becomes LRU
    assert c.get("u1") is not None
    c.put("u4", _task(4))
    assert "u2" not in c
    assert "u1" in c and "u4" in c


async def test_pop_consumes_once():
    c = addon.SpeculativeCache(max_entries=3, ttl=60)
    c.put("u1", _task("v"))
    entry = c.pop("u1")
    assert entry is not None
    assert await entry.task == "v"
    assert c.pop("u1") is None
    assert "u1" not in c


async def test_eviction_cancels_task():
    c = addon.SpeculativeCache(max_entries=2, ttl=60)

    async def _never():
        await asyncio.sleep(3600)

    t = asyncio.ensure_future(_never())
    c.put("u1", t)
    c.put("u2", _task())
    c.put("u3", _task())  # evicts u1
    assert "u1" not in c
    await asyncio.sleep(0)
    assert t.cancelled()


async def test_expired_entries_are_dropped():
    c = addon.SpeculativeCache(max_entries=10, ttl=0.05)
    c.put("u1", _task("v"))
    assert "u1" in c
    await asyncio.sleep(0.08)
    assert "u1" not in c
    assert c.pop("u1") is None


async def test_evict_expired():
    c = addon.SpeculativeCache(max_entries=10, ttl=0.05)
    c.put("u1", _task())
    c.put("u2", _task())
    await asyncio.sleep(0.08)
    c.evict_expired()
    assert len(c) == 0


async def test_clear_cancels_pending():
    c = addon.SpeculativeCache(max_entries=10, ttl=60)

    async def _never():
        await asyncio.sleep(3600)

    tasks = [asyncio.ensure_future(_never()) for _ in range(3)]
    for i, t in enumerate(tasks):
        c.put(f"u{i}", t)
    c.clear()
    assert len(c) == 0
    await asyncio.sleep(0)
    assert all(t.cancelled() for t in tasks)


async def test_put_replaces_existing_url():
    c = addon.SpeculativeCache(max_entries=10, ttl=60)
    t1 = _task("old")
    t2 = _task("new")
    c.put("u1", t1)
    c.put("u1", t2)
    assert len(c) == 1
    entry = c.pop("u1")
    assert await entry.task == "new"


async def test_max_entries_bound_with_many_puts():
    c = addon.SpeculativeCache(max_entries=100, ttl=60)
    for i in range(500):
        c.put(f"u{i}", _task(i))
    assert len(c) == 100
    # only the 100 most recent survive
    assert "u499" in c
    assert "u0" not in c
    c.clear()
