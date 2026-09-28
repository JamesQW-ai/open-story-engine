"""Full short-draft position scan with occurrence-level coverage accounting."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import LlmError, parse_json_content, writer_config_from_env
from test_support.context_candidate_mapping import FIXTURE as SEED, LIMITATIONS, assess as assess_item, load_cases as load_seed
from test_support.context_claim_pipeline import CheckpointError, save_checkpoint
from test_support.context_indexed_pipeline import indexed_input
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import _load_env
from test_support.context_scene_relations import _normal, load_contract
from test_support.context_semantic_eval import ROOT, sha

FIXTURE = ROOT/'test_support/fixtures/context-fullscan-cases-2026-09-26.json'
PROMPT = ROOT/'test_support/prompts/context_fullscan.md'
OUTPUT = ROOT/'docs/evidence/context-management-2026-09-22/context-fullscan-2026-09-26.json'


def model_input(case, entities):
    return {**indexed_input(case), 'entities': {k:{f:v[f] for f in ('kind','mentions')} for k,v in entities.items()}}


def _signature(ids, status, normal, limitations):
    return json.dumps([sorted(ids), status, normal, sorted(limitations)], sort_keys=True, ensure_ascii=False)


def assess(response, case, entities):
    units = indexed_input(case)['units']
    if not isinstance(response, dict) or set(response) != {'items'} or not isinstance(response['items'], list):
        return dict(status='invalid_response', error='只能返回 items')
    actual, duplicates = [], []
    try:
        for index, item in enumerate(response['items']):
            if not isinstance(item, dict) or set(item) != {'unitIds','status','normal','limitations','reason'}:
                raise ValueError('命题字段无效')
            ids = item['unitIds']
            if (not isinstance(ids, list) or not ids or any(not isinstance(i,str) or i not in units for i in ids)
                    or len(set(ids)) != len(ids) or ids != [i for i in units if i in ids]):
                raise ValueError('原文单元不存在、重复或顺序无效')
            quote = ''.join(units[i]['quote'] for i in ids)
            check = assess_item({k:v for k,v in item.items() if k != 'unitIds'},
                                dict(target={'quote':quote}, expectedStatus='candidate', expectedNormal=None, expectedLimitations=[]), entities)
            if check['status'] == 'invalid_response':
                raise ValueError(check['error'])
            # A limitation must occur in a real selected span, not across a
            # synthetic concatenation of two nonadjacent spans.
            for limit in item['limitations']:
                if not any(limit['quote'] in units[i]['quote'] for i in ids):
                    raise ValueError('限定引文跨越单元，须选择真实单元内引文')
            signature = _signature(ids,item['status'],item['normal'],[x['kind'] for x in item['limitations']])
            if signature in actual:
                duplicates.append(index)
            actual.append(signature)
    except (ValueError,KeyError,TypeError) as error:
        return dict(status='invalid_response',error=str(error))
    targets = [_signature(t['unitIds'],t['expectedStatus'],t['expectedNormal'],t['expectedLimitations']) for t in case['targets']]
    available = list(enumerate(targets))
    matched, extra = [], []
    for index, signature in enumerate(actual):
        found = next(((i,s) for i,s in available if s == signature),None)
        if found is None:
            extra.append(index)
        else:
            matched.append(found[0]); available.remove(found)
    missing = [i for i,_ in available]
    return dict(status='matched' if not missing and not extra else 'mismatched', matchedTargets=matched,
                missingTargets=missing, extraItems=extra, duplicateItems=duplicates,
                requiresSemanticReview=True, productionEnablement=False)


def load_cases(path=FIXTURE):
    seed = load_seed()
    _,_,evidence,originals,_ = load_contract()
    fixture = json.loads(path.read_text())
    if fixture['bindings'] != {str(SEED.relative_to(ROOT)):sha(SEED)}:
        raise ValueError('前序来源绑定变化')
    entities = fixture['entities']
    if set(entities) != set(seed['entities']) | set(fixture['additionalEntitySources']):
        raise ValueError('实体集合无来源')
    for key,descriptor in seed['entities'].items():
        if entities[key] != descriptor: raise ValueError('前序实体描述变化')
    for key,ref in fixture['additionalEntitySources'].items():
        entity=entities[key]
        if (ref['id'] not in evidence or hashlib.sha256(evidence[ref['id']].encode()).hexdigest()!=ref['contentSha256']
                or ref['quote'] not in evidence[ref['id']] or not ref['quote']
                or set(entity)!={'kind','mentions'} or entity['kind']!='scene_person'
                or entity['mentions'] != [ref['quote']]):
            raise ValueError('新增实体未绑定公开称呼')
    seen=set()
    for case in fixture['cases']:
        if not isinstance(case['id'],str) or not case['id'] or case['id'] in seen:raise ValueError('案例编号无效')
        seen.add(case['id'])
        if hashlib.sha256(case['draft'].encode()).hexdigest()!=case['draftSha256']:raise ValueError('原稿哈希变化')
        if case['origin']=='recorded_v11' and case['draft']!=originals['recorded_v11:C10']['draft']:raise ValueError('原始失败稿变化')
        units=indexed_input(case)['units']; signatures=[]
        for target in case['targets']:
            ids=target['unitIds']
            if not ids or len(set(ids))!=len(ids) or any(i not in units for i in ids) or ids != [i for i in units if i in ids]:raise ValueError('预标定位无效')
            if target['expectedStatus']=='candidate':
                _normal(target['expectedNormal'],entities)
                if target['expectedLimitations']:raise ValueError('候选预标不能丢弃限定')
            elif target['expectedStatus']=='needs_review':
                flags=target['expectedLimitations']
                if target['expectedNormal'] is not None or not flags or not set(flags)<=LIMITATIONS or len(set(flags))!=len(flags):raise ValueError('待复核预标无效')
            else:raise ValueError('未知预标状态')
            signature=_signature(ids,target['expectedStatus'],target['expectedNormal'],target['expectedLimitations'])
            if signature in signatures:raise ValueError('预标命题重复')
            signatures.append(signature)
    if not 1<=len(seen)<=9:raise ValueError('固定调用上限为 9')
    return fixture


def run(output):
    fixture=load_cases(); config=writer_config_from_env(); prompt=PROMPT.read_text()
    if any(not config.get(k) for k in ('base_url','api_key','model')) or 'json' not in prompt.lower():raise ValueError('模型配置或 JSON 提示无效')
    dependencies=['test_support/context_candidate_mapping.py','test_support/context_indexed_pipeline.py','test_support/context_claim_pipeline.py']
    report=dict(schemaVersion='context-fullscan/0.1',status='incomplete',recordedAt=datetime.now(timezone.utc).isoformat(),
                fixtureSha256=sha(FIXTURE),promptSha256=sha(PROMPT),harnessSha256=sha(Path(__file__)),
                dependencyHashes={p:sha(ROOT/p) for p in dependencies},model={k:config[k] for k in ('route','model')},
                scope=fixture['scope'],callLimit=len(fixture['cases']),actualCalls=0,cases=[],acceptance=False,
                productionEnablement=False,formalSessionWrites=0,reviewRecordsWritten=0,jevCalls=0)
    output.parent.mkdir(parents=True,exist_ok=True);save_checkpoint(output,report,create=True)
    recorder=_gateway(config,report['callLimit'])
    def save():
        report['actualCalls']=len(recorder.calls)
        try:save_checkpoint(output,report)
        except OSError as error:raise CheckpointError('证据保存失败，停止调用') from error
    blocked=False
    for case in fixture['cases']:
        row=dict(caseId=case['id'],status='incomplete',calls=[]);report['cases'].append(row)
        if blocked:
            row['status']='not_run';save();continue
        before=len(recorder.calls)
        try:
            try:
                response=recorder.complete_json([dict(role='system',content=prompt),dict(role='user',content=json.dumps(model_input(case,fixture['entities']),ensure_ascii=False))])
            finally:
                row['calls'].extend(recorder.calls[before:]);save()
            try:row.update(assess(parse_json_content(response.content),case,fixture['entities']))
            except (ValueError,LlmError) as error:row.update(status='invalid_response',error=str(error))
        except CheckpointError:raise
        except LlmError as error:row.update(status='model_error',error=str(error));blocked=True
        except Exception as error:row.update(status='harness_error',error=str(error));blocked=True
        save();print(json.dumps(dict(caseId=row['caseId'],status=row['status']),ensure_ascii=False),flush=True)
    report['status']='blocked' if blocked else 'completed'
    report['summary']={s:sum(r['status']==s for r in report['cases']) for s in ('matched','mismatched','invalid_response','model_error','harness_error','not_run')}
    save();return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=OUTPUT)
    args=parser.parse_args();_load_env(ROOT/'.env')
    print(json.dumps(run(args.output)['summary'],ensure_ascii=False))
