import json
import time

from fastapi.testclient import TestClient
import pytest

from reelpilot.api import create_app
from reelpilot.demo import make_demo
from reelpilot.journal import Journal
from reelpilot.media import probe
from reelpilot.pipeline import run_job
from reelpilot.retrieval import Retriever
from reelpilot.schema import Brief, explicit_spec


@pytest.fixture(scope="module")
def media_library(tmp_path_factory):
    return make_demo(tmp_path_factory.mktemp("real-media"))


def test_real_media_render_interrupt_resume_and_corruption_repair(media_library, tmp_path):
    journal = Journal(tmp_path/"jobs")
    spec = explicit_spec(Brief(text="forest;robot", budget_seconds=8))
    retriever = Retriever(media_library)
    with pytest.raises(RuntimeError, match="controlled interruption"):
        run_job(media_library, retriever, journal, "recovery", spec, mode="lexical", interrupt_after="render")
    assert journal.get("recovery")["status"] == "failed"
    result = run_job(media_library, retriever, journal, "recovery", spec, mode="lexical")
    assert result["status"] == "complete"
    assert abs(result["media"]["duration"]-8) < 0.25
    assert next(s for s in journal.get("recovery")["stages"] if s["stage"] == "render")["attempts"] == 1
    output = journal.root/"recovery/output.mp4"
    output.write_bytes(b"corrupt")
    run_job(media_library, retriever, journal, "recovery", spec, mode="lexical")
    assert probe(output)["width"] == 640
    assert next(s for s in journal.get("recovery")["stages"] if s["stage"] == "render")["attempts"] == 2


def test_api_source_seek_search_and_completed_output(media_library, tmp_path):
    app = create_app(media_library.root, tmp_path/"api-jobs")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["dense_configured"] is False
        response = client.post("/api/search", json={"query": "robot", "mode": "lexical"})
        assert response.json()["hits"][0]["segment"]["id"] == "demo_0001"
        assert client.get("/api/assets/demo", headers={"Range": "bytes=0-99"}).status_code == 206
        assert client.post("/api/search", json={"query": "robot", "mode": "hybrid"}).status_code == 422
        request = {"brief": {"text": "forest;robot", "budget_seconds": 8}, "job_id": "api-job"}
        assert client.post("/api/jobs", json=request).status_code == 202
        for _ in range(100):
            state = client.get("/api/jobs/api-job").json()
            if state["status"] in {"complete", "failed"}:
                break
            time.sleep(0.05)
        assert state["status"] == "complete", state
        assert client.get("/api/jobs/api-job/output").headers["content-type"] == "video/mp4"
        request["brief"]["text"] = "dragon"
        assert client.post("/api/jobs", json=request).status_code == 409


def test_final_manifest_is_fenced_and_recoverable(media_library,tmp_path,monkeypatch):
    from reelpilot.journal import BusyJob
    journal=Journal(tmp_path/'final-fence')
    spec=explicit_spec(Brief(text='forest;robot',budget_seconds=8))
    retriever=Retriever(media_library)
    publish=journal.publish
    def steal(job,owner,temporary,destination):
        if destination.name=='result.json':
            with journal.connect() as db:
                db.execute('UPDATE jobs SET owner=?,lease_until=? WHERE id=?',
                           ('successor',time.time()+60,job))
        return publish(job,owner,temporary,destination)
    monkeypatch.setattr(journal,'publish',steal)
    with pytest.raises(BusyJob):
        run_job(media_library,retriever,journal,'fence',spec,mode='lexical')
    assert not (journal.root/'fence/result.json').exists()
    assert journal.get('fence')['status']=='running'
    with journal.connect() as db:db.execute('UPDATE jobs SET lease_until=0 WHERE id=?',('fence',))
    monkeypatch.setattr(journal,'publish',publish)
    assert run_job(media_library,retriever,journal,'fence',spec,mode='lexical')['status']=='complete'
    assert next(s for s in journal.get('fence')['stages'] if s['stage']=='render')['attempts']==1
