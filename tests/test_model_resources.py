from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time

import numpy as np
import pytest

from reelpilot.control import ResourceBusy, ServiceDraining, TaskCancelled
from reelpilot.resources import FairGate, ModelResources, VectorCache
from reelpilot.schema import Brief, explicit_spec


def until(check):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("resource condition did not settle")


def test_same_key_singleflight_and_independent_return_buffers():
    cache = VectorCache()
    entered, release = Event(), Event()
    barrier = Barrier(8)
    calls = []

    def compute():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return np.array([1.0, 2.0])

    def request():
        barrier.wait(timeout=5)
        return cache.get("model", "same", compute)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request) for _ in range(8)]
        try:
            assert entered.wait(3)
            until(lambda: cache.stats()["coalesced"] == 7)
        finally:
            release.set()
        vectors = [f.result(timeout=5) for f in futures]
    assert len(calls) == 1 and cache.stats()["pending"] == 0
    vectors[0][0] = 99
    assert all(v[0] == 1 for v in vectors[1:])
    assert cache.get("model", "same", lambda: None)[0] == 1


def test_follower_cancellation_does_not_cancel_leader():
    cache = VectorCache()
    entered, release, cancel = Event(), Event(), Event()

    def compute():
        entered.set()
        assert release.wait(5)
        return [1, 2]

    def guard():
        if cancel.is_set():
            raise TaskCancelled("follower only")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(cache.get, "m", "q", compute)
        assert entered.wait(3)
        follower = pool.submit(cache.get, "m", "q", lambda: None, check=guard)
        try:
            until(lambda: cache.stats()["coalesced"] == 1)
            cancel.set()
            with pytest.raises(TaskCancelled):
                follower.result(timeout=3)
        finally:
            release.set()
        assert list(leader.result(timeout=3)) == [1, 2]
    assert cache.stats()["entries"] == 1


def test_cancelled_leader_is_replaced_by_live_follower():
    cache = VectorCache()
    entered, release, cancel = Event(), Event(), Event()
    calls = []

    def original():
        calls.append("old")
        entered.set()
        assert release.wait(5)
        return [1, 0]

    def next_compute():
        calls.append("new")
        return [0, 1]

    def old_guard():
        if cancel.is_set():
            raise TaskCancelled("old owner stopped")

    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(cache.get, "m", "q", original, check=old_guard)
        assert entered.wait(3)
        new = pool.submit(cache.get, "m", "q", next_compute)
        try:
            until(lambda: cache.stats()["coalesced"] == 1)
            cancel.set()
        finally:
            release.set()
        with pytest.raises(TaskCancelled):
            old.result(timeout=3)
        assert list(new.result(timeout=3)) == [0, 1]
    assert calls == ["old", "new"] and cache.stats()["pending"] == 0


def test_lru_byte_budget_namespace_and_copy_isolation():
    cache = VectorCache(max_entries=2, max_bytes=28)
    one = lambda: [1, 0]
    cache.get("model", "a", one)
    cache.get("model", "b", one)
    assert cache.stats()["accounted_bytes"] == 28
    cache.get("model", "a", lambda: None)  # a becomes most recently used.
    cache.get("model", "c", one)
    calls = []
    cache.get("model", "b", lambda: calls.append(True) or [0, 1])
    assert len(calls) == 1 and cache.stats()["entries"] == 2
    assert cache.stats()["accounted_bytes"] <= 28
    assert cache.stats()["evictions"] == 2
    assert list(cache.get("other", "b", lambda: [2, 3])) == [2, 3]
    assert cache.stats()["accounted_bytes"] <= 28


def test_oversize_values_are_shared_for_one_flight_but_not_retained():
    cache = VectorCache(max_bytes=4)
    assert list(cache.get("m", "q", lambda: [1, 2])) == [1, 2]
    assert cache.stats()["entries"] == 0 and cache.stats()["oversize_not_retained"] == 1
    cache.get("m", "q", lambda: [1, 2])
    assert cache.stats()["misses"] == 2


@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 1], [[1, 2]]])
def test_failed_fill_does_not_poison_retry(vector):
    cache = VectorCache()
    with pytest.raises(ValueError):
        cache.get("m", "q", lambda: vector)
    assert cache.stats()["pending"] == cache.stats()["entries"] == 0
    assert list(cache.get("m", "q", lambda: [1, 0])) == [1, 0]


def test_fill_capacity_rejects_distinct_keys_without_consuming_a_slot():
    cache = VectorCache(max_pending=1)
    entered, release = Event(), Event()

    def compute():
        entered.set()
        assert release.wait(5)
        return [1, 2]

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(cache.get, "m", "q", compute)
        try:
            assert entered.wait(3)
            with pytest.raises(ResourceBusy):
                cache.get("m", "other", lambda: [1, 2])
            assert cache.stats()["pending"] == 1
        finally:
            release.set()
        first.result(timeout=3)


def test_cache_hit_still_obeys_request_guard():
    cache = VectorCache()
    cache.get("m", "q", lambda: [1, 0])

    def cancelled():
        raise TaskCancelled("already cancelled")

    with pytest.raises(TaskCancelled):
        cache.get("m", "q", lambda: None, check=cancelled)


def test_fifo_gate_and_exception_release():
    gate = FairGate()
    order = []

    def use(i):
        with gate.lease():
            order.append(i)

    with ThreadPoolExecutor(max_workers=2) as pool:
        with gate.lease():
            a = pool.submit(use, 1)
            until(lambda: gate.stats()["waiting"] == 1)
            b = pool.submit(use, 2)
            until(lambda: gate.stats()["waiting"] == 2)
        a.result(timeout=3)
        b.result(timeout=3)
    assert order == [1, 2]
    with pytest.raises(ValueError):
        with gate.lease():
            raise ValueError("tool error")
    with gate.lease():
        pass
    assert gate.stats()["waiting"] == 0 and not gate.stats()["active"]


def test_gate_cancelled_head_does_not_block_successor():
    gate = FairGate(max_waiters=2)
    cancel = Event()
    order = []

    def guard():
        if cancel.is_set():
            raise TaskCancelled("head left")

    def use(check, i):
        with gate.lease(check):
            order.append(i)

    with ThreadPoolExecutor(max_workers=2) as pool:
        with gate.lease():
            a = pool.submit(use, guard, 1)
            until(lambda: gate.stats()["waiting"] == 1)
            b = pool.submit(use, lambda: None, 2)
            until(lambda: gate.stats()["waiting"] == 2)
            with pytest.raises(ResourceBusy):
                with gate.lease():
                    pass
            cancel.set()
            with pytest.raises(TaskCancelled):
                a.result(timeout=3)
            until(lambda: gate.stats()["waiting"] == 1)
        b.result(timeout=3)
    assert order == [2]


def test_gate_close_wakes_waiters_and_rejects_new_work():
    gate = FairGate()

    def use():
        with gate.lease():
            pass

    with ThreadPoolExecutor(max_workers=1) as pool:
        with gate.lease():
            waiter = pool.submit(use)
            until(lambda: gate.stats()["waiting"] == 1)
            gate.begin_close()
            with pytest.raises(ServiceDraining):
                waiter.result(timeout=3)
        with pytest.raises(ServiceDraining):
            use()
    assert not gate.stats()["active"] and gate.stats()["waiting"] == 0


def test_retained_planner_reuses_weights_but_not_answers():
    loads = []
    closed = []

    class Planner:
        def __init__(self):
            loads.append(True)

        def spec(self, brief, *, check):
            check()
            return explicit_spec(brief)

        def close(self):
            closed.append(True)

    resources = ModelResources(planner_factory=Planner, cache_planner=True)
    specs = [resources.plan(Brief(text="forest", budget_seconds=n)) for n in [3, 5, 7]]
    assert [s.budget_seconds for s in specs] == [3, 5, 7]
    assert len(loads) == 1 and resources.stats()["planner_reuses"] == 2
    resources.close()
    resources.close()
    assert len(closed) == 1


def test_shutdown_waits_for_active_encoding_and_cache_fill_before_close():
    entered, release, closed = Event(), Event(), Event()

    class Encoder:
        fingerprint = "m"

        def encode(self, *, text, check):
            entered.set()
            assert release.wait(5)
            return [1, 2]

        def close(self):
            closed.set()

    resources = ModelResources(encoder_factory=Encoder, fingerprint="m")
    with ThreadPoolExecutor(max_workers=2) as pool:
        inference = pool.submit(resources.query, "q")
        assert entered.wait(3)
        shutdown = pool.submit(resources.close)
        try:
            until(lambda: resources.stats()["draining"])
            assert not closed.is_set() and not shutdown.done()
        finally:
            release.set()
        with pytest.raises(ServiceDraining):
            inference.result(timeout=3)
        shutdown.result(timeout=3)
    assert closed.is_set() and resources.cache.stats()["pending"] == 0 and resources.closed


def test_retained_planner_survives_one_cancelled_request_without_reloading():
    loads = []

    class Planner:
        def __init__(self):
            loads.append(True)
            self.calls = 0

        def spec(self, brief, *, check):
            self.calls += 1
            if self.calls == 1:
                raise TaskCancelled("first request stopped")
            check()
            return explicit_spec(brief)

        def close(self):
            pass

    resources = ModelResources(planner_factory=Planner, cache_planner=True)
    with pytest.raises(TaskCancelled):
        resources.plan(Brief(text="forest"))
    assert resources.plan(Brief(text="robot")).goals[0].query == "robot"
    assert len(loads) == 1
    resources.close()
