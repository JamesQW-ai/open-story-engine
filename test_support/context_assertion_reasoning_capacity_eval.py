"""One budget-capacity revision after a recorded reasoning-only truncation."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from test_support import context_assertion_quote as contract
from test_support import context_assertion_locator_replay as previous
from test_support import context_assertion_reasoning_eval as baseline

f = previous.f
FIXTURE = f.ROOT/'test_support/fixtures/context-assertion-quote-2026-09-27.json'
FIXTURE_SHA256 = '8625d757aed9f3c60099117c3bc1e635d0479068093ff0968d04043d09550727'
PROMPT = f.ROOT/'test_support/prompts/context_assertion_quote_v2.md'
PROMPT_SHA256 = 'f073073e540faff37d47d6503503c2e7381bed46549cb7242792624aa288a023'
PRIOR_SHA256 = '0da97a2e313b977d28a3ca6a4c190bc63d231228eac0e495ecb18109994a5c94'
PRIOR_AUDIT_SHA256 = 'd3f07857f8d1546b23fda7499882335c369e20bb018de9386b1e0194eb6753ec'
OUTPUT = previous.OUTPUT.with_name('context-assertion-quote-reasoning-capacity-eval-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-assertion-quote-reasoning-capacity-eval-audit-2026-09-27.json')
CALL_LIMIT = 12
OUTPUT_TOKEN_CAP = 8192
BASELINE_HASHES = {'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-2026-09-27.json': '456344eb777076e5284582082e87aa8bd0ea20af634891a63a0a5e360cb907c1', 'docs/evidence/context-management-2026-09-22/context-assertion-quote-reasoning-eval-audit-2026-09-27.json': '11f4eaecfd3c757ebe04cc14b9a86912b98a80676f638100dd46c6618ec588ef'}
CASE_IDS = ['two_people:second_operator', 'denial:operator', 'control:object_position', 'control:quoted_words', 'control:mixed_people', 'frame:position_and_quote', 'frame:other_report', 'hall:source_position', 'frame:self_report', 'design:dual_origin', 'design:nested_pending', 'design:unknown_speaker_pending']
REQUESTED_REASONING_EFFORT = "low"


def prepare():
    fixture,references,prior=baseline.prepare()
    for path,expected in BASELINE_HASHES.items():
        if f.sha(f.ROOT/path)!=expected: raise ValueError('首轮实验或审计变化')
    by_id={c['id']:c for c in fixture['cases']}
    ref_by_id={r['caseId']:r for r in references}
    fixture['cases']=[by_id[cid] for cid in CASE_IDS]
    references=[ref_by_id[cid] for cid in CASE_IDS]
    if len(CASE_IDS)!=CALL_LIMIT or len(set(CASE_IDS))!=CALL_LIMIT:
        raise ValueError('固定诊断子集变化')
    for c in fixture['cases']: messages(c,fixture['scenes'][c['sceneKey']])
    return fixture,references,prior


def messages(case, entities):
    prompt = PROMPT.read_text()
    if f.sha(PROMPT) != PROMPT_SHA256 or len(prompt) > 1537 or 'json' not in prompt.lower():
        raise ValueError('提示变化或超过原归属提示字符预算')
    user = json.dumps(contract.model_input(case, entities), ensure_ascii=False, separators=(',', ':'))
    if len(user)+len(prompt) > 4000:
        raise ValueError('单次总输入字符预算超限')
    return [dict(role='system',content=prompt),dict(role='user',content=user)]


def assess_content(content, case, entities, reference):
    try:
        response = json.loads(content)
    except (ValueError, TypeError):
        return dict(status='invalid_json', rawContent=content, semanticStatus='unverified',
                    acceptance=False, productionEnablement=False)
    return contract.assess(response, case, entities, reference)


def bindings(prior):
    inputs,evidence=baseline.bindings(prior)
    for p in (PROMPT,Path(__file__)):
        inputs[str(p.relative_to(f.ROOT))]=f.sha(p)
    evidence.update(BASELINE_HASHES)
    return inputs,evidence


def summary(rows):
    return dict(all=dict(Counter(r['status'] for r in rows)),
        diagnosticSubset=dict(Counter(r['status'] for r in rows if not r['caseId'].startswith('design:'))),
        designControls=dict(Counter(r['status'] for r in rows if r['caseId'].startswith('design:'))))


def run(output=OUTPUT):
    fixture, references, prior = prepare()
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url','api_key','model')): raise ValueError('模型配置不完整')
    inputs,evidence = bindings(prior)
    report = dict(schemaVersion='assertion-quote-reasoning-capacity-eval/0.1', status='incomplete',
        recordedAt=datetime.now(timezone.utc).isoformat(), inputHashes=inputs, priorEvidenceHashes=evidence,
        model={k:config[k] for k in ('route','model')}, callLimit=CALL_LIMIT,
        outputTokenCap=OUTPUT_TOKEN_CAP, requestedReasoningEffort=REQUESTED_REASONING_EFFORT, actualCalls=0, cases=[],
        comparisonScope='frozen_reference_not_semantic_truth', legacySubtypeComparable=False,
        acceptance=False, productionEnablement=False, formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    f.save_checkpoint(output,report,create=True)
    recorder = f._gateway(config,CALL_LIMIT)
    recorder.gateway.max_tokens = min(recorder.gateway.max_tokens, OUTPUT_TOKEN_CAP)
    recorder.gateway.reasoning_effort = REQUESTED_REASONING_EFFORT
    blocked = False
    def save():
        report['actualCalls'] = len(recorder.calls)
        try: f.save_checkpoint(output,report)
        except OSError as error: raise f.CheckpointError('检查点保存失败，停止调用') from error
    for c, ref in zip(fixture['cases'],references):
        row = dict(caseId=c['id'],status='incomplete',calls=[])
        report['cases'].append(row)
        if blocked:
            row['status']='not_run'; save(); continue
        before=len(recorder.calls)
        try:
            try: response=recorder.complete_json(messages(c,fixture['scenes'][c['sceneKey']]))
            finally:
                row['calls'].extend(recorder.calls[before:]); save()
            row.update(assess_content(response.content,c,fixture['scenes'][c['sceneKey']],ref['reference']))
        except f.CheckpointError: raise
        except f.LlmError as error:
            row.update(status='model_error',error=str(error)); blocked=True
        except Exception as error:
            row.update(status='harness_error',error=str(error)); blocked=True
        save()
        print(json.dumps(dict(caseId=c['id'],status=row['status'])),flush=True)
    report.update(status='blocked' if blocked else 'completed',summary=summary(report['cases']))
    save()
    return report


def audit(source=OUTPUT):
    fixture,references,prior=prepare(); report=json.loads(source.read_text())
    inputs,evidence=bindings(prior)
    if report['inputHashes']!=inputs or report['priorEvidenceHashes']!=evidence:
        raise ValueError('实验绑定变化')
    if (report['requestedReasoningEffort']!=REQUESTED_REASONING_EFFORT
            or report['schemaVersion']!='assertion-quote-reasoning-capacity-eval/0.1'
            or report['comparisonScope']!='frozen_reference_not_semantic_truth'
            or report['legacySubtypeComparable'] is not False):
        raise ValueError('协议或比较口径变化')
    if (report['callLimit']!=CALL_LIMIT or report['outputTokenCap']!=OUTPUT_TOKEN_CAP or len(report['cases'])!=CALL_LIMIT
            or report['actualCalls']!=sum(len(r['calls']) for r in report['cases'])
            or report['actualCalls']>CALL_LIMIT or report['acceptance'] is not False
            or report['productionEnablement'] is not False
            or any(report[k]!=0 for k in ('formalSessionWrites','reviewRecordsWritten','jevCalls'))):
        raise ValueError('实验边界或调用计数变化')
    blocked=False
    for c,ref,row in zip(fixture['cases'],references,report['cases']):
        if row['caseId']!=c['id']: raise ValueError('案例顺序变化')
        if blocked:
            if row!=dict(caseId=c['id'],status='not_run',calls=[]): raise ValueError('阻塞后仍调用')
            continue
        if len(row['calls'])!=1: raise ValueError('单案例调用预算变化')
        call=row['calls'][0]
        if call['messages']!=messages(c,fixture['scenes'][c['sceneKey']]): raise ValueError('实际输入变化')
        if row['status']=='model_error':
            if not call.get('error') or row != dict(caseId=c['id'],status='model_error',
                    calls=[call],error=call['error']):
                raise ValueError('失败记录与原始错误不一致')
            blocked=True; continue
        if row['status']=='harness_error': raise ValueError('执行器错误未解决')
        raw=json.loads(call['rawResponse'])
        if raw['choices'][0]['message']['content']!=call['content']: raise ValueError('原始回答变化')
        expected=assess_content(call['content'],c,fixture['scenes'][c['sceneKey']],ref['reference'])
        if {k:v for k,v in row.items() if k not in ('caseId','calls')}!=expected: raise ValueError('评分不可复现')
    if report['summary']!=summary(report['cases']) or report['status']!=('blocked' if blocked else 'completed'):
        raise ValueError('汇总或运行状态变化')
    return dict(schemaVersion='assertion-quote-reasoning-capacity-eval-audit/0.1',sourceSha256=f.sha(source),
        allScoresReproduced=True,actualInputsVerified=True,summary=report['summary'],
        originalCalls=report['actualCalls'],newModelCalls=0,acceptance=False,productionEnablement=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit',action='store_true')
    args=parser.parse_args()
    if args.audit:
        result=audit(); f.save_checkpoint(AUDIT,result,create=True)
    else:
        f._load_env(f.ROOT/'.env'); result=run()
    print(json.dumps(result['summary']))
