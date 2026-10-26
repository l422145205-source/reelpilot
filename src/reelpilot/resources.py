"""FIFO model admission and bounded, copy-safe single-flight query vectors.

These are per-process resource controls, not a distributed GPU scheduler or a
hard limit on CUDA/RSS memory. Cache byte accounting covers keys and arrays.
"""

from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Event, Lock
import time

import numpy as np

from .control import BusyJob, DeadlineExceeded, ResourceBusy, ServiceDraining, TaskCancelled

CONTROLS = (BusyJob, DeadlineExceeded, ServiceDraining, TaskCancelled)


class FairGate:
    def __init__(self, max_waiters=16):
        if isinstance(max_waiters, bool) or not isinstance(max_waiters, int) or max_waiters < 1:
            raise ValueError("max_waiters must be positive")
        self.limit = max_waiters
        self.cv = Condition()
        self.queue = deque()
        self.active = False
        self.closed = False
        self.grants = 0
        self.rejected = 0
        self.wait_seconds = 0.0

    @contextmanager
    def lease(self, check=lambda: None):
        token = object()
        granted = False
        start = time.monotonic()
        check()
        with self.cv:
            if self.closed:
                raise ServiceDraining("model resource is draining")
            if len(self.queue) >= self.limit:
                self.rejected += 1
                raise ResourceBusy("model waiting capacity reached")
            self.queue.append(token)
        try:
            while True:
                # Never hold the condition while executing a user/request guard:
                # a SQLite check or callback may block or raise.
                check()
                with self.cv:
                    if self.closed:
                        raise ServiceDraining("model resource is draining")
                    if not self.active and self.queue[0] is token:
                        self.queue.popleft()
                        self.active = True
                        granted = True
                        self.grants += 1
                        self.wait_seconds += time.monotonic() - start
                        break
                    self.cv.wait(0.02)
            check()
            yield
        finally:
            with self.cv:
                if granted:
                    self.active = False
                else:
                    self.queue.remove(token)
                self.cv.notify_all()

    def begin_close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def wait_idle(self):
        with self.cv:
            while self.active:
                self.cv.wait(0.02)

    def stats(self):
        with self.cv:
            return {
                "active": self.active,
                "waiting": len(self.queue),
                "max_waiters": self.limit,
                "grants": self.grants,
                "rejected": self.rejected,
                "wait_seconds": self.wait_seconds,
                "closed": self.closed,
            }


@dataclass
class Flight:
    done: Event = field(default_factory=Event)
    value: np.ndarray | None = None
    error: BaseException | None = None


class VectorCache:
    def __init__(self, *, max_entries=256, max_bytes=16 * 1024 * 1024, max_pending=16):
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1
            for v in [max_entries, max_bytes, max_pending]
        ):
            raise ValueError("cache limits must be positive integers")
        self.max_entries, self.max_bytes, self.max_pending = max_entries, max_bytes, max_pending
        self.lock = Lock()
        self.values = OrderedDict()
        self.pending = {}
        self.bytes = 0
        self.idle = Condition(self.lock)
        self.hits = self.misses = self.coalesced = self.evictions = self.bypassed = 0

    def get(self, namespace, query, compute, *, check=lambda: None):
        if not isinstance(namespace, str) or not isinstance(query, str):
            raise TypeError("cache keys must be strings")
        key = (namespace, query)
        key_bytes = len(namespace.encode()) + len(query.encode())
        if key_bytes > 4096:
            raise ValueError("query cache key too large")
        while True:
            check()
            with self.lock:
                if key in self.values:
                    self.hits += 1
                    value, cost = self.values.pop(key)
                    self.values[key] = (value, cost)
                    return value.copy()
                flight = self.pending.get(key)
                leader = flight is None
                if leader:
                    if len(self.pending) >= self.max_pending:
                        raise ResourceBusy("query fill capacity reached")
                    flight = self.pending[key] = Flight()
                    self.misses += 1
                else:
                    self.coalesced += 1
            if not leader:
                while not flight.done.wait(0.02):
                    check()
                check()
                if isinstance(flight.error, CONTROLS):
                    # A cancelled leader does not own another request's fate.
                    # A live follower can become the next leader, subject to its guard.
                    continue
                if flight.error is not None:
                    raise flight.error
                return flight.value.copy()
            try:
                check()
                value = np.asarray(compute(), dtype=np.float32)
                if (
                    value.ndim != 1
                    or not len(value)
                    or not np.isfinite(value).all()
                    or np.linalg.norm(value) <= 0
                ):
                    raise ValueError("invalid query vector")
                value = value.copy()
                value.flags.writeable = False
                check()
                with self.lock:
                    cost = key_bytes + value.nbytes
                    if cost <= self.max_bytes:
                        while self.values and (
                            len(self.values) >= self.max_entries or self.bytes + cost > self.max_bytes
                        ):
                            _, (_, old_cost) = self.values.popitem(last=False)
                            self.bytes -= old_cost
                            self.evictions += 1
                        self.values[key] = (value, cost)
                        self.bytes += cost
                    else:
                        self.bypassed += 1
                    flight.value = value
                return value.copy()
            except BaseException as exc:
                flight.error = exc
                raise
            finally:
                with self.lock:
                    del self.pending[key]
                    flight.done.set()
                    self.idle.notify_all()

    def wait_idle(self):
        with self.idle:
            while self.pending:
                self.idle.wait(0.02)

    def clear(self):
        with self.lock:
            if self.pending:
                raise RuntimeError("cannot clear cache while fills are active")
            self.values.clear()
            self.bytes = 0

    def stats(self):
        with self.lock:
            return {
                "entries": len(self.values),
                "accounted_bytes": self.bytes,
                "pending": len(self.pending),
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "max_pending": self.max_pending,
                "hits": self.hits,
                "misses": self.misses,
                "coalesced": self.coalesced,
                "evictions": self.evictions,
                "oversize_not_retained": self.bypassed,
            }


class ModelResources:
    def __init__(
        self,
        *,
        encoder_factory=None,
        fingerprint=None,
        planner_factory=None,
        planner_call=None,
        cache_planner=False,
        stop=None,
        max_waiters=16,
        cache=None
    ):
        self.stop = stop if stop is not None else Event()
        self.gate = FairGate(max_waiters)
        self.cache = cache if cache is not None else VectorCache(max_pending=max_waiters)
        self.encoder_factory = encoder_factory
        self.fingerprint = fingerprint
        self.planner_factory = planner_factory
        self.planner_call = planner_call
        self.cache_planner = cache_planner
        self.encoder = self.planner = None
        self.encoder_loads = self.planner_loads = self.planner_reuses = self.planner_requests = 0
        self.closed = False
        self.close_lock = Lock()

    def guard(self):
        if self.stop.is_set():
            raise ServiceDraining("service shutdown; retry original request after restart")

    def checked(self, check):
        def guard():
            self.guard()
            if check is not None:
                check()

        return guard

    def query(self, query, *, check=None):
        guard = self.checked(check)
        if self.encoder_factory is None:
            raise ValueError("no query encoding model configured")

        def compute():
            with self.gate.lease(guard):
                if self.encoder is None:
                    self.encoder = self.encoder_factory()
                    self.encoder_loads += 1
                guard()
                if self.encoder.fingerprint != self.fingerprint:
                    raise ValueError("loaded encoder identity changed")
                value = self.encoder.encode(text=query, check=guard)
                guard()
                return value

        return self.cache.get(self.fingerprint, query, compute, check=guard)

    def proxy(self, check=None):
        manager = self

        class QueryEncoder:
            fingerprint = manager.fingerprint

            def __call__(self, query):
                return manager.query(query, check=check)

        return QueryEncoder()

    def plan(self, brief, *, check=None):
        guard = self.checked(check)
        with self.gate.lease(guard):
            self.planner_requests += 1
            if not self.cache_planner:
                if self.planner_call is None:
                    raise ValueError("no planning model configured")
                result = self.planner_call(brief, check=guard)
                guard()
                return result
            if self.planner_factory is None:
                raise ValueError("no planning model configured")
            if self.planner is None:
                self.planner = self.planner_factory()
                self.planner_loads += 1
            else:
                self.planner_reuses += 1
            guard()
            result = self.planner.spec(brief, check=guard)
            guard()
            return result

    def begin_shutdown(self):
        self.stop.set()
        self.gate.begin_close()

    def close(self):
        with self.close_lock:
            self.begin_shutdown()
            self.gate.wait_idle()
            self.cache.wait_idle()
            if self.closed:
                return
            errors = []
            for model in [self.encoder, self.planner]:
                if model is not None:
                    try:
                        model.close()
                    except Exception as exc:
                        errors.append(exc)
            if errors:
                raise RuntimeError("model resource cleanup failed") from errors[0]
            self.cache.clear()
            self.encoder = self.planner = None
            self.closed = True

    def stats(self):
        return {
            "draining": self.stop.is_set(),
            "planner_retention": self.cache_planner,
            "encoder_loads": self.encoder_loads,
            "planner_loads": self.planner_loads,
            "planner_reuses": self.planner_reuses,
            "planner_requests": self.planner_requests,
            "gate": self.gate.stats(),
            "query_cache": self.cache.stats(),
        }
