"""Rescore saved fullscan output, separating localization from interpretation."""
import argparse
import json
import statistics
from pathlib import Path

from test_support import context_fullscan as f
from test_support.context_claim_pipeline import save_checkpoint

OUTPUT = f.OUTPUT.with_name('context-fullscan-audit-2026-09-26.json')


def alignment(items, targets):
    """One-to-one by exact unit set; prefer exact meaning within shared spans."""
    remaining_items = set(range(len(items)))
    remaining_targets = set(range(len(targets)))
    pairs = []
    def match(i, j, phase):
        item, target = items[i], targets[j]
        if set(item['unitIds']) != set(target['unitIds']):
            return False
        normal = item['status'] == target['expectedStatus'] and item['normal'] == target['expectedNormal']
        flags = {x['kind'] for x in item['limitations']} == set(target['expectedLimitations'])
        return (normal and flags) if phase == 0 else normal if phase == 1 else True
    for phase in range(3):
        for i in sorted(remaining_items):
            found = next((j for j in sorted(remaining_targets) if match(i,j,phase)), None)
            if found is not None:
                pairs.append((i,found))
                remaining_items.remove(i)
                remaining_targets.remove(found)
    differences = []
    for i,j in pairs:
        item,target = items[i],targets[j]
        fields = []
        if item['status'] != target['expectedStatus']:fields.append('status')
        if item['normal'] != target['expectedNormal']:fields.append('normal')
        actual = {x['kind'] for x in item['limitations']}
        expected = set(target['expectedLimitations'])
        if actual != expected:fields.append('limitations')
        if fields:
            differences.append(dict(itemIndex=i,targetIndex=j,fields=fields,
                                    extraLimitations=sorted(actual-expected),missingLimitations=sorted(expected-actual)))
    return dict(locatedTargets=len(pairs),missingLocations=sorted(remaining_targets),
                extraLocations=sorted(remaining_items),semanticDifferences=differences)


def audit(source=f.OUTPUT):
    fixture=f.load_cases();report=json.loads(source.read_text())
    for field,path in (('fixtureSha256',f.FIXTURE),('promptSha256',f.PROMPT),
                       ('harnessSha256',f.ROOT/'test_support/context_fullscan.py')):
        if report[field]!=f.sha(path):raise ValueError('实验输入或程序变化：'+field)
    for name,h in report['dependencyHashes'].items():
        if f.sha(f.ROOT/name)!=h:raise ValueError('依赖变化：'+name)
    if report['status']!='completed' or len(report['cases'])!=len(fixture['cases']):raise ValueError('实验未完整结束')
    rows=[];models=set();finishes=set();duration=[];usage=dict(input=0,output=0)
    for row,case in zip(report['cases'],fixture['cases']):
        if row['caseId']!=case['id'] or len(row['calls'])!=1:raise ValueError('案例或请求计数不一致')
        call=row['calls'][0]
        messages=[dict(role='system',content=f.PROMPT.read_text()),
                  dict(role='user',content=json.dumps(f.model_input(case,fixture['entities']),ensure_ascii=False))]
        if call['messages']!=messages:raise ValueError('实际请求与冻结输入不一致')
        response=json.loads(call['content']);score=f.assess(response,case,fixture['entities'])
        if any(row[k]!=v for k,v in score.items()):raise ValueError('评分不可复现')
        if score['status']=='invalid_response':raise ValueError('格式失败不能作定位复算')
        rows.append(dict(caseId=case['id'],strictStatus=score['status'],
                         **alignment(response['items'],case['targets']),duplicateItems=score['duplicateItems']))
        raw=json.loads(call['rawResponse']);models.add(raw['model']);finishes.add(raw['choices'][0]['finish_reason'])
        usage['input']+=raw['usage']['prompt_tokens'];usage['output']+=raw['usage']['completion_tokens']
        duration.extend(o['transport']['durationMs'] for o in call['observations'] if o['outcome']=='completed')
    return dict(schemaVersion='context-fullscan-audit/0.1',sourceArtifact=str(source.relative_to(f.ROOT)),
                sourceSha256=f.sha(source),auditHarnessSha256=f.sha(Path(__file__)),allStatusesReproduced=True,
                actualInputsVerified=True,originalCalls=report['actualCalls'],newModelCalls=0,
                rawModels=sorted(models),finishReasons=sorted(finishes),requestMedianMs=statistics.median(duration),usage=usage,
                cases=rows,summary=dict(strictMatched=report['summary']['matched'],strictMismatched=report['summary']['mismatched'],
                totalTargets=sum(len(c['targets']) for c in fixture['cases']),locatedTargets=sum(r['locatedTargets'] for r in rows),
                missingLocations=sum(len(r['missingLocations']) for r in rows),extraLocations=sum(len(r['extraLocations']) for r in rows),
                duplicateItems=sum(len(r['duplicateItems']) for r in rows),semanticDifferences=sum(len(r['semanticDifferences']) for r in rows)),
                findings=[dict(severity='P2',caseId=r['caseId'],detail=d) for r in rows for d in r['semanticDifferences']],
                acceptance=False,productionEnablement=False,
                limitations=['定位匹配不是事实正确；严格评分未因多报限定而放宽。',
                             '同一场景的固定开发集，不是新场景留出集或全类型事实扫描。'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=OUTPUT)
    args=parser.parse_args();report=audit();save_checkpoint(args.output,report,create=True)
    print(json.dumps(dict(summary=report['summary'],usage=report['usage'],medianMs=report['requestMedianMs']),ensure_ascii=False))
