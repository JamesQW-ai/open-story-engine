"""Reproducible iteration summary; missing responses never count as zero-cost success."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
from test_support import context_assertion_quote_eval as original

f=original.f
OUTPUT=original.OUTPUT.with_name('context-assertion-iteration-summary-2026-09-27.json')
AUDIT=OUTPUT.with_name('context-assertion-iteration-summary-audit-2026-09-27.json')
SOURCES=[{'name': 'context-assertion-quote-eval-2026-09-27.json', 'path': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-eval-2026-09-27.json', 'sha256': 'e85edce268dd11a07e7c8b94855d22fae26ccebf7a4a7d15777e881c985d3ddf', 'auditPath': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-eval-audit-2026-09-27.json', 'auditSha256': '6221268b8cd94a17bf9fe97770df66dc324200c032ac9bd28ab05246458a5444'}, {'name': 'context-assertion-quote-v2-eval-2026-09-27.json', 'path': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-v2-eval-2026-09-27.json', 'sha256': '8005a872b4a912abc75cf9d07f4cf61ff4bc5d2db017b47872262085a830bffb', 'auditPath': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-v2-eval-audit-2026-09-27.json', 'auditSha256': 'f7026dc71877270a6fdf2365b6c535843037476f083793b4bb8ad268664eeddc'}, {'name': 'context-assertion-quote-reasoning-eval-2026-09-27.json', 'path': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-2026-09-27.json', 'sha256': '456344eb777076e5284582082e87aa8bd0ea20af634891a63a0a5e360cb907c1', 'auditPath': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-audit-2026-09-27.json', 'auditSha256': '11f4eaecfd3c757ebe04cc14b9a86912b98a80676f638100dd46c6618ec588ef'}, {'name': 'context-assertion-quote-reasoning-capacity-eval-2026-09-27.json', 'path': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-capacity-eval-2026-09-27.json', 'sha256': '608ed513c2983720da93b67b6afd13ada02cef8ce470056e6585e0ca25b06e5c', 'auditPath': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-capacity-eval-audit-2026-09-27.json', 'auditSha256': 'bf0bc186d8f5cebb602e1f00f0a3714a3479eb1b9f51070c28cc4799c64fcadd'}, {'name': 'context-assertion-quote-reasoning-remainder-eval-2026-09-27.json', 'path': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-remainder-eval-2026-09-27.json', 'sha256': '4e9d228a5e2a20eecc8aecfac817afd198fac167b12c05aa418a5347960f980e', 'auditPath': 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-remainder-eval-audit-2026-09-27.json', 'auditSha256': '08fba03e493f7623b47e2f9bdc0b0048e4d60c915c122571e391b0b4dd845293'}]


def metrics(rows):
    inputs=outputs=reasoning=unknown=0;durations=[];calls=0;finishes=Counter()
    for row in rows:
        for call in row['calls']:
            calls+=1
            raw=json.loads(call['rawResponse']) if call.get('rawResponse') else {}
            usage=raw.get('usage')
            if not usage:unknown+=1
            else:
                inputs+=usage['prompt_tokens'];outputs+=usage['completion_tokens']
                reasoning+=usage.get('completion_tokens_details',{}).get('reasoning_tokens',0)
            for choice in raw.get('choices',[]):finishes[choice.get('finish_reason')]+=1
            for o in call.get('observations',[]):
                t=o.get('transport',{})
                if t.get('completeBodyMs') is not None:durations.append(t['completeBodyMs'])
    return dict(actualCalls=calls,statuses=dict(Counter(r['status'] for r in rows)),
        reportedInputTokens=inputs,reportedOutputTokens=outputs,reportedReasoningTokens=reasoning,
        reportedTotalTokens=inputs+outputs,callsWithUnknownUsage=unknown,
        completeResponseCount=len(durations),completeResponseMedianMs=statistics.median(durations) if durations else None,
        completeResponseMaxMs=max(durations) if durations else None,finishReasons=dict(finishes))


def build():
    reports=[]
    for source in SOURCES:
        for path_key,hash_key in (('path','sha256'),('auditPath','auditSha256')):
            if f.sha(f.ROOT/source[path_key])!=source[hash_key]:raise ValueError('冻结证据变化')
        report=json.loads((f.ROOT/source['path']).read_text())
        audit=json.loads((f.ROOT/source['auditPath']).read_text())
        if audit['sourceSha256']!=source['sha256'] or audit['allScoresReproduced'] is not True:
            raise ValueError('审计与原证据不匹配')
        for group in ('inputHashes','priorEvidenceHashes'):
            for path,sha in report[group].items():
                if f.sha(f.ROOT/path)!=sha:raise ValueError('代码或输入漂移')
        reports.append(report)
    first,second,truncated,capacity,remainder=reports
    capacity_ids={r['caseId'] for r in capacity['cases']}
    remainder_ids={r['caseId'] for r in remainder['cases']}
    ids={r['caseId'] for r in second['cases']}
    if capacity_ids&remainder_ids or capacity_ids|remainder_ids!=ids or len(ids)!=48:
        raise ValueError('推理组重复或缺案例')
    baseline_calls={r['caseId']:r['calls'][0]['messages'] for r in second['cases']}
    for row in capacity['cases']+remainder['cases']:
        for call in row['calls']:
            if call['messages']!=baseline_calls[row['caseId']]:raise ValueError('推理组改变提示或输入')
    rows=[row for report in reports for row in report['cases']]
    return dict(schemaVersion='assertion-iteration-summary/0.1',sources=SOURCES,
        generatorSha256=f.sha(Path(__file__)),runs={s['name']:metrics(r['cases']) for s,r in zip(SOURCES,reports)},
        totals=metrics(rows),reasoning8192Coverage=metrics(capacity['cases']+remainder['cases']),
        reasoningInputsUnchanged=True,reasoningCasesDisjoint=True,defaultChanged=False,
        blockReason='reasoning profile timed out before completing fixed coverage; non-reasoning semantic errors remain',
        acceptance=False,productionEnablement=False,formalSessionWrites=0,jevCalls=0)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--audit',action='store_true');args=parser.parse_args()
    result=build()
    if args.audit:
        if json.loads(OUTPUT.read_text())!=result:raise ValueError('汇总不可复现')
        result=dict(sourceSha256=f.sha(OUTPUT),allMetricsReproduced=True,newModelCalls=0,acceptance=False)
    f.save_checkpoint(AUDIT if args.audit else OUTPUT,result,create=True)
    print(json.dumps(result.get('totals',result),ensure_ascii=False))
