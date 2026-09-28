"""Frozen bounded quote-contract experiment; no production writes or automatic repair."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from test_support import context_assertion_quote as contract
from test_support import context_assertion_locator_replay as previous

f = previous.f
FIXTURE = f.ROOT/'test_support/fixtures/context-assertion-quote-2026-09-27.json'
FIXTURE_SHA256 = '8625d757aed9f3c60099117c3bc1e635d0479068093ff0968d04043d09550727'
PROMPT = f.ROOT/'test_support/prompts/context_assertion_quote.md'
PROMPT_SHA256 = '5ac19ce3d32a1480907637f382897649d2034bc183a9e023382fd298fef2b714'
PRIOR_SHA256 = '0da97a2e313b977d28a3ca6a4c190bc63d231228eac0e495ecb18109994a5c94'
PRIOR_AUDIT_SHA256 = 'd3f07857f8d1546b23fda7499882335c369e20bb018de9386b1e0194eb6753ec'
OUTPUT = previous.OUTPUT.with_name('context-assertion-quote-eval-2026-09-27.json')
AUDIT = previous.AUDIT.with_name('context-assertion-quote-eval-audit-2026-09-27.json')
CALL_LIMIT = 48
OUTPUT_TOKEN_CAP = 1024


def prepare():
    for path, expected in ((FIXTURE,FIXTURE_SHA256),(PROMPT,PROMPT_SHA256),
            (previous.OUTPUT,PRIOR_SHA256),(previous.AUDIT,PRIOR_AUDIT_SHA256)):
        if f.sha(path) != expected:
            raise ValueError('冻结输入或前序证据变化：'+path.name)
    prior = json.loads(previous.OUTPUT.read_text())
    for group in ('inputHashes','priorEvidenceHashes'):
        for name, expected in prior[group].items():
            if f.sha(f.ROOT/name) != expected: raise ValueError('历史绑定变化：'+name)
    fixture, _, _ = previous.load_inputs()
    extra = json.loads(FIXTURE.read_text())
    fixture['cases'].extend(extra['additionalCases'])
    references = extra['cases']
    if len(fixture['cases']) != CALL_LIMIT or len(references) != CALL_LIMIT:
        raise ValueError('固定预算或参考数量变化')
    for c, ref in zip(fixture['cases'], references):
        if c['id'] != ref['caseId'] or hashlib.sha256(c['draft'].encode()).hexdigest() != ref['draftSha256']:
            raise ValueError('参考与实际原文不一致')
        if contract.inspect(ref['reference'], c, fixture['scenes'][c['sceneKey']])['status'] == 'invalid_response':
            raise ValueError('参考不能表达：'+c['id'])
        messages(c, fixture['scenes'][c['sceneKey']])
    return fixture, references, prior


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
    inputs, evidence = dict(prior['inputHashes']), dict(prior['priorEvidenceHashes'])
    for p in (FIXTURE,PROMPT,Path(__file__),Path(contract.__file__)):
        inputs[str(p.relative_to(f.ROOT))] = f.sha(p)
    for p in (previous.OUTPUT,previous.AUDIT):
        evidence[str(p.relative_to(f.ROOT))] = f.sha(p)
    return inputs,evidence


def summary(rows):
    return dict(all=dict(Counter(r['status'] for r in rows)),
        original44=dict(Counter(r['status'] for r in rows if not r['caseId'].startswith('design:'))),
        design4=dict(Counter(r['status'] for r in rows if r['caseId'].startswith('design:'))))


def run(output=OUTPUT):
    fixture, references, prior = prepare()
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url','api_key','model')): raise ValueError('模型配置不完整')
    inputs,evidence = bindings(prior)
    report = dict(schemaVersion='assertion-quote-eval/0.1', status='incomplete',
        recordedAt=datetime.now(timezone.utc).isoformat(), inputHashes=inputs, priorEvidenceHashes=evidence,
        model={k:config[k] for k in ('route','model')}, callLimit=CALL_LIMIT,
        outputTokenCap=OUTPUT_TOKEN_CAP, actualCalls=0, cases=[],
        comparisonScope='frozen_reference_not_semantic_truth', legacySubtypeComparable=False,
        acceptance=False, productionEnablement=False, formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    f.save_checkpoint(output,report,create=True)
    recorder = f._gateway(config,CALL_LIMIT)
    recorder.gateway.max_tokens = min(recorder.gateway.max_tokens, OUTPUT_TOKEN_CAP)
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
    if (report['schemaVersion']!='assertion-quote-eval/0.1'
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
    return dict(schemaVersion='assertion-quote-eval-audit/0.1',sourceSha256=f.sha(source),
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
