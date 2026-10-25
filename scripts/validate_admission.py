"""Reproduce admission/backpressure/recovery with generated media, no models.

This is a controlled integration workload, not a production load test or a
semantic/video-quality benchmark. Report paths contain no personal documents.
"""
from pathlib import Path
from threading import Event
import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

from fastapi.testclient import TestClient
from reelpilot import api
from reelpilot.demo import make_demo
from reelpilot.journal import Journal
from reelpilot.media import probe


def settle(client,job):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        result=client.get('/api/jobs/'+job).json()
        if result['status'] in ('complete','failed','interrupted'):
            return result
        time.sleep(.02)
    raise RuntimeError('controlled workload timed out')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report',type=Path,default=ROOT/'reports/admission_validation.json')
    parser.add_argument('--scratch',type=Path)
    args=parser.parse_args()
    scratch=args.scratch or Path(tempfile.mkdtemp(prefix='reelpilot-admission-'))
    scratch.mkdir(parents=True,exist_ok=True)
    library=make_demo(scratch/'media')
    jobs=scratch/'jobs';original=api.run_job
    entered,gate=Event(),Event()
    def gated(*a,**kw):
        entered.set()
        if not gate.wait(20):raise RuntimeError('test gate timeout')
        return original(*a,**kw)
    api.run_job=gated
    requests=[];responses=[]
    try:
        with TestClient(api.create_app(library.root,jobs,workers=1,max_inflight=3)) as client:
            for i in range(8):
                body={'job_id':f'burst-{i}','brief':{'text':'forest;robot','budget_seconds':8}}
                requests.append(body)
                responses.append(client.post('/api/jobs',json=body).status_code)
                if i==0:assert entered.wait(5)
            duplicate=client.post('/api/jobs',json=requests[0]).status_code
            conflict=client.post('/api/jobs',json={**requests[0],'brief':{'text':'dragon','budget_seconds':8}}).status_code
            gate.set()
            states=[settle(client,f'burst-{i}')['status'] for i in range(3)]
    finally:
        gate.set();api.run_job=original
    assert responses==[202]*3+[429]*5 and states==['complete']*3
    assert duplicate==409 and conflict==409
    journal=Journal(jobs)
    def render_attempts():
        return next(s['attempts'] for s in journal.get('burst-0')['stages'] if s['stage']=='render')
    before=render_attempts()
    with TestClient(api.create_app(library.root,jobs)) as client:
        assert client.post('/api/jobs',json=requests[0]).status_code==202
        assert settle(client,'burst-0')['status']=='complete'
        replay=render_attempts()
        (jobs/'burst-0/output.mp4').write_bytes(b'controlled corruption')
        assert client.post('/api/jobs',json=requests[0]).status_code==202
        assert settle(client,'burst-0')['status']=='complete'
        repaired=render_attempts()
        assert client.post('/api/jobs',json={'job_id':'preplan-failure','provider':'transformers',
            'brief':{'text':'forest','budget_seconds':6}}).status_code==202
        failure=settle(client,'preplan-failure')
        assert failure['status']=='failed' and 'planning model' in failure['error']
    command=[sys.executable,'-B','-c',
        'import json,sys; from pathlib import Path; from reelpilot.journal import Journal; '
        'from reelpilot.admission import Admission; '
        'print(json.dumps(Admission(Journal(Path(sys.argv[1]))).get("preplan-failure")))',str(jobs)]
    fresh=json.loads(subprocess.run(command,check=True,capture_output=True,text=True).stdout)
    assert fresh['status']=='failed' and before==replay==1 and repaired==2
    metadata=probe(jobs/'burst-0/output.mp4')
    report={'scope':'Controlled in-process HTTP handlers + SQLite + actual FFmpeg. '
                     'Original generated geometric media; no LLM/embedding inference, human users, '
                     'subjective quality score or production throughput measurement.',
        'environment':{'python':platform.python_version(),'platform':platform.system()},
        'burst':{'workers':1,'max_inflight':3,'requests':8,'http_statuses':responses,
                 'accepted_completed':3,'capacity_rejected':5,'duplicate_status':duplicate,
                 'changed_brief_status':conflict},
        'verified_replay':{'render_attempts_initial':before,'render_attempts_after_identical_replay':replay,
                           'render_attempts_after_corruption_repair':repaired},
        'preplanning_failure':{'original_status':failure['status'],'fresh_process_status':fresh['status'],
                               'error':fresh['error']},
        'output':{k:metadata[k] for k in ['duration','width','height']},
        'checks_passed':True,
        'limits':['Lease expiry and final publication ownership are tested with controlled injection.',
                  'No power-loss durability, multi-host queue, automatic restart or new model-quality claim.']}
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
