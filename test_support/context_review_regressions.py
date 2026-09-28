"""Bounded scene-review prompt comparison on official longform evidence."""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import parse_json_content, writer_config_from_env
from open_story_engine.prompts import render_prompt
from open_story_engine.reader_scene_review import SceneReviewError, validate_scene_review
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import ROOT, _load_env


FIXTURE = ROOT / 'test_support/fixtures/context-review-regressions-2026-09-25.json'
DEFAULT_OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-review-regressions-2026-09-25.json'


def assess(review, case, evidence):
    """Keep malformed reviews, false positives and missed violations distinct."""
    expected = {tuple(item) for item in case['expectedIssues']}
    try:
        paragraphs = {f'P{i+1}' for i, _ in enumerate(case['draft'].split('\n\n'))}
        checks = review.get('sceneChecks')
        if (not isinstance(review.get('issues'), list) or not isinstance(checks, list)
                or len(checks) != len(paragraphs)
                or {c.get('paragraphId') for c in checks if isinstance(c, dict)} != paragraphs
                or any(not isinstance(c, dict)
                       or c.get('playerDecision') not in {'none', 'authorized', 'overreach'}
                       or c.get('background') not in {'none', 'supported', 'unsupported', 'contradicted'}
                       or not isinstance(c.get('sources'), list) for c in checks)):
            raise ValueError('场景审核覆盖或字段不完整')
        try:
            validate_scene_review(copy.deepcopy(review), case['draft'], evidence)
            violations, unlocated = [], []
        except SceneReviewError as error:
            violations, unlocated = error.violations, error.unlocated
        actual = {(item['paragraphId'], item['type']) for item in violations}
        return {
            'status': 'matched' if actual == expected and not unlocated else 'mismatch',
            'actualIssues': sorted(actual), 'missedIssues': sorted(expected - actual),
            'extraIssues': sorted(actual - expected), 'unlocated': unlocated,
        }
    except (ValueError, TypeError, AttributeError) as error:
        return {'status': 'invalid_review', 'error': str(error)}


def run(output, *, current_only=False):
    fixture = json.loads(FIXTURE.read_text())
    source = ROOT / fixture['sourceArtifact']
    if hashlib.sha256(source.read_bytes()).hexdigest() != fixture['sourceSha256']:
        raise ValueError('原始失败证据已变化，禁止静默替换固定样本来源')
    config = writer_config_from_env()
    if any(not config.get(key) for key in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        'schemaVersion': 'context-review-regressions/0.1', 'status': 'incomplete',
        'recordedAt': datetime.now(timezone.utc).isoformat(),
        'fixture': str(FIXTURE.relative_to(ROOT)),
        'fixtureSha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'boundary': fixture['boundary'], 'formalSessionWrites': 0, 'jevCalls': 0,
        'model': {'route': config['route'], 'model': config['model']}, 'cases': [],
    }
    with output.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    arms = [('current', render_prompt('reader.scene_review'))]
    if not current_only:
        arms.insert(0, ('baseline', fixture['baselineSceneReviewPrompt']))
    recorder = _gateway(config, len(fixture['cases']) * len(arms))
    for case in fixture['cases']:
        for arm, system in arms:
            payload = {
                'input': fixture['input'], 'requirements': fixture['requirements'],
                'sceneEvidence': fixture['sceneEvidence'],
                'draft': {f'P{i+1}': p for i, p in enumerate(case['draft'].split('\n\n'))},
            }
            messages = [{'role': 'system', 'content': system},
                        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]
            record = {'caseId': case['id'], 'arm': arm, 'expectedIssues': case['expectedIssues']}
            try:
                completion = recorder.complete_json(messages)
                record.update(assess(parse_json_content(completion.content), case, fixture['sceneEvidence']))
            except Exception as error:
                record.update(status='model_error', error=str(error), errorType=type(error).__name__)
            record['call'] = recorder.calls[-1]
            report['cases'].append(record)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report['status'] = 'completed'
    report['summary'] = {
        arm: {status: sum(r['arm'] == arm and r['status'] == status for r in report['cases'])
              for status in ('matched', 'mismatch', 'invalid_review', 'model_error')}
        for arm, _ in arms
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--current-only', action='store_true', help='仅验证有新改动的当前提示，保留已有基线结果')
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    report = run(args.output, current_only=args.current_only)
    print(json.dumps({'output': str(args.output), 'summary': report['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
