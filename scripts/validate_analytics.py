"""Reproduce SQL marts using real original media and real durable task controls.

No models, downloads or user files. All media/databases live in a fresh UUID run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from reelpilot.admission import Admission
from reelpilot.analytics import ingest_journal, report_warehouse
from reelpilot.control import ServiceDraining
from reelpilot.demo import make_demo
from reelpilot.journal import Journal
from reelpilot.pipeline import run_job
from reelpilot.retrieval import Retriever
from reelpilot.schema import Brief, explicit_spec


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=Path('data/analytics-validation'))
    parser.add_argument('--output', type=Path, default=Path('reports/analytics_validation.json'))
    args = parser.parse_args()
    run = args.workspace.resolve()/('analytics-'+uuid.uuid4().hex)
    run.mkdir(parents=True, exist_ok=False)
    library = make_demo(run/'collection')
    retriever = Retriever(library)
    journal = Journal(run/'jobs')
    admission = Admission(journal)
    warehouse = run/'analytics.sqlite'
    body = {'brief': {'text': 'forest;robot', 'budget_seconds': 8},
            'provider': 'explicit', 'mode': 'lexical'}
    def execute(job, *, interrupt=False, text='forest;robot'):
        request_body = {**body, 'brief': {**body['brief'], 'text': text}}
        query_spec = explicit_spec(Brief.model_validate(request_body['brief']))
        ticket = admission.reserve(job, request_body)
        ticket.phase('executing')
        try:
            result = run_job(library, retriever, journal, job, query_spec,
                             mode='lexical', request_owner=ticket.owner,
                             interrupt_after='render' if interrupt else None)
        except Exception as error:
            ticket.finish(error)
            raise
        ticket.finish()
        return result

    try:
        execute('retry', interrupt=True)
    except RuntimeError as error:
        require(str(error) == 'controlled interruption after render', 'unexpected first failure')
    else:
        raise RuntimeError('expected controlled failure after committed real render')
    initial_load = ingest_journal(journal.db, warehouse, source_id='original-media-lab')
    first_report = report_warehouse(warehouse)
    retry = execute('retry')
    success = execute('success')
    execute('success')  # A successful resubmission is deliberately NOT called failure recovery.
    try:
        execute('no-evidence', text='nonexistentkeyword')
    except ValueError:
        pass
    else:
        raise RuntimeError('a no-evidence edit unexpectedly succeeded')
    ticket = admission.reserve('cancelled', body)
    admission.cancel('cancelled')
    ticket.finish()
    ticket = admission.reserve('deadline', body, timeout_seconds=0.001)
    time.sleep(0.02)
    ticket.finish()
    admission.reserve('draining', body).finish(ServiceDraining('controlled shutdown'))

    with sqlite3.connect(journal.db) as db:
        before_source = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()

    def interrupt(phase):
        require(phase == 'before_commit', 'unexpected checkpoint')
        raise RuntimeError('controlled warehouse interruption')

    try:
        ingest_journal(journal.db, warehouse, source_id='original-media-lab', checkpoint=interrupt)
    except RuntimeError as error:
        require(str(error) == 'controlled warehouse interruption', 'unexpected ETL error')
    else:
        raise RuntimeError('expected ETL rollback')
    require(report_warehouse(warehouse) == first_report, 'partial facts or watermark leaked through rollback')
    update = ingest_journal(journal.db, warehouse, source_id='original-media-lab')
    repeated = ingest_journal(journal.db, warehouse, source_id='original-media-lab')
    report = report_warehouse(warehouse)
    with sqlite3.connect(journal.db) as db:
        after_source = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
    require(before_source == after_source, 'source rows changed during extraction')
    totals = {name: sum(row[name] for row in report['daily']) for name in
              ['logical_requests', 'api_attempts', 'complete', 'failed', 'cancelled', 'timed_out',
               'interrupted', 'resubmitted_requests', 'complete_after_resubmission', 'render_commits', 'cache_hits']}
    require(totals == {'logical_requests': 6, 'api_attempts': 8, 'complete': 2, 'failed': 1,
                      'cancelled': 1, 'timed_out': 1, 'interrupted': 1, 'resubmitted_requests': 2,
                      'complete_after_resubmission': 2, 'render_commits': 2, 'cache_hits': 7},
            'SQL denominators or actual execution counts differ')
    require(all(repeated[k] == 0 for k in ['inserted_events', 'changed_requests', 'changed_stages']),
            'identical extraction duplicated facts')
    output = {
        'schema_version': 1, 'version': '0.6.0', 'status': 'passed',
        'kind': 'original-media CPU execution and incremental SQL reconciliation',
        'models_loaded': False, 'totals': totals, 'initial_load': initial_load,
        'post_failure_update': update, 'identical_collection': repeated,
        'checks': {'transactional_rollback': True, 'source_rows_unchanged': True,
                   'no_join_fanout': True, 'no_duplicate_facts_on_replay': True,
                   'retry_and_successful_resubmission_distinguished_in_scope': True},
        'media': [{'case': label, 'duration': row['media']['duration'],
                   'width': row['media']['width'], 'height': row['media']['height']}
                  for label, row in [('retry', retry), ('success', success)]],
        'metrics': report,
        'scope': ['6 deliberately exercised local cases, not a user cohort or success-rate benchmark',
                  'one failure recovery and one successful resubmission; SQL reports both as resubmission',
                  'real original media, lexical retrieval, explicit planner; no model semantics measured',
                  'one SQLite source snapshot per ingest; multi-source loads are not a global snapshot'],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'totals': totals, 'repeat_fact_writes': 0}))


if __name__ == '__main__':
    main()
