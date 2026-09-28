"""Replay exact-name binding checks without replacing frozen model scores."""
import argparse
from collections import Counter
import json
from pathlib import Path

from test_support import context_assertion_binding_audit as binding
from test_support import context_assertion_quote_eval as original

f=original.f
OUTPUT=original.OUTPUT.with_name('context-assertion-binding-replay-2026-09-27.json')
AUDIT=OUTPUT.with_name('context-assertion-binding-replay-audit-2026-09-27.json')
SOURCES={'docs/evidence/context-management-2026-09-22/context-assertion-quote-eval-2026-09-27.json': 'e85edce268dd11a07e7c8b94855d22fae26ccebf7a4a7d15777e881c985d3ddf', 'docs/evidence/context-management-2026-09-22/context-assertion-quote-v2-eval-2026-09-27.json': '8005a872b4a912abc75cf9d07f4cf61ff4bc5d2db017b47872262085a830bffb', 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-capacity-eval-2026-09-27.json': '608ed513c2983720da93b67b6afd13ada02cef8ce470056e6585e0ca25b06e5c'}


def build():
    fixture,_,_=original.prepare()
    cases={c['id']:c for c in fixture['cases']}
    rows=[];bound={}
    for name,expected in SOURCES.items():
        path=f.ROOT/name
        if f.sha(path)!=expected:raise ValueError('冻结模型报告变化')
        report=json.loads(path.read_text())
        for group in ('inputHashes','priorEvidenceHashes'):
            for source,sha in report[group].items():
                if f.sha(f.ROOT/source)!=sha:raise ValueError('输入或历史证据漂移')
                bound[source]=sha
        for row in report['cases']:
            c=cases[row['caseId']]
            if 'proposedResponse' not in row:raise ValueError('源报告没有可重放的候选')
            result=binding.audit(row['proposedResponse'],c,fixture['scenes'][c['sceneKey']])
            rows.append(dict(source=name,caseId=c['id'],originalStatus=row['status'],bindingAudit=result))
    for p in (Path(__file__),Path(binding.__file__)):
        bound[str(p.relative_to(f.ROOT))]=f.sha(p)
    return dict(schemaVersion='assertion-binding-replay/0.1',sources=SOURCES,inputHashes=bound,cases=rows,
        summary={name:dict(Counter(r['bindingAudit']['status'] for r in rows if r['source']==name)) for name in SOURCES},
        modelCalls=0,acceptance=False,productionEnablement=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--audit',action='store_true');args=parser.parse_args()
    result=build()
    if args.audit:
        if json.loads(OUTPUT.read_text())!=result:raise ValueError('离线绑定审核不可复现')
        result=dict(schemaVersion='assertion-binding-replay-audit/0.1',sourceSha256=f.sha(OUTPUT),
            allFindingsReproduced=True,modelCalls=0,acceptance=False,productionEnablement=False)
    f.save_checkpoint(AUDIT if args.audit else OUTPUT,result,create=True)
    print(json.dumps(result.get('summary',result),ensure_ascii=False))
