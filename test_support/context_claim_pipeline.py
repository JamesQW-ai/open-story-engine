"""Experimental extraction/verification separation on immutable longform prose."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from open_story_engine.llm import LlmError, parse_json_content, writer_config_from_env
from open_story_engine.reader_scene_review import grounding_claims, grounding_input_evidence
from test_support.context_prose_ab import ROOT, _load_env
from test_support.context_planner_smoke import _gateway
from test_support.context_semantic_eval import FIXTURE, load_fixture, sha

PROMPTS = {name: ROOT / f'test_support/prompts/context_claim_{name}.md' for name in ('extract', 'verify')}
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-claim-pipeline-2026-09-25.json'


class CheckpointError(RuntimeError):
    """Stop evaluation when evidence cannot be persisted."""


def save_checkpoint(output, report, *, create=False):
    # Publish only a complete file; initial publication must not overwrite evidence.
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                prefix='.' + output.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if create:
            os.link(temporary, output)
        else:
            os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def extraction_input(case):
    return {'draft': case['draft'], 'units': grounding_claims(case['draft'])}


def validate_extraction(data, payload):
    if not isinstance(data, dict) or set(data) != {'units'} or not isinstance(data['units'], list):
        raise ValueError('提取只允许 units，不允许审核结论')
    units, seen, statements = payload['units'], set(), {}
    for unit in data['units']:
        if (not isinstance(unit, dict) or set(unit) != {'id', 'parts'}
                or not isinstance(unit['id'], str) or unit['id'] not in units or unit['id'] in seen
                or not isinstance(unit['parts'], list) or not unit['parts']):
            raise ValueError('提取单元不存在、重复或格式无效')
        seen.add(unit['id'])
        parts = unit['parts']
        for part in parts:
            if (not isinstance(part, dict) or set(part) != {'quote', 'statement'}
                    or any(not isinstance(part[k], str) or not part[k].strip() for k in ('quote', 'statement'))):
                raise ValueError('提取只允许逐字定位及待核命题')
        if ''.join(part['quote'] for part in parts) != units[unit['id']]['claim']:
            raise ValueError('提取未逐字完整覆盖单元：' + unit['id'])
        for index, part in enumerate(parts, 1):
            statements[unit['id'] + '-F' + str(index)] = {
                **part, 'paragraphId': units[unit['id']]['paragraphId'],
            }
    if seen != set(units):
        raise ValueError('提取遗漏原文单元')
    return statements


def verification_input(case, statements, template):
    # No previous verdict, hidden state, full bundle, or stale turn intent.
    return {'draft': case['draft'], 'input': case['input'], 'statements': statements,
            'publicEvidence': grounding_input_evidence(template)}


def validate_verification(data, payload):
    checks = data.get('checks') if isinstance(data, dict) else None
    if not isinstance(checks, list):
        raise ValueError('核对缺少 checks')
    seen, violations, extraction_issues = set(), [], []
    statements, evidence = payload['statements'], payload['publicEvidence']
    for check in checks:
        if (not isinstance(check, dict) or not isinstance(check.get('id'), str)
                or check['id'] not in statements or check['id'] in seen
                or check.get('extraction') not in ('faithful', 'lossy', 'uncertain')
                or check.get('verdict') not in ('supported', 'unsupported', 'contradicted', 'nonfactual')
                or not isinstance(check.get('reason'), str) or not check['reason'].strip()
                or not isinstance(check.get('sources'), list)):
            raise ValueError('核对编号或字段无效')
        seen.add(check['id'])
        for ref in check['sources']:
            if (not isinstance(ref, dict) or not isinstance(ref.get('id'), str)
                    or not isinstance(ref.get('quote'), str) or len(ref['quote'].strip()) < 4
                    or ref['id'] not in evidence or ref['quote'] not in evidence[ref['id']]):
                raise ValueError('核对引用不在本次公开证据中')
        if check['verdict'] == 'nonfactual':
            if check.get('basis') not in ('authorized_action', 'ordinary_reaction', 'expressed_uncertainty') or check['sources']:
                raise ValueError('非事实判断缺少边界或混入来源')
        elif check.get('basis') != '':
            raise ValueError('事实命题不能附带非事实豁免')
        if check['verdict'] == 'supported' and not check['sources']:
            raise ValueError('事实通过必须提供公开来源')
        statement = statements[check['id']]
        finding = {'id': check['id'], 'paragraphId': statement['paragraphId'],
                   'claim': statement['quote'], 'statement': statement['statement'], 'reason': check['reason']}
        if check['extraction'] != 'faithful':
            extraction_issues.append(finding)
        if check['verdict'] in {'unsupported', 'contradicted'}:
            violations.append(finding)
    if seen != set(statements):
        raise ValueError('核对遗漏待核命题')
    return violations, extraction_issues


def assess(data, payload, case):
    try:
        violations, extraction_issues = validate_verification(data, payload)
    except ValueError as error:
        return {'status': 'invalid_review', 'error': str(error)}
    def hits(target, violation):
        return target['paragraphId'] == violation['paragraphId'] and target['quote'] in violation['claim']

    detected = [t for t in case['targets'] if any(
        hits(t, v) for v in violations)]
    missed = [t for t in case['targets'] if t not in detected]
    # Targets are not exhaustive truth labels for the rest of a negative draft.
    # Preserve unmatched rejections for review rather than declaring them false.
    additional = [v for v in violations if not any(hits(t, v) for t in case['targets'])]
    if extraction_issues:
        status = 'extraction_error'
    elif case['expected'] == 'allow':
        status = 'false_positive' if violations else 'matched'
    elif missed:
        status = 'false_negative'
    else:
        status = 'unexpected_rejection' if additional else 'matched'
    return {'status': status, 'detectedTargets': detected, 'missedTargets': missed,
            'additionalRejections': additional, 'extractionIssues': extraction_issues,
            'requiresReview': bool(extraction_issues or additional), 'violations': violations}


def run(output, *, fixture_path=FIXTURE):
    fixture, template = load_fixture(fixture_path)
    config = writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    prompts = {stage: path.read_text() for stage, path in PROMPTS.items()}
    if any('json' not in prompt.lower() for prompt in prompts.values()):
        raise ValueError('JSON 输出模式要求两个阶段的提示显式包含 JSON')
    report = {'schemaVersion': 'context-claim-pipeline/0.2', 'status': 'incomplete',
              'recordedAt': datetime.now(timezone.utc).isoformat(), 'fixtureSha256': sha(fixture_path),
              'sourceSha256': fixture['sourceSha256'], 'harnessSha256': sha(Path(__file__)),
              'promptHashes': {k: hashlib.sha256(v.encode()).hexdigest() for k, v in prompts.items()},
              'model': {'route': config['route'], 'model': config['model']},
              'callLimit': 2 * len(fixture['cases']), 'actualCalls': 0,
              'scope': '正文提取和公开事实支持专项；同一模型的独立请求，不构成独立模型证明或全链验收。',
              'formalSessionWrites': 0, 'temporarySessionWrites': 0, 'jevCalls': 0,
              'acceptance': False, 'productionEnablement': False, 'cases': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, report, create=True)
    recorder = _gateway(config, report['callLimit'])

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            save_checkpoint(output, report)
        except OSError as error:
            raise CheckpointError('检查点写入失败，停止评测以保护证据') from error

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
        row = {'caseId': case['id'], 'expected': case['expected'], 'targets': case['targets'],
               'status': 'incomplete', 'calls': []}
        report['cases'].append(row)
        if blocked:
            row.update(status='not_run', reason=blocked)
            save()
            continue
        stage = 'extract'
        try:
            payload = extraction_input(case)
            response = request(stage, payload, row)
            try:
                statements = validate_extraction(parse_json_content(response.content), payload)
            except (ValueError, LlmError) as error:
                row.update(status='invalid_extraction', error=str(error))
            else:
                row['statements'] = statements
                stage = 'verify'
                payload = verification_input(case, statements, template)
                response = request(stage, payload, row)
                try:
                    row.update(assess(parse_json_content(response.content), payload, case))
                except (ValueError, LlmError) as error:
                    row.update(status='invalid_review', error=str(error))
        except CheckpointError:
            raise
        except LlmError as error:
            row.update(status='model_error', failureStage=stage, error=str(error), errorType=type(error).__name__)
            blocked = 'blocked_model_error'
        except Exception as error:
            row.update(status='harness_error', failureStage=stage, error=str(error), errorType=type(error).__name__)
            blocked = 'blocked_harness_error'
        save()
        print(json.dumps({k: row[k] for k in ('caseId', 'status')}, ensure_ascii=False), flush=True)
    report['status'] = blocked or 'completed'
    report['summary'] = {s: sum(r['status'] == s for r in report['cases']) for s in (
        'matched', 'false_positive', 'false_negative', 'unexpected_rejection',
        'invalid_extraction', 'extraction_error', 'invalid_review',
        'model_error', 'harness_error', 'not_run')}
    report['scoringCounts'] = {
        'scoredCases': sum('detectedTargets' in r for r in report['cases']),
        'unscoredCases': sum('detectedTargets' not in r for r in report['cases']),
        'detectedTargets': sum(len(r.get('detectedTargets', [])) for r in report['cases']),
        'missedTargets': sum(len(r.get('missedTargets', [])) for r in report['cases']),
        'additionalRejections': sum(len(r.get('additionalRejections', [])) for r in report['cases']),
        'casesRequiringReview': sum(bool(r.get('requiresReview')) for r in report['cases']),
    }
    save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    print(json.dumps(run(args.output)['summary'], ensure_ascii=False))
