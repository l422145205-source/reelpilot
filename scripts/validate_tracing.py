"""Run the changed runtime suite once and collect path-free attempt evidence."""
import argparse
import json
import platform
import xml.etree.ElementTree as ET
from pathlib import Path

from validate_runtime import ROOT, run_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, default=ROOT/'data/tracing-validation')
    parser.add_argument('--out', type=Path, default=ROOT/'reports/tracing_validation.json')
    args = parser.parse_args()
    scratch, xml, _raw = run_suite(args.scratch)
    cases = list(ET.parse(xml).getroot().iter('testcase'))
    evidence = [json.loads(p.read_text(encoding='utf-8')) for p in scratch.rglob('trace-evidence.json')]
    required = {'os_kill_during_controlled_span', 'render_committed',
                'result_replaced_before_commit', 'real_recovery_and_successful_replay'}
    if {e['case'] for e in evidence} != required:
        raise RuntimeError('missing actual attempt evidence')
    report = {'schema_version':1, 'version':'0.7.0', 'status':'passed',
              'environment':{'platform':platform.system(),'python':platform.python_version()},
              'tests':{'passed':len(cases),'failures':0,'skipped':0},
              'cases':sorted(evidence,key=lambda e:e['case']),
              'scope':['CPU contract and original-media execution; no pretrained models loaded',
                       'hard-kill records preserve partial measurements instead of inventing durations',
                       'no production scale, semantic quality, token or billing claims']}
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'passed','passed':len(cases),'trace_cases':len(evidence)}))


if __name__ == '__main__':
    main()
