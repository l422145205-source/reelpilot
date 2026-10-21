from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from reelpilot.journal import BusyJob, Journal
from reelpilot.library import digest


def test_lease_allows_only_one_concurrent_owner(tmp_path):
    journal = Journal(tmp_path)
    journal.create("job", {"x": 1})
    def claim(_):
        try:
            return journal.claim("job")
        except BusyJob:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        owners = list(pool.map(claim, range(6)))
    assert sum(o is not None for o in owners) == 1


def test_expired_worker_cannot_publish_or_commit(tmp_path):
    journal = Journal(tmp_path)
    journal.create("job", {})
    old = journal.claim("job")
    with journal.connect() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id='job'")
    new = journal.claim("job")
    temporary = tmp_path/"old.partial"
    temporary.write_text("stale output")
    with pytest.raises(BusyJob):
        journal.publish("job", old, temporary, tmp_path/"output")
    with pytest.raises(BusyJob):
        journal.stage("job", old, "work", {}, lambda: {})
    assert new != old and not (tmp_path/"output").exists()
    with pytest.raises(BusyJob):
        journal.renew("job", old)


def test_idempotency_key_cannot_be_reused_for_another_brief(tmp_path):
    journal = Journal(tmp_path)
    journal.reserve_request("job", {"brief": "forest"})
    journal.reserve_request("job", {"brief": "forest"})
    with pytest.raises(ValueError):
        journal.reserve_request("job", {"brief": "robot"})
    journal.create("job", {"spec": "forest"})
    with pytest.raises(ValueError):
        journal.create("job", {"spec": "robot"})


def test_cache_rechecks_artifact_content_not_just_filename(tmp_path):
    journal = Journal(tmp_path)
    journal.create("job", {})
    owner = journal.claim("job")
    calls = []
    def build():
        calls.append(1)
        path = tmp_path/"artifact.txt"
        path.write_text("verified")
        return {"artifacts": {"artifact.txt": digest(path)}}
    journal.stage("job", owner, "render", {}, build)
    journal.stage("job", owner, "render", {}, build)
    assert len(calls) == 1
    (tmp_path/"artifact.txt").write_text("tampered")
    journal.stage("job", owner, "render", {}, build)
    assert len(calls) == 2


@pytest.mark.parametrize("job_id", ["../secret", "a/b", "", "x"*97])
def test_job_ids_cannot_be_filesystem_paths(tmp_path, job_id):
    with pytest.raises(ValueError):
        Journal(tmp_path).create(job_id, {})

