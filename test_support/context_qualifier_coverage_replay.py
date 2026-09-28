"""Offline coverage inventory of frozen responses; no new model or quality score."""
import argparse
import json
from pathlib import Path

from test_support import context_qualifier_core_contract_eval as previous
from test_support import context_qualifier_coverage as coverage

f = previous.f
OUTPUT = previous.OUTPUT.with_name('context-qualifier-coverage-replay-2026-09-26.json')
AUDIT = previous.AUDIT.with_name('context-qualifier-coverage-replay-audit-2026-09-26.json')


def build():
    if previous.audit() != json.loads(previous.AUDIT.read_text()):
        raise ValueError('前序真实响应审计不一致')
    report = json.loads(previous.OUTPUT.read_text())
    fixture = previous.load_cases()
    hashes = dict(report['inputHashes'])
    for path in (Path(__file__), Path(coverage.__file__)):
        hashes[str(path.relative_to(f.ROOT))] = f.sha(path)
    rows = []
    for c, old in zip(fixture['cases'], report['cases']):
        entities = fixture['scenes'][c['sceneKey']]
        data = coverage.model_input(c, entities)
        old_data = json.loads(old['calls'][0]['messages'][1]['content'])
        if {k: data[k] for k in ('source', 'entityRefs')} != old_data:
            raise ValueError('源文或实体输入发生变化')
        index = data['coverageIndex']
        items = old['proposedResponse'][coverage.extraction.VERSION]
        missing = [sid for sid, slot in index['slots'].items()
                   if not any(item['subject'] == slot['subjectRef']
                              and item['boundary'] in slot['boundaryRefs'] for item in items)]
        # This only reports absent occurrence decisions. No automatic non_position
        # decision, new protocol response, label injection or semantic repair.
        rows.append(dict(caseId=c['id'], priorStatus=old['status'], coverageIndex=index,
                         slotsWithoutSubmittedItems=missing,
                         priorUserChars=len(old['calls'][0]['messages'][1]['content']),
                         proposedUserChars=len(json.dumps(data, ensure_ascii=False))))
    return dict(schemaVersion='qualifier-coverage-replay/0.1', inputHashes=hashes,
                priorEvidenceHashes={str(p.relative_to(f.ROOT)): f.sha(p)
                                     for p in (previous.OUTPUT, previous.AUDIT)},
                cases=rows, priorSummary=report['summary'], newModelCalls=0,
                summary=dict(cases=len(rows), slots=sum(len(r['coverageIndex']['slots']) for r in rows),
                             slotsWithoutSubmittedItems=sum(len(r['slotsWithoutSubmittedItems']) for r in rows),
                             pendingUnits=sum(len(r['coverageIndex']['pendingUnits']) for r in rows),
                             priorUserChars=sum(r['priorUserChars'] for r in rows),
                             proposedUserChars=sum(r['proposedUserChars'] for r in rows)),
                semanticCoverage='unverified', acceptance=False, productionEnablement=False,
                formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)


def audit(source=OUTPUT):
    report = json.loads(source.read_text())
    if build() != report:
        raise ValueError('离线覆盖回放或绑定不可复现')
    return dict(schemaVersion='qualifier-coverage-replay-audit/0.1', sourceSha256=f.sha(source),
                sourceArtifact=str(source), allRowsReproduced=True, newModelCalls=0,
                summary=report['summary'], priorSummary=report['priorSummary'],
                semanticCoverage='unverified', acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = audit() if args.audit else build()
    f.save_checkpoint(AUDIT if args.audit else OUTPUT, result, create=True)
    print(json.dumps(result['summary']))
