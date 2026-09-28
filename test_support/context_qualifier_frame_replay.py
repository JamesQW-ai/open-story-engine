"""Offline replacement-contract review; preserves prior failures and measures inputs."""
import argparse
import hashlib
import json
from pathlib import Path

from test_support import context_qualifier_coverage_eval as previous
from test_support import context_qualifier_frame as frame

f = previous.f
FIXTURE = f.ROOT/'test_support/fixtures/context-qualifier-frame-cases-2026-09-26.json'
PROMPT = f.ROOT/'test_support/prompts/context_qualifier_frame.md'
EXPECTED_FIXTURE_SHA256 = '9d53bdcab322b3e51aeaa023846adaa8ffbb40e60b2a196595a0dece896753e2'
EXPECTED_PROMPT_SHA256 = '7d4a368ebaba977a229cc12d850ec3cb8c46b62ed222aea1556d90378adb3da7'
OUTPUT = previous.OUTPUT.with_name('context-qualifier-frame-replay-2026-09-26.json')
AUDIT = previous.AUDIT.with_name('context-qualifier-frame-replay-audit-2026-09-26.json')


def load_cases():
    if f.sha(FIXTURE) != EXPECTED_FIXTURE_SHA256 or f.sha(PROMPT) != EXPECTED_PROMPT_SHA256:
        raise ValueError('替代协议提示或夹具哈希变化')
    fixture = previous.load_cases()
    extra = json.loads(FIXTURE.read_text())
    fixture['cases'].extend(extra['cases'])
    if (len(fixture['cases']) != 44
            or len({c['id'] for c in fixture['cases']}) != 44
            or set(extra['attributions']) != {c['id'] for c in fixture['cases']}):
        raise ValueError('固定案例集合变化')
    for c in fixture['cases']:
        expected = extra['attributions'][c['id']]
        data = frame.model_input(c, fixture['scenes'][c['sceneKey']])
        if (hashlib.sha256(c['draft'].encode()).hexdigest() != c['draftSha256']
                or set(expected) != set(data['tasks'])
                or any(kind not in frame.ATTRIBUTIONS for kind in expected.values())):
            raise ValueError('原文或归属标签绑定变化')
        c['frameAttributions'] = expected
    fixture['scope'] = '38条已见固定用例加6条归属控制；4条仅验证未决路由，非真实模型或正文验收。'
    return fixture


def messages(case, entities):
    return [dict(role='system', content=PROMPT.read_text()),
            dict(role='user', content=json.dumps(frame.model_input(case, entities), ensure_ascii=False))]


def build():
    if previous.audit() != json.loads(previous.AUDIT.read_text()):
        raise ValueError('前序实测审计变化')
    fixture = load_cases()
    prior = json.loads(previous.OUTPUT.read_text())
    old_rows = {r['caseId']: r for r in prior['cases']}
    hashes = dict(prior['inputHashes'])
    for path in (FIXTURE, PROMPT, Path(__file__), Path(frame.__file__)):
        hashes[str(path.relative_to(f.ROOT))] = f.sha(path)
    evidence = dict(prior['priorEvidenceHashes'])
    for path in (previous.OUTPUT, previous.AUDIT):
        evidence[str(path.relative_to(f.ROOT))] = f.sha(path)
    rows = []
    for c in fixture['cases']:
        entities = fixture['scenes'][c['sceneKey']]
        data = frame.model_input(c, entities)
        source = ''.join(x if isinstance(x, str) else ''.join(x['segments'].values()) for x in data['source'])
        if source != c['draft']:
            raise ValueError('原文投影不可逐字还原')
        old = old_rows.get(c['id'])
        if old:
            old_data = json.loads(old['calls'][0]['messages'][1]['content'])
            if any(data[k] != old_data[k] for k in ('source', 'entityRefs')):
                raise ValueError('旧原文或实体引用变化')
        rows.append(dict(caseId=c['id'], cohort=c['cohort'], draftSha256=c['draftSha256'],
                         tasks=len(data['tasks']), contextUnitAssignments=sum(len(t['contextUnits']) for t in data['tasks'].values()),
                         expectedPositions=len(c['targets']), expectedUnresolved=sum(x == 'unresolved' for x in c['frameAttributions'].values()),
                         candidateUserChars=len(json.dumps(data, ensure_ascii=False)),
                         candidateInputChars=sum(len(m['content']) for m in messages(c, entities)),
                         priorStatus=old['status'] if old else None,
                         priorInputChars=sum(len(m['content']) for m in old['calls'][0]['messages']) if old else None))
    shared = [r for r in rows if r['priorInputChars'] is not None]
    return dict(schemaVersion='qualifier-frame-replay/0.1', scope=fixture['scope'], inputHashes=hashes,
                priorEvidenceHashes=evidence, cases=rows, priorSummary=prior['summary'],
                priorFailures=[dict(caseId=r['caseId'], status=r['status'], error=r.get('error'))
                               for r in prior['cases'] if r['status'] not in ('matched', 'scope_pending')],
                summary=dict(cases=len(rows), tasks=sum(r['tasks'] for r in rows),
                             expectedPositions=sum(r['expectedPositions'] for r in rows),
                             contextUnitAssignments=sum(r['contextUnitAssignments'] for r in rows),
                             scopeControlCases=sum(r['cohort'] == 'scope_controls' for r in rows),
                             sharedCases=len(shared), priorInputChars=sum(r['priorInputChars'] for r in shared),
                             replacementInputChars=sum(r['candidateInputChars'] for r in shared),
                             allCandidateInputChars=sum(r['candidateInputChars'] for r in rows)),
                newModelCalls=0, semanticCoverage='unverified', reviewRequired=True,
                acceptance=False, productionEnablement=False,
                formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)


def audit(source=OUTPUT):
    report = json.loads(source.read_text())
    if report != build():
        raise ValueError('替代契约回放或哈希绑定不可复现')
    return dict(schemaVersion='qualifier-frame-replay-audit/0.1', sourceArtifact=str(source),
                sourceSha256=f.sha(source), allRowsReproduced=True, newModelCalls=0,
                summary=report['summary'], priorSummary=report['priorSummary'],
                semanticCoverage='unverified', acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = audit() if args.audit else build()
    f.save_checkpoint(AUDIT if args.audit else OUTPUT, result, create=True)
    print(json.dumps(result['summary']))
