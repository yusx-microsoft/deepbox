"""Reusable, dependency-free security primitives for the deepbox server.

This module intentionally avoids any framework or global state so that it can
be unit tested in isolation and composed by callers (middleware, routes) as
they see fit. Everything here is either a pure function or a self-contained
object whose only mutable state is explicitly owned by the caller.

The three primitives provided are:

* :class:`RateLimiter` -- a bounded, thread-safe fixed-window rate limiter
  with an injectable monotonic clock, per-key/per-path classing, explicit
  reset, and ``Retry-After`` computation.
* :func:`is_origin_allowed` -- an origin allowlist predicate used to protect
  cookie/CSRF-sensitive unsafe HTTP methods.
* :func:`build_security_headers` -- a pure builder for baseline response
  security headers that only enables HSTS in production.

None of these hold process-global state; callers own their instances.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "SAFE_METHODS",
    "UNSAFE_METHODS",
    "RateLimitRule",
    "RateLimitDecision",
    "RateLimiter",
    "normalize_origin",
    "is_origin_allowed",
    "build_security_headers",
]

# HTTP methods that are considered "safe" and therefore do not mutate state.
# Cookie/CSRF origin checks only need to gate the complementary unsafe set.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RateLimitRule:
    """A fixed-window limit: at most ``limit`` requests per ``window_seconds``.

    ``limit`` must be >= 1 and ``window_seconds`` must be > 0.
    """

    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a single :meth:`RateLimiter.check` call."""

    allowed: bool
    limit: int
    remaining: int
    #: Whole seconds a client should wait before retrying. ``0`` when allowed.
    retry_after: int
    #: Monotonic timestamp at which the current window resets.
    reset_at: float


@dataclass
class _Window:
    start: float
    count: int


class RateLimiter:
    """Bounded, thread-safe fixed-window rate limiter.

    The limiter maps a *class key* (an arbitrary hashable, typically derived
    from a client identity and/or request path class) to a rolling fixed
    window. It is bounded: at most ``max_keys`` distinct keys are retained.
    When full, the key whose window resets soonest (i.e. the most stale) is
    evicted to make room, which favours keeping actively-limited keys.

    The clock is injectable via ``clock`` (defaults to
    :func:`time.monotonic`) so tests can advance time deterministically. A
    monotonic clock is required for correctness; wall-clock time must not be
    used because it can jump backwards.
    """

    def __init__(
        self,
        rule: RateLimitRule,
        *,
        max_keys: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        self._rule = rule
        self._max_keys = int(max_keys)
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: Dict[object, _Window] = {}

    @property
    def rule(self) -> RateLimitRule:
        return self._rule

    @property
    def max_keys(self) -> int:
        return self._max_keys

    def __len__(self) -> int:
        with self._lock:
            return len(self._windows)

    @staticmethod
    def classify(*parts: object) -> Tuple[object, ...]:
        """Build a stable, hashable class key from parts.

        This lets callers group requests by, e.g., ``(client_id, path_class)``
        without imposing a particular string format. ``None`` parts are kept
        so callers can distinguish "anonymous" from other identities.
        """

        return tuple(parts)

    def check(self, key: object, *, cost: int = 1) -> RateLimitDecision:
        """Attempt to consume ``cost`` units for ``key``.

        Returns a :class:`RateLimitDecision`. When not allowed, no units are
        consumed and ``retry_after`` reflects the wait until the window reset.
        """

        if cost < 1:
            raise ValueError("cost must be >= 1")
        now = self._clock()
        with self._lock:
            window = self._windows.get(key)
            if window is None or (now - window.start) >= self._rule.window_seconds:
                # Start a fresh window. Enforce the bound before insertion.
                if key not in self._windows and len(self._windows) >= self._max_keys:
                    self._evict_locked(now)
                window = _Window(start=now, count=0)
                self._windows[key] = window

            reset_at = window.start + self._rule.window_seconds
            if window.count + cost > self._rule.limit:
                retry_after = max(1, _ceil_positive(reset_at - now))
                remaining = max(0, self._rule.limit - window.count)
                return RateLimitDecision(
                    allowed=False,
                    limit=self._rule.limit,
                    remaining=remaining,
                    retry_after=retry_after,
                    reset_at=reset_at,
                )

            window.count += cost
            remaining = max(0, self._rule.limit - window.count)
            return RateLimitDecision(
                allowed=True,
                limit=self._rule.limit,
                remaining=remaining,
                retry_after=0,
                reset_at=reset_at,
            )

    def reset(self, key: Optional[object] = None) -> None:
        """Reset one key's window, or all windows when ``key`` is ``None``."""

        with self._lock:
            if key is None:
                self._windows.clear()
            else:
                self._windows.pop(key, None)

    def _evict_locked(self, now: float) -> None:
        # Drop expired windows first; they carry no useful state.
        expired = [
            k
            for k, w in self._windows.items()
            if (now - w.start) >= self._rule.window_seconds
        ]
        for k in expired:
            del self._windows[k]
        if len(self._windows) < self._max_keys:
            return
        # Still full: evict the window that resets soonest (most stale start).
        oldest = min(self._windows, key=lambda k: self._windows[k].start)
        del self._windows[oldest]


def _ceil_positive(value: float) -> int:
    """Ceil a non-negative float to an int without importing math.ceil edge
    cases; values <= 0 map to 0."""

    if value <= 0:
        return 0
    whole = int(value)
    return whole if whole == value else whole + 1


# ---------------------------------------------------------------------------
# Origin allowlist for cookie/CSRF-protected unsafe methods
# ---------------------------------------------------------------------------
def normalize_origin(origin: Optional[str]) -> Optional[str]:
    """Normalise an ``Origin`` value for comparison.

    Trims surrounding whitespace, strips a single trailing slash, and lowers
    the scheme+host portion is left untouched beyond stripping because origins
    are already case-normalised by browsers for scheme/host. Returns ``None``
    for missing/blank/opaque ("null") origins.
    """

    if origin is None:
        return None
    value = origin.strip()
    if not value or value.lower() == "null":
        return None
    return value.rstrip("/")


def is_origin_allowed(
    method: str,
    origin: Optional[str],
    allowed_origins: Iterable[str],
    *,
    require_origin: bool = True,
) -> bool:
    """Predicate protecting cookie/CSRF-sensitive *unsafe* methods.

    Safe methods (GET/HEAD/OPTIONS/TRACE) are always allowed since they must
    not mutate state. For unsafe methods the request ``Origin`` must be present
    (unless ``require_origin`` is ``False``) and must exactly match one of the
    normalised ``allowed_origins``. This is a pure function; it holds no state
    and does not consult the environment.
    """

    if method.upper() in SAFE_METHODS:
        return True

    allowed = {
        norm
        for norm in (normalize_origin(o) for o in allowed_origins)
        if norm is not None
    }

    normalized = normalize_origin(origin)
    if normalized is None:
        # No usable Origin header: fail closed unless explicitly relaxed and
        # there is no allowlist to enforce.
        return not require_origin and not allowed

    return normalized in allowed


# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------
def build_security_headers(
    *,
    production: bool,
    hsts_max_age: int = 63072000,
    include_subdomains: bool = True,
    preload: bool = False,
    frame_options: str = "DENY",
    referrer_policy: str = "no-referrer",
    content_type_options: str = "nosniff",
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build a baseline set of security response headers.

    Pure and deterministic given its arguments. HSTS
    (``Strict-Transport-Security``) is only emitted when ``production`` is
    true, because forcing HTTPS on local/plain-HTTP development would break it
    and can poison browser HSTS caches. ``extra`` headers are merged last and
    win, letting callers override or augment without mutating globals.
    """

    headers: Dict[str, str] = {
        "X-Content-Type-Options": content_type_options,
        "X-Frame-Options": frame_options,
        "Referrer-Policy": referrer_policy,
    }

    if production:
        if hsts_max_age < 0:
            raise ValueError("hsts_max_age must be >= 0")
        value = f"max-age={hsts_max_age}"
        if include_subdomains:
            value += "; includeSubDomains"
        if preload:
            value += "; preload"
        headers["Strict-Transport-Security"] = value

    if extra:
        headers.update(extra)

    return headers
