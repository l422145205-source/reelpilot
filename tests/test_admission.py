from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
import time

from fastapi.testclient import TestClient
import pytest

from reelpilot.admission import Admission, QueueFull
from reelpilot.api import create_app
from reelpilot.journal import BusyJob, Journal


def expire(journal, job):
    with journal.connect() as db:
        db.execute("UPDATE submissions SET lease_until=0 WHERE job=?", (job,))


def wait_terminal(client, job):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        data = client.get('/api/jobs/'+job).json()
        if data['status'] in ('complete', 'failed', 'interrupted'):
            return data
        time.sleep(.01)
    raise AssertionError('background worker did not settle')


def test_failure_receipt_survives_new_admission_instance(tmp_path):
    j=Journal(tmp_path);q=Admission(j)
    t=q.reserve('failure', {'brief': 'original'})
    t.phase('planning');t.finish(error='bad model JSON')
    reopened=Admission(Journal(tmp_path)).get('failure')
    assert reopened['status']=='failed' and reopened['error']=='bad model JSON'
    assert 'owner' not in reopened and 'lease_until' not in reopened
    with pytest.raises(KeyError):j.get('failure')


def test_idempotency_conflict_and_capacity_leave_no_rejected_receipt(tmp_path):
    j=Journal(tmp_path);q=Admission(j,capacity=1)
    first=q.reserve('one', {'text':'same'})
    with pytest.raises(BusyJob):q.reserve('one', {'text':'same'})
    with pytest.raises(ValueError):q.reserve('one', {'text':'changed'})
    with pytest.raises(QueueFull):q.reserve('rejected', {'text':'x'})
    with j.connect() as db:
        assert db.execute('SELECT count(*) FROM requests WHERE job=?',('rejected',)).fetchone()[0]==0
    first.finish(error='controlled')
    retry=q.reserve('one', {'text':'same'})
    assert q.get('one')['attempts']==2
    retry.finish()


def test_expiry_reclaims_capacity_and_fences_old_owner(tmp_path):
    j=Journal(tmp_path);q=Admission(j,capacity=1)
    old=q.reserve('old', {'text':'same'})
    expire(j,'old')
    assert q.get('old')['status']=='interrupted'
    replacement=q.reserve('old', {'text':'same'})
    for operation in [old.guard,old.renew,old.finish,lambda:old.phase('executing')]:
        with pytest.raises(BusyJob):operation()
    assert q.get('old')['status']=='queued'
    replacement.finish()


def test_admission_is_atomic_across_instances(tmp_path):
    j=Journal(tmp_path);a=Admission(j,capacity=3);b=Admission(Journal(tmp_path),capacity=3)
    barrier=Barrier(8)
    def request(i):
        barrier.wait(timeout=5)
        try:return (a if i%2 else b).reserve('job-'+str(i), {'i':i})
        except QueueFull:return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        accepted=[t for t in pool.map(request,range(8)) if t is not None]
    assert len(accepted)==3
    for t in accepted:t.finish()


@pytest.mark.parametrize('kwargs',[{'capacity':0},{'ttl':0},{'ttl':1,'interval':2}])
def test_invalid_queue_configuration(tmp_path,kwargs):
    with pytest.raises(ValueError):Admission(Journal(tmp_path),**kwargs)


def test_no_request_can_take_over_cli_job(tmp_path):
    j=Journal(tmp_path);j.create('cli',{'input':'from CLI'})
    with pytest.raises(ValueError,match='without an API receipt'):
        Admission(j).reserve('cli',{'text':'new request'})


def test_planning_failure_is_visible_after_app_restart(library,tmp_path):
    jobs=tmp_path/'jobs'
    request={'job_id':'planning-error','provider':'transformers',
             'brief':{'text':'forest','budget_seconds':6}}
    with TestClient(create_app(library.root,jobs)) as client:
        assert client.post('/api/jobs',json=request).status_code==202
        state=wait_terminal(client,'planning-error')
        assert state['status']=='failed' and 'planning model' in state['error']
    with TestClient(create_app(library.root,jobs)) as client:
        state=client.get('/api/jobs/planning-error').json()
        assert state['status']=='failed' and 'planning model' in state['error']
        assert client.get('/api/jobs/planning-error/output').status_code==409
        assert client.post('/api/jobs',json=request).status_code==202
        assert wait_terminal(client,'planning-error')['attempts']==2


def test_backpressure_is_shared_between_two_apps(library,tmp_path,monkeypatch):
    from reelpilot import api
    entered,finish=Event(),Event()
    def held_pipeline(*args,**kwargs):
        entered.set()
        assert finish.wait(5)
    monkeypatch.setattr(api,'run_job',held_pipeline)
    jobs=tmp_path/'jobs';body={'job_id':'held','brief':{'text':'forest','budget_seconds':6}}
    with TestClient(create_app(library.root,jobs,workers=1,max_inflight=1)) as first:
        with TestClient(create_app(library.root,jobs,workers=1,max_inflight=1)) as second:
            try:
                assert first.post('/api/jobs',json=body).status_code==202
                assert entered.wait(3)
                assert second.post('/api/jobs',json=body).status_code==409
                response=second.post('/api/jobs',json={**body,'job_id':'overflow'})
                assert response.status_code==429 and response.headers['Retry-After']=='5'
                assert second.get('/api/jobs/overflow').status_code==404
            finally:finish.set()


def test_stale_preplanner_cannot_enter_pipeline(library,tmp_path,monkeypatch):
    from reelpilot import api,planner
    from reelpilot.schema import explicit_spec
    jobs=tmp_path/'jobs';calls=[]
    def stale_plan(brief,*args,**kwargs):
        expire(Journal(jobs),'stale')
        return explicit_spec(brief)
    monkeypatch.setattr(planner,'transformers_spec',stale_plan)
    monkeypatch.setattr(api,'run_job',lambda *a,**k:calls.append(True))
    with TestClient(create_app(library.root,jobs,planner_path='test-double-no-model')) as client:
        assert client.post('/api/jobs',json={'job_id':'stale','provider':'transformers',
            'brief':{'text':'forest','budget_seconds':6}}).status_code==202
        assert wait_terminal(client,'stale')['status']=='interrupted'
    assert calls==[]
