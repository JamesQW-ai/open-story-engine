"""Bounded experiment: program-owned spans, blind fidelity, then public support."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import LlmError, parse_json_content, writer_config_from_env
from open_story_engine.reader_scene_review import grounding_input_evidence
from test_support.context_claim_pipeline import CheckpointError, assess, save_checkpoint
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import ROOT, _load_env
from test_support.context_semantic_eval import FIXTURE, load_fixture, sha

PROMPTS = {s: ROOT / f'test_support/prompts/context_indexed_{s}.md' for s in ('extract', 'fidelity', 'support')}
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-indexed-pipeline-2026-09-26.json'
STATUSES = ('matched', 'false_positive', 'false_negative', 'unexpected_rejection', 'ambiguous_rejection',
            'invalid_extraction', 'invalid_fidelity', 'extraction_error', 'invalid_review',
            'model_error', 'harness_error', 'not_run')


def indexed_input(case):
    draft, units, offset = case['draft'], {}, 0
    for paragraph, text in enumerate(draft.split('\n\n'), 1):
        for number, match in enumerate(re.finditer(r'.+?(?:[，,；;。！？!?]+[”」』]*|——|$)', text, re.S), 1):
            units[f'P{paragraph}-U{number}'] = dict(quote=match.group(), paragraphId=f'P{paragraph}',
                                                 start=offset + match.start(), end=offset + match.end())
        offset += len(text) + 2
    if not units:
        raise ValueError('原稿没有可提取单元')
    return {'draft': draft, 'units': units}


def extract_statements(data, payload):
    if not isinstance(data, dict) or set(data) != {'units'} or not isinstance(data['units'], list):
        raise ValueError('提取仅允许 units')
    seen, statements = set(), {}
    for unit in data['units']:
        if (not isinstance(unit, dict) or set(unit) != {'id', 'statements'}
                or not isinstance(unit['id'], str) or unit['id'] not in payload['units'] or unit['id'] in seen
                or not isinstance(unit['statements'], list) or not unit['statements']
                or any(not isinstance(s, str) or not s.strip() for s in unit['statements'])):
            raise ValueError('提取单元或命题格式无效')
        seen.add(unit['id'])
        for index, statement in enumerate(unit['statements'], 1):
            statements[f'{unit["id"]}-F{index}'] = {
                **payload['units'][unit['id']], 'unitId': unit['id'], 'statement': statement,
            }
    if seen != set(payload['units']):
        raise ValueError('提取遗漏单元')
    return statements


def fidelity_input(payload, statements):
    return {**payload, 'statements': statements}


def fidelity_issues(data, payload):
    if not isinstance(data, dict) or set(data) != {'units'} or not isinstance(data['units'], list):
        raise ValueError('忠实度核对仅允许 units')
    seen, issues = set(), []
    for unit in data['units']:
        if (not isinstance(unit, dict) or set(unit) != {'id', 'verdict', 'reason'}
                or not isinstance(unit['id'], str) or unit['id'] not in payload['units'] or unit['id'] in seen
                or unit['verdict'] not in ('faithful', 'lossy', 'uncertain')
                or not isinstance(unit['reason'], str) or not unit['reason'].strip()):
            raise ValueError('忠实度核对编号或字段无效')
        seen.add(unit['id'])
        if unit['verdict'] != 'faithful':
            issues.append(unit)
    if seen != set(payload['units']):
        raise ValueError('忠实度核对遗漏单元')
    return issues


def support_input(case, statements, template):
    return {'draft': case['draft'], 'input': case['input'], 'statements': statements,
            'publicEvidence': grounding_input_evidence(template)}


def assess_support(data, payload, case):
    # Called only after the separate blind fidelity gate passed. No verdict override.
    if (not isinstance(data, dict) or set(data) != {'checks'} or not isinstance(data['checks'], list)
            or any(not isinstance(c, dict) or set(c) != {'id', 'verdict', 'basis', 'sources', 'reason'}
                   for c in data['checks'])):
        return {'status': 'invalid_review', 'error': '来源核对只允许命题支持字段'}
    result = assess({'checks': [dict(c, extraction='faithful') for c in data['checks']]}, payload, case)
    ambiguous = [v for v in result.get('violations', []) if sum(
        s['unitId'] == payload['statements'][v['id']]['unitId'] for s in payload['statements'].values()) > 1]
    if ambiguous:
        result.update(ambiguousRejections=ambiguous, requiresReview=True)
        if result['status'] == 'matched':
            result['status'] = 'ambiguous_rejection'
    return result


def run(output, *, fixture_path=FIXTURE):
    fixture, template = load_fixture(fixture_path)
    config = writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    prompts = {s: path.read_text() for s, path in PROMPTS.items()}
    if any('json' not in text.lower() for text in prompts.values()):
        raise ValueError('各阶段提示须显式声明 JSON')
    report = dict(schemaVersion='context-indexed-pipeline/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(), fixtureSha256=sha(fixture_path),
                  sourceSha256=fixture['sourceSha256'], harnessSha256=sha(Path(__file__)),
                  sharedScorerSha256=sha(ROOT / 'test_support/context_claim_pipeline.py'),
                  promptHashes={s: sha(path) for s, path in PROMPTS.items()},
                  model={k: config[k] for k in ('route', 'model')}, callLimit=3 * len(fixture['cases']),
                  actualCalls=0, formalSessionWrites=0, temporarySessionWrites=0, jevCalls=0,
                  acceptance=False, productionEnablement=False, cases=[])
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, report, create=True)
    recorder = _gateway(config, report['callLimit'])
    report['requestSettings'] = dict(maxTokens=recorder.gateway.max_tokens,
                                   reasoningEffort=recorder.gateway.reasoning_effort,
                                   timeoutSeconds=recorder.gateway.timeout_seconds,
                                   transportFallback=False)

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            save_checkpoint(output, report)
        except OSError as error:
            raise CheckpointError('检查点失败，停止评测') from error

    def request(stage, payload, row):
        before = len(recorder.calls)
        try:
            return recorder.complete_json([{'role': 'system', 'content': prompts[stage]},
                                           {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}])
        finally:
            if len(recorder.calls) > before:
                row['calls'].append({'stage': stage, **recorder.calls[-1]})
            save()

    blocked = None
    for case in fixture['cases']:
        row = dict(caseId=case['id'], expected=case['expected'], targets=case['targets'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row.update(status='not_run', reason=blocked)
            save()
            continue
        stage, payload = 'extract', indexed_input(case)
        try:
            response = request(stage, payload, row)
            try:
                statements = extract_statements(parse_json_content(response.content), payload)
            except (ValueError, LlmError) as error:
                row.update(status='invalid_extraction', error=str(error))
            else:
                row['statements'] = statements
                stage = 'fidelity'
                blind = fidelity_input(payload, statements)
                response = request(stage, blind, row)
                try:
                    issues = fidelity_issues(parse_json_content(response.content), blind)
                except (ValueError, LlmError) as error:
                    row.update(status='invalid_fidelity', error=str(error))
                else:
                    if issues:
                        row.update(status='extraction_error', extractionIssues=issues, requiresReview=True)
                    else:
                        stage = 'support'
                        support = support_input(case, statements, template)
                        response = request(stage, support, row)
                        try:
                            row.update(assess_support(parse_json_content(response.content), support, case))
                        except (ValueError, LlmError) as error:
                            row.update(status='invalid_review', error=str(error))
        except CheckpointError:
            raise
        except LlmError as error:
            row.update(status='model_error', failureStage=stage, error=str(error))
            blocked = 'blocked_model_error'
        except Exception as error:
            row.update(status='harness_error', failureStage=stage, error=str(error))
            blocked = 'blocked_harness_error'
        save()
        print(json.dumps({k: row[k] for k in ('caseId', 'status')}, ensure_ascii=False), flush=True)
    report['status'] = blocked or 'completed'
    report['summary'] = {s: sum(r['status'] == s for r in report['cases']) for s in STATUSES}
    save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    print(json.dumps(run(args.output)['summary'], ensure_ascii=False))
