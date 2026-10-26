"""Shared request controls, evaluated inside the artifact commit transaction."""

from __future__ import annotations

import time


class BusyJob(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    pass


class DeadlineExceeded(TimeoutError):
    pass


class ProcessCleanupError(RuntimeError):
    """Termination could not be confirmed; do not acknowledge successful cancellation."""


def guard_request(db, job, owner, *, now=None):
    if owner is None:
        return
    now = time.time() if now is None else now
    row = db.execute(
        "SELECT owner,lease_until,status,cancel_requested,cancel_at,deadline " "FROM submissions WHERE job=?",
        (job,),
    ).fetchone()
    if (
        not row
        or row["owner"] != owner
        or row["lease_until"] < now
        or row["status"] not in ("queued", "running")
    ):
        raise BusyJob("request ownership expired or was replaced")
    error = requested_error(row, now)
    if error is not None:
        raise error


def requested_error(row, now):
    """Same control ordering for live guards and expired receipt inspection."""
    # The first control event wins when cancellation and deadline race.
    if row["cancel_requested"] and (row["deadline"] is None or row["cancel_at"] <= row["deadline"]):
        return TaskCancelled("cancellation requested")
    if row["deadline"] is not None and now >= row["deadline"]:
        return DeadlineExceeded("request deadline exceeded")
    if row["cancel_requested"]:
        return TaskCancelled("cancellation requested")
    return None


def terminal_status(error):
    if isinstance(error, TaskCancelled):
        return "cancelled"
    if isinstance(error, DeadlineExceeded):
        return "timed_out"
    return "failed" if error else "complete"
