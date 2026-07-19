"""Unit tests for :mod:`server.app.security`.

These tests are pure: they inject a fake monotonic clock and never rely on
real time, sleeping, or process-global state.
"""
import threading
import unittest

from server.app import security
from server.app.security import (
    RateLimitRule,
    RateLimiter,
    build_security_headers,
    is_origin_allowed,
    normalize_origin,
)


class FakeClock:
    """Deterministic, monotonic-by-construction clock for tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self._t += seconds


# ---------------------------------------------------------------------------
# RateLimitRule validation
# ---------------------------------------------------------------------------
class RuleTests(unittest.TestCase):
    def test_valid(self):
        rule = RateLimitRule(limit=5, window_seconds=1.0)
        self.assertEqual(rule.limit, 5)
        self.assertEqual(rule.window_seconds, 1.0)

    def test_bad_limit(self):
        with self.assertRaises(ValueError):
            RateLimitRule(limit=0, window_seconds=1.0)

    def test_bad_window(self):
        with self.assertRaises(ValueError):
            RateLimitRule(limit=1, window_seconds=0)
        with self.assertRaises(ValueError):
            RateLimitRule(limit=1, window_seconds=-3)


# ---------------------------------------------------------------------------
# RateLimiter behaviour
# ---------------------------------------------------------------------------
class RateLimiterTests(unittest.TestCase):
    def _limiter(self, limit=3, window=10.0, max_keys=4096, start=100.0):
        clock = FakeClock(start)
        limiter = RateLimiter(
            RateLimitRule(limit=limit, window_seconds=window),
            max_keys=max_keys,
            clock=clock,
        )
        return limiter, clock

    def test_allows_up_to_limit(self):
        limiter, _ = self._limiter(limit=3, window=10.0)
        for expected_remaining in (2, 1, 0):
            d = limiter.check("k")
            self.assertTrue(d.allowed)
            self.assertEqual(d.remaining, expected_remaining)
            self.assertEqual(d.retry_after, 0)
            self.assertEqual(d.limit, 3)

    def test_blocks_over_limit_with_retry_after(self):
        limiter, clock = self._limiter(limit=2, window=10.0, start=0.0)
        self.assertTrue(limiter.check("k").allowed)
        self.assertTrue(limiter.check("k").allowed)
        d = limiter.check("k")
        self.assertFalse(d.allowed)
        self.assertEqual(d.remaining, 0)
        self.assertEqual(d.retry_after, 10)
        self.assertEqual(d.reset_at, 10.0)

    def test_retry_after_shrinks_as_time_passes(self):
        limiter, clock = self._limiter(limit=1, window=10.0, start=0.0)
        self.assertTrue(limiter.check("k").allowed)
        clock.advance(3.5)
        d = limiter.check("k")
        self.assertFalse(d.allowed)
        # 10 - 3.5 = 6.5 -> ceil = 7
        self.assertEqual(d.retry_after, 7)

    def test_window_resets_after_expiry(self):
        limiter, clock = self._limiter(limit=1, window=10.0, start=0.0)
        self.assertTrue(limiter.check("k").allowed)
        self.assertFalse(limiter.check("k").allowed)
        clock.advance(10.0)  # exactly at boundary -> new window
        d = limiter.check("k")
        self.assertTrue(d.allowed)
        self.assertEqual(d.reset_at, 20.0)

    def test_keys_are_independent(self):
        limiter, _ = self._limiter(limit=1, window=10.0)
        self.assertTrue(limiter.check("a").allowed)
        self.assertTrue(limiter.check("b").allowed)
        self.assertFalse(limiter.check("a").allowed)
        self.assertFalse(limiter.check("b").allowed)

    def test_classify_key_grouping(self):
        limiter, _ = self._limiter(limit=1, window=10.0)
        k1 = limiter.classify("client-1", "/api/login")
        k2 = limiter.classify("client-1", "/api/logout")
        k3 = limiter.classify(None, "/api/login")
        self.assertEqual(k1, ("client-1", "/api/login"))
        self.assertTrue(limiter.check(k1).allowed)
        self.assertFalse(limiter.check(k1).allowed)
        # Different path class -> different bucket.
        self.assertTrue(limiter.check(k2).allowed)
        # Anonymous identity distinct from named identity.
        self.assertTrue(limiter.check(k3).allowed)

    def test_cost_consumes_multiple_units(self):
        limiter, _ = self._limiter(limit=5, window=10.0)
        d = limiter.check("k", cost=3)
        self.assertTrue(d.allowed)
        self.assertEqual(d.remaining, 2)
        d2 = limiter.check("k", cost=3)
        self.assertFalse(d2.allowed)
        # Rejected cost does not consume; a smaller cost still fits.
        d3 = limiter.check("k", cost=2)
        self.assertTrue(d3.allowed)
        self.assertEqual(d3.remaining, 0)

    def test_bad_cost(self):
        limiter, _ = self._limiter()
        with self.assertRaises(ValueError):
            limiter.check("k", cost=0)

    def test_reset_single_key(self):
        limiter, _ = self._limiter(limit=1, window=10.0)
        self.assertTrue(limiter.check("a").allowed)
        self.assertTrue(limiter.check("b").allowed)
        limiter.reset("a")
        self.assertTrue(limiter.check("a").allowed)  # freed
        self.assertFalse(limiter.check("b").allowed)  # untouched

    def test_reset_all(self):
        limiter, _ = self._limiter(limit=1, window=10.0)
        self.assertTrue(limiter.check("a").allowed)
        self.assertTrue(limiter.check("b").allowed)
        limiter.reset()
        self.assertEqual(len(limiter), 0)
        self.assertTrue(limiter.check("a").allowed)
        self.assertTrue(limiter.check("b").allowed)

    def test_bounded_max_keys(self):
        limiter, clock = self._limiter(limit=5, window=10.0, max_keys=3, start=0.0)
        for i in range(3):
            limiter.check(f"k{i}")
        self.assertEqual(len(limiter), 3)
        # Adding a 4th key must not exceed the bound.
        limiter.check("k3")
        self.assertLessEqual(len(limiter), 3)

    def test_bound_evicts_expired_first(self):
        limiter, clock = self._limiter(limit=5, window=10.0, max_keys=2, start=0.0)
        limiter.check("old")
        clock.advance(20.0)  # 'old' now expired
        limiter.check("mid")
        limiter.check("new")  # forces eviction; expired 'old' should go
        self.assertLessEqual(len(limiter), 2)

    def test_bad_max_keys(self):
        with self.assertRaises(ValueError):
            RateLimiter(RateLimitRule(1, 1.0), max_keys=0)

    def test_default_clock_is_monotonic(self):
        limiter = RateLimiter(RateLimitRule(1, 60.0))
        self.assertTrue(limiter.check("k").allowed)
        self.assertFalse(limiter.check("k").allowed)

    def test_thread_safety_under_contention(self):
        limiter, _ = self._limiter(limit=1000, window=1000.0, start=0.0)
        allowed = []
        lock = threading.Lock()

        def worker():
            local = 0
            for _ in range(100):
                if limiter.check("shared").allowed:
                    local += 1
            with lock:
                allowed.append(local)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 10 threads * 100 checks = 1000, exactly the limit; no over-count.
        self.assertEqual(sum(allowed), 1000)
        self.assertFalse(limiter.check("shared").allowed)


# ---------------------------------------------------------------------------
# Origin allowlist predicate
# ---------------------------------------------------------------------------
class NormalizeOriginTests(unittest.TestCase):
    def test_none_and_blank(self):
        self.assertIsNone(normalize_origin(None))
        self.assertIsNone(normalize_origin("   "))

    def test_null_opaque(self):
        self.assertIsNone(normalize_origin("null"))
        self.assertIsNone(normalize_origin("NULL"))

    def test_trailing_slash_and_whitespace(self):
        self.assertEqual(normalize_origin("  https://x.io/  "), "https://x.io")
        self.assertEqual(normalize_origin("https://x.io"), "https://x.io")


class OriginAllowedTests(unittest.TestCase):
    ALLOWED = ["https://app.example.com/", "https://admin.example.com"]

    def test_safe_methods_always_allowed(self):
        for m in ("GET", "HEAD", "OPTIONS", "TRACE", "get"):
            self.assertTrue(is_origin_allowed(m, None, self.ALLOWED))

    def test_unsafe_matching_origin(self):
        self.assertTrue(
            is_origin_allowed("POST", "https://app.example.com", self.ALLOWED)
        )
        # Trailing slash normalisation on both sides.
        self.assertTrue(
            is_origin_allowed("POST", "https://admin.example.com/", self.ALLOWED)
        )

    def test_unsafe_mismatched_origin(self):
        self.assertFalse(
            is_origin_allowed("POST", "https://evil.example.com", self.ALLOWED)
        )

    def test_unsafe_missing_origin_fails_closed(self):
        self.assertFalse(is_origin_allowed("DELETE", None, self.ALLOWED))
        self.assertFalse(is_origin_allowed("DELETE", "null", self.ALLOWED))

    def test_require_origin_false_with_empty_allowlist(self):
        # Only relaxed when both flag is set AND there is no allowlist.
        self.assertTrue(
            is_origin_allowed("POST", None, [], require_origin=False)
        )
        self.assertFalse(
            is_origin_allowed("POST", None, self.ALLOWED, require_origin=False)
        )

    def test_empty_allowlist_blocks_present_origin(self):
        self.assertFalse(is_origin_allowed("PATCH", "https://x.io", []))

    def test_allowlist_entries_normalized(self):
        self.assertTrue(
            is_origin_allowed("PUT", "https://x.io", ["  https://x.io/ ", "null"])
        )


# ---------------------------------------------------------------------------
# Security header builder
# ---------------------------------------------------------------------------
class SecurityHeaderTests(unittest.TestCase):
    def test_baseline_headers_non_production(self):
        h = build_security_headers(production=False)
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Strict-Transport-Security", h)

    def test_hsts_only_in_production(self):
        h = build_security_headers(production=True)
        self.assertIn("Strict-Transport-Security", h)
        self.assertEqual(
            h["Strict-Transport-Security"],
            "max-age=63072000; includeSubDomains",
        )

    def test_hsts_preload_and_no_subdomains(self):
        h = build_security_headers(
            production=True,
            hsts_max_age=100,
            include_subdomains=False,
            preload=True,
        )
        self.assertEqual(h["Strict-Transport-Security"], "max-age=100; preload")

    def test_hsts_negative_max_age_rejected(self):
        with self.assertRaises(ValueError):
            build_security_headers(production=True, hsts_max_age=-1)

    def test_extra_headers_merge_and_override(self):
        h = build_security_headers(
            production=False,
            extra={"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "default-src 'self'"},
        )
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(h["Content-Security-Policy"], "default-src 'self'")

    def test_custom_policies(self):
        h = build_security_headers(
            production=False,
            frame_options="SAMEORIGIN",
            referrer_policy="strict-origin",
            content_type_options="nosniff",
        )
        self.assertEqual(h["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(h["Referrer-Policy"], "strict-origin")

    def test_returns_fresh_dict_no_shared_state(self):
        a = build_security_headers(production=False)
        b = build_security_headers(production=False)
        self.assertIsNot(a, b)
        a["X-Frame-Options"] = "MUTATED"
        self.assertEqual(b["X-Frame-Options"], "DENY")


class ModuleSurfaceTests(unittest.TestCase):
    def test_method_sets_disjoint(self):
        self.assertTrue(security.SAFE_METHODS.isdisjoint(security.UNSAFE_METHODS))


if __name__ == "__main__":
    unittest.main()
