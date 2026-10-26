# When an agent finishes just before its supervisor checks ownership

This is an implementation case study from ReelPilot, a local video-editing agent. The reproduction uses SQLite and threads, without an LLM or GPU. The relevant regression is [test_supervisor_completion_race_uses_one_state_snapshot](../../tests/test_runtime_control.py).

## The symptom

A supervised worker can successfully publish its result while the supervising process is still polling. Completion clears the active lease. The original polling sequence was equivalent to:

```python
if ticket.completed_by_self():
    allow_worker_cleanup()
else:
    ticket.guard()
```

Each operation read the database separately. If completion committed between those reads, the first operation saw an active request and returned false. The second saw a complete request with no active lease and raised BusyJob. The result was valid, but the supervisor treated the worker as having lost ownership.

A Windows CI failure led to inspecting this boundary. The regression now forces the state transition at exactly that point rather than hoping a repeated run encounters it again. The earlier CI run is retained in [Actions](https://github.com/l422145205-source/reelpilot/actions/runs/40271783334); the [fixed snapshot](https://github.com/l422145205-source/reelpilot/actions/runs/40272436262) passed on Windows and Ubuntu.

## The correction

[Ticket.worker_state](../../src/reelpilot/admission.py) reads the owner, status, lease and control fields once. A matching committed completion is recognized from that row; otherwise the same row is validated by [guard_row](../../src/reelpilot/control.py).

```python
row = read_one_request_snapshot()
if owned_by_me(row) and row.status == "complete":
    return "complete"
validate_that_snapshot(row)
return "active"
```

This does not freeze the database for the worker's lifetime. An active snapshot may become complete immediately afterwards; the next poll observes it. Artifact commits still check current ownership inside their write transaction. The correction prevents a normal transition from being misclassified by two inconsistent observations.

Run the deterministic reproduction:

```bash
python -m pip install -e '.[dev]'
pytest tests/test_runtime_control.py::test_supervisor_completion_race_uses_one_state_snapshot -q
```

The test commits completion after fetching the observer's first row. It demonstrates the old sequence raising BusyJob, then the new sequence returning active followed by complete. Both cases end with a valid committed completion. It is a concrete ordering test, not a proof over every possible schedule.

## Why cancelling a Future is a different problem

A Future represents submitted work; cancelling a running Future does not forcibly stop its Python thread. ReelPilot separately handles persistent control intent, permission to commit results, and the lifetime of local tools. [Python's Future documentation](https://docs.python.org/3.13/library/concurrent.futures.html#concurrent.futures.Future.cancel) describes that execution distinction.

Cancellation is stored on the request. Stage and finalization guards check it before committing. The process backend can terminate a worker and its trusted FFmpeg tools. The thread backend checks between operations and, for local Transformers generation, at generation steps; it cannot interrupt a blocked kernel or an arbitrary remote service.

These layers also explain why a completed output and a cleanup error can coexist. A later cleanup error is recorded as postcommit_error; it does not retroactively change a published completion into cancellation.

## Another boundary: a file exists before completion commits

[Journal.finalize](../../src/reelpilot/journal.py) replaces the final manifest and updates both request and pipeline completion in one SQLite write transaction. The filesystem rename itself cannot be rolled back by SQLite. If the worker dies after rename but before SQL commit, the file may exist while the completion record is absent.

The API serves results only after the persistent completion decision. A real OS-kill experiment leaves the file present, verifies that download returns 409, then recovers from the verified render without committing a second render. The [fault lab](../runtime-fault-lab.md) explains the injection points and distinguishes real process death from controlled Python exceptions. This does not establish power-loss atomicity.

## Make the first reproduction inexpensive

For a model-free installation check:

```bash
reelpilot selfcheck
```

It generates original media and exercises interruption, replay and corruption repair in its own fresh directory. For real process-death, service-drain and restart scenarios:

```bash
python scripts/validate_resources.py
```

The [resource design](../model-resources.md) additionally covers bounded model admission, query single-flight, cancelled leaders and independent waiter controls. Each mechanism has a different contract; none should be summarized as “exactly once execution.”
