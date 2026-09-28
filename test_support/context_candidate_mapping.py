"""Bounded blind mapping experiment; never creates reviewed or authoritative data."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import LlmError, parse_json_content, writer_config_from_env
from test_support.context_claim_pipeline import CheckpointError, save_checkpoint
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import _load_env
from test_support.context_scene_relations import DATA, REVIEW, _normal, _span, load_contract
from test_support.context_relation_audit import FIXTURE as RELATION_FIXTURE
from test_support.context_semantic_eval import ROOT, sha

FIXTURE = ROOT / 'test_support/fixtures/context-candidate-mapping-cases-2026-09-26.json'
PROMPT = ROOT / 'test_support/prompts/context_candidate_mapping.md'
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-candidate-mapping-2026-09-26.json'
LIMITATIONS = {'time', 'condition', 'modality', 'unresolved_subject', 'unresolved_object', 'ambiguity'}


def load_cases(path=FIXTURE):
    data, _, _, originals, _ = load_contract()
    fixture = json.loads(path.read_text())
    required = {str(p.relative_to(ROOT)):sha(p) for p in (DATA, REVIEW, RELATION_FIXTURE)}
    if fixture['bindings'] != required:
        raise ValueError('候选实验的前序来源已变化')
    entities = fixture['entities']
    if set(entities) != {e['id'] for e in data['entities']}:
        raise ValueError('实体集合不匹配')
    for entity in data['entities']:
        descriptor = entities[entity['id']]
        if (set(descriptor) != {'kind', 'mentions'} or descriptor['kind'] != entity['kind']
                or not isinstance(descriptor['mentions'], list) or not descriptor['mentions']
                or any(not isinstance(x, str) or not x or x not in entity['source']['span']['quote'] for x in descriptor['mentions'])):
            raise ValueError('实体描述包含未绑定称呼或多余字段')
    seen = set()
    for case in fixture['cases']:
        if not isinstance(case['id'], str) or not case['id'] or case['id'] in seen:
            raise ValueError('案例编号无效或重复')
        seen.add(case['id'])
        if hashlib.sha256(case['draft'].encode()).hexdigest() != case['draftSha256']:
            raise ValueError('原稿哈希不匹配')
        _span(case['target'], case['draft'])
        if case['expectedStatus'] == 'candidate':
            original = originals[case['origin']]
            mapping = next(m for m in data['mappings'] if m['origin'] == case['origin'])
            if (case['draft'] != original['draft'] or case['target'] != original['span']
                    or case['expectedNormal'] != mapping['normal'] or case['expectedLimitations']):
                raise ValueError('固定映射期待值变化')
            _normal(case['expectedNormal'], entities)
        elif case['expectedStatus'] == 'needs_review':
            if (case['expectedNormal'] is not None or not case['expectedLimitations']
                    or not set(case['expectedLimitations']) <= LIMITATIONS
                    or not case['limitationQuote'] or case['limitationQuote'] not in case['target']['quote']):
                raise ValueError('待复核期待值无效')
        else:
            raise ValueError('未知期待状态')
    if not 1 <= len(seen) <= 10:
        raise ValueError('实验必须有 1 至 10 条固定用例')
    return fixture


def model_input(case, entities):
    return dict(draft=case['draft'], target=dict(case['target']),
                entities={key:{k:value[k] for k in ('kind', 'mentions')} for key,value in entities.items()})


def assess(response, case, entities):
    try:
        if (not isinstance(response, dict) or set(response) != {'status', 'normal', 'limitations', 'reason'}
                or response['status'] not in ('candidate', 'needs_review')
                or not isinstance(response['reason'], str) or not response['reason'].strip()
                or not isinstance(response['limitations'], list)):
            raise ValueError('候选响应字段无效')
        if response['status'] == 'candidate':
            _normal(response['normal'], entities)
            if response['limitations']:
                raise ValueError('有未决限定不能输出当前关系候选')
        else:
            if response['normal'] is not None or not response['limitations']:
                raise ValueError('待复核须保留限制且不得提供去掉限定的候选')
            seen = set()
            for limit in response['limitations']:
                if (not isinstance(limit, dict) or set(limit) != {'kind', 'quote'}
                        or not isinstance(limit['kind'], str) or limit['kind'] not in LIMITATIONS
                        or limit['kind'] in seen or not isinstance(limit['quote'], str) or not limit['quote']
                        or limit['quote'] not in case['target']['quote']):
                    raise ValueError('限定未绑定原文或重复')
                seen.add(limit['kind'])
    except (ValueError, KeyError, TypeError) as error:
        return dict(status='invalid_response', error=str(error), requiresSemanticReview=True)
    differences = []
    if response['status'] != case['expectedStatus']:
        differences.append('status')
    if response['normal'] != case['expectedNormal']:
        if isinstance(response['normal'], dict) and isinstance(case['expectedNormal'], dict):
            differences.extend(k for k in case['expectedNormal'] if response['normal'][k] != case['expectedNormal'][k])
        else:
            differences.append('normal')
    if {x['kind'] for x in response['limitations']} != set(case['expectedLimitations']):
        differences.append('limitations')
    return dict(status='mismatched' if differences else ('mapped' if response['status'] == 'candidate' else 'abstained'),
                differences=differences, requiresSemanticReview=True, productionEnablement=False)


def run(output):
    fixture = load_cases()
    config = writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    prompt = PROMPT.read_text()
    if 'json' not in prompt.lower():
        raise ValueError('提示缺少 JSON 声明')
    report = dict(schemaVersion='context-candidate-mapping/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(), fixtureSha256=sha(FIXTURE),
                  promptSha256=sha(PROMPT), harnessSha256=sha(Path(__file__)), bindings=fixture['bindings'],
                  contractValidatorSha256=sha(ROOT/'test_support/context_scene_relations.py'),
                  checkpointSha256=sha(ROOT/'test_support/context_claim_pipeline.py'),
                  model={k:config[k] for k in ('route','model')}, scope=fixture['scope'],
                  callLimit=len(fixture['cases']), actualCalls=0, cases=[], acceptance=False,
                  productionEnablement=False, reviewRecordsWritten=0, formalSessionWrites=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, report, create=True)
    recorder = _gateway(config, report['callLimit'])

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            save_checkpoint(output, report)
        except OSError as error:
            raise CheckpointError('证据保存失败，停止调用') from error

    blocked = False
    for case in fixture['cases']:
        row = dict(caseId=case['id'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json([dict(role='system',content=prompt),
                    dict(role='user',content=json.dumps(model_input(case, fixture['entities']),ensure_ascii=False))])
            finally:
                row['calls'].extend(recorder.calls[before:])
                save()
            try:
                row.update(assess(parse_json_content(response.content), case, fixture['entities']))
            except (ValueError, LlmError) as error:
                row.update(status='invalid_response', error=str(error))
        except CheckpointError:
            raise
        except LlmError as error:
            row.update(status='model_error', error=str(error))
            blocked = True
        except Exception as error:
            row.update(status='harness_error', error=str(error))
            blocked = True
        save()
        print(json.dumps(dict(caseId=row['caseId'],status=row['status']),ensure_ascii=False),flush=True)
    report['status'] = 'blocked' if blocked else 'completed'
    report['summary'] = {s:sum(r['status'] == s for r in report['cases']) for s in
                         ('mapped','abstained','mismatched','invalid_response','model_error','harness_error','not_run')}
    save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    _load_env(ROOT/'.env')
    print(json.dumps(run(args.output)['summary'],ensure_ascii=False))
