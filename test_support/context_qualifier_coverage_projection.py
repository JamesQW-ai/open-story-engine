"""Send decision references once; keep coverage diagnostics in the local ledger."""
import argparse
import json
from pathlib import Path

from test_support import context_qualifier_coverage as coverage
from test_support import context_qualifier_coverage_replay as replay

f = replay.f
OUTPUT = replay.OUTPUT.with_name('context-qualifier-coverage-projection-2026-09-26.json')


def model_input(case, entities):
    data = coverage.model_input(case, entities)
    tasks = {sid: {k: slot[k] for k in ('subjectRef', 'boundaryRefs')}
             for sid, slot in data['coverageIndex']['slots'].items()}
    return dict(source=data['source'], entityRefs=data['entityRefs'], coverageTasks=tasks)


def build():
    replay.audit()
    base = json.loads(replay.OUTPUT.read_text())
    fixture = replay.previous.load_cases()
    rows = []
    for c, old in zip(fixture['cases'], base['cases']):
        data = model_input(c, fixture['scenes'][c['sceneKey']])
        rows.append(dict(caseId=c['id'], priorUserChars=old['priorUserChars'],
                         fullIndexUserChars=old['proposedUserChars'],
                         projectedUserChars=len(json.dumps(data, ensure_ascii=False))))
    return dict(schemaVersion='qualifier-coverage-projection/0.1',
                inputHashes={str(p.relative_to(f.ROOT)): f.sha(p)
                             for p in (Path(__file__), Path(coverage.__file__), replay.OUTPUT, replay.AUDIT)},
                cases=rows, summary={k: sum(r[k] for r in rows)
                                    for k in ('priorUserChars', 'fullIndexUserChars', 'projectedUserChars')},
                newModelCalls=0, semanticCoverage='unverified', acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = build()
    if args.audit:
        if result != json.loads(OUTPUT.read_text()):
            raise ValueError('精简投影回放不可复现')
        print('离线投影与哈希绑定复现；新增模型调用 0')
    else:
        f.save_checkpoint(OUTPUT, result, create=True)
    print(json.dumps(result['summary']))
