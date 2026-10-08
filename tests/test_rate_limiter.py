from spotify_queuer.rate_limiter import RateLimiter


def test_rate_limiter_allows_requests_within_budget():
    clock = [0.0]
    sleeps = []
    limiter = RateLimiter(
        max_requests=5, window_seconds=10, sleep_fn=lambda s: sleeps.append(s), time_fn=lambda: clock[0]
    )
    for _ in range(5):
        limiter.acquire()
    assert sleeps == []


def test_rate_limiter_blocks_when_budget_exceeded():
    clock = [0.0]
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    limiter = RateLimiter(max_requests=2, window_seconds=10, sleep_fn=fake_sleep, time_fn=lambda: clock[0])
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()  # 3rd request within the window must wait

    assert sleeps
    assert clock[0] >= 10
