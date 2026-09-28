"""Compare frozen prose reviews; no generation, repair, commits, or Jev."""
import argparse
import copy
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import parse_json_content, writer_config_from_env
from open_story_engine.prompts import render_prompt, catalog_version
from open_story_engine.reader_scene_review import (
    SceneReviewError, dialogue_units, grounding_claims, grounding_input_evidence,
    validate_grounding, validate_knowledge_access,
)
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import ROOT, _load_env
from test_support.longform import longform_cases

FIXTURE = ROOT / 'test_support/fixtures/context-semantic-cases-2026-09-25.json'
PROMPT = ROOT / 'test_support/prompts/context_entailment.md'
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-semantic-eval-2026-09-25.json'
KINDS = {'current', 'unknown', 'background', 'reported', 'inference'}
VERDICTS = {'supported', 'unsupported', 'contradicted'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixture(path=FIXTURE):
    fixture = json.loads(path.read_text())
    if not any(c['package_id'] == fixture['packageId'] and c['version'] == fixture['packageVersion']
               for c in longform_cases()):
        raise ValueError('固定样本不属于当前达标官方长篇')
    source = ROOT / fixture['sourceArtifact']
    if sha(source) != fixture['sourceSha256']:
        raise ValueError('固定来源哈希变化')
    payload = json.loads(json.loads(source.read_text())['calls'][fixture['sourceCallIndex']]['messages'][1]['content'])
    ids = set()
    for case in fixture['cases']:
        if case['id'] in ids or case['expected'] not in {'allow', 'reject'}:
            raise ValueError('样本编号或期待值无效')
        ids.add(case['id'])
        paragraphs = dict(enumerate(case['draft'].split('\n\n'), 1))
        if (case['expected'] == 'reject') != bool(case['targets']):
            raise ValueError('拒绝样本须有明确目标')
        for target in case['targets']:
            if not target['quote'] or target['quote'] not in paragraphs[int(target['paragraphId'][1:])]:
                raise ValueError('期待引文不属于固定稿')
        if case['origin'] == 'recorded' and case['draft'] != '\n\n'.join(payload['draft'].values()):
            raise ValueError('原始失败稿不得修改')
    return fixture, payload


def clause_claims(body):
    """Preserve every character while indexing punctuation-bounded spans.

    These are mechanical segments, not assertions of semantic atomicity.
    Full prose stays in draft for subjects, dialogue, and conditional scope.
    """
    claims = {}
    for i, paragraph in enumerate(body.split('\n\n')):
        start = 0
        parts = []
        for match in re.finditer(r'[，,；;。！？!?]+[”」』]*|——', paragraph):
            parts.append(paragraph[start:match.end()])
            start = match.end()
        if start < len(paragraph):
            parts.append(paragraph[start:])
        for j, part in enumerate(parts):
            claims[f'P{i+1}-C{j+1}'] = {'claim': part, 'paragraphId': f'P{i+1}'}
    return claims


def case_payload(template, case, *, segmentation='sentence'):
    if segmentation not in {'sentence', 'clause'}:
        raise ValueError('未知分段模式')
    payload = copy.deepcopy(template)
    body = case['draft']
    payload.update(input=case['input'], requirements={'A1': case['input']}, boundaries={},
                   draft={f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))},
                   paragraphs=clause_claims(body) if segmentation == 'clause' else grounding_claims(body),
                   dialogueUnits=dialogue_units(body),
                   priorRepairIssues=None, repairTargets={})
    # Both arms get exactly the same context. Expected labels never enter it.
    return payload


def validate_shape(data, claims, evidence, *, propositions=False):
    checks = data.get('checks') if isinstance(data, dict) else None
    if not isinstance(checks, list) or len(checks) != len(claims):
        raise ValueError('核对须完整覆盖每个原文单元')
    seen = set()

    def validate_item(item):
        if (not isinstance(item, dict) or item.get('kind') not in KINDS
                or item.get('verdict') not in VERDICTS
                or not isinstance(item.get('reason'), str) or not item['reason'].strip()
                or not isinstance(item.get('sources'), list)):
            raise ValueError('核对字段无效')
        for ref in item['sources']:
            if (not isinstance(ref, dict) or not isinstance(ref.get('id'), str)
                    or not isinstance(ref.get('quote'), str) or len(ref['quote'].strip()) < 4
                    or ref['id'] not in evidence or ref['quote'] not in evidence[ref['id']]):
                raise ValueError('来源引文无效')

    for check in checks:
        validate_item(check)
        cid = check.get('id')
        if not isinstance(cid, str) or cid not in claims or cid in seen:
            raise ValueError('核对单元重复或不存在')
        seen.add(cid)
        if propositions:
            items = check.get('propositions')
            if not isinstance(items, list) or not items:
                raise ValueError('缺少逐命题核对')
            for item in items:
                validate_item(item)
                if (not isinstance(item.get('quote'), str) or not item['quote']
                        or not isinstance(item.get('claim'), str) or not item['claim'].strip()):
                    raise ValueError('命题缺少原文及语义')
            if ''.join(p['quote'] for p in items) != claims[cid]['claim']:
                raise ValueError('命题原文未按顺序完整覆盖单元')
    return checks


def assess(data, payload, case, arm):
    evidence = grounding_input_evidence(payload)
    violations, errors = [], []
    try:
        checks = validate_shape(data, payload['paragraphs'], evidence, propositions=arm == 'entailment')
    except (ValueError, TypeError) as error:
        return {'status': 'invalid_review', 'error': str(error)}

    validators = [lambda: validate_grounding(data, payload['paragraphs'], evidence=evidence)]
    if arm == 'baseline':
        validators.append(lambda: validate_knowledge_access(
            data, case['draft'], evidence, payload['people'], payload['playerId'], speaker_names=payload['people']))
    for validate in validators:
        try:
            validate()
        except SceneReviewError as error:
            violations.extend(error.violations)
        except (ValueError, TypeError) as error:
            errors.append(str(error))
    if arm == 'entailment':
        for check in checks:
            for item in check['propositions']:
                if (item['verdict'] != 'supported' or
                        (item['kind'] in {'background', 'reported', 'inference'} and not item['sources'])):
                    violations.append({'paragraphId': payload['paragraphs'][check['id']]['paragraphId'],
                                       'claim': item['quote'], 'reason': item['reason']})
    if errors:
        return {'status': 'invalid_review', 'errors': errors, 'violations': violations}
    detected = [target for target in case['targets'] if any(
        v['paragraphId'] == target['paragraphId'] and target['quote'] in v['claim'] for v in violations)]
    status = ('false_positive' if violations else 'matched') if case['expected'] == 'allow' else (
        'matched' if len(detected) == len(case['targets']) else 'false_negative')
    return {'status': status, 'detectedTargets': detected, 'violations': violations}


def run(output, *, fixture_path=FIXTURE, arms=('baseline', 'entailment'), segmentation='sentence', model=None):
    if not arms or len(set(arms)) != len(arms) or set(arms) - {'baseline', 'entailment'}:
        raise ValueError('评测路径无效')
    if segmentation not in {'sentence', 'clause'}:
        raise ValueError('未知分段模式')
    fixture, template = load_fixture(fixture_path)
    config = dict(writer_config_from_env())
    configured_model = config.get('model')
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError('实验模型名不能为空')
        config['model'] = model.strip()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    prompts = {'baseline': render_prompt('reader.scene_grounding'), 'entailment': PROMPT.read_text()}
    report = {'schemaVersion': 'context-semantic-eval/0.1', 'status': 'incomplete',
              'recordedAt': datetime.now(timezone.utc).isoformat(), 'fixtureSha256': sha(fixture_path),
              'sourceSha256': fixture['sourceSha256'], 'promptVersion': catalog_version(),
              'harnessSha256': sha(Path(__file__)), 'segmentation': segmentation,
              'promptHashes': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in prompts.items()},
              'model': {'route': config['route'], 'model': config['model']},
              'configuredModel': configured_model,
              'scope': fixture['boundary'], 'comparisonLimit': '审核契约与核对职责不同，只比较公开事实专项错误，不归因于上下文裁剪或模型切换。',
              'formalSessionWrites': 0, 'temporarySessionWrites': 0, 'jevCalls': 0,
              'productionEnablement': False, 'acceptance': False,
              'callLimit': len(arms) * len(fixture['cases']), 'cases': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    recorder = _gateway(config, report['callLimit'])
    for case in fixture['cases']:
        payload = case_payload(template, case, segmentation=segmentation)
        for arm in arms:
            row = {'caseId': case['id'], 'arm': arm, 'expected': case['expected'], 'targets': case['targets']}
            started = time.monotonic()
            count_before = len(recorder.calls)
            try:
                response = recorder.complete_json([{'role': 'system', 'content': prompts[arm]},
                                                   {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
                row.update(assess(parse_json_content(response.content), payload, case, arm))
            except Exception as error:
                row.update(status='model_error', error=str(error), errorType=type(error).__name__)
            row['elapsedMs'] = round((time.monotonic() - started) * 1000)
            row['call'] = recorder.calls[-1] if len(recorder.calls) > count_before else None
            report['cases'].append(row)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print(json.dumps({k: row[k] for k in ('caseId', 'arm', 'status')}, ensure_ascii=False), flush=True)
    report.update(status='completed', actualCalls=len(recorder.calls), summary={
        arm: {status: sum(r['arm'] == arm and r['status'] == status for r in report['cases'])
              for status in ('matched', 'false_positive', 'false_negative', 'invalid_review', 'model_error')}
        for arm in arms})
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--arm', choices=('baseline', 'entailment'), action='append')
    parser.add_argument('--segmentation', choices=('sentence', 'clause'), default='sentence')
    parser.add_argument('--model', help='仅覆盖本次评测模型；不修改环境文件、服务地址或生产配置')
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    report = run(args.output, arms=args.arm or ('baseline', 'entailment'),
                 segmentation=args.segmentation, model=args.model)
    print(json.dumps(report['summary'], ensure_ascii=False))
