from southcity_assistant.app import RateLimiter


def test_window_per_client():
    limiter = RateLimiter(per_minute=2)
    assert [limiter.allow("ip:a") for _ in range(3)] == [True, True, False]
    assert limiter.allow("ip:b")


def test_hard_capacity_and_amortized_expiry(monkeypatch):
    """Bounded table, no full scan on every request."""
    now = [1000.0]
    monkeypatch.setattr("southcity_assistant.app.time.monotonic", lambda: now[0])
    limiter = RateLimiter(per_minute=5)
    limiter.MAX_CLIENTS = 3
    assert all(limiter.allow(f"ip:{i}") for i in range(3))
    assert not limiter.allow("ip:new")  # table full of active clients
    assert limiter.allow("ip:0")  # known clients keep working
    now[0] += 61  # everyone idle; next call sweeps once
    assert limiter.allow("ip:new")
    assert set(limiter.hits) == {"ip:new"}
