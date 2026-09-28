"""Three bounded dialogue checks against saved official longform input."""
import argparse
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import parse_json_content, writer_config_from_env
from open_story_engine.prompts import catalog_version, render_prompt
from open_story_engine.reader_scene_review import (
    SceneReviewError, dialogue_units, grounding_claims, grounding_input_evidence,
    scene_speaker_candidates, validate_grounding, validate_knowledge_access,
    validate_scope, validate_scene_boundaries,
)
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import ROOT, _entry_samples, _load_env

SOURCE = ROOT / 'docs/evidence/context-management-2026-09-22/context-final-guard-validation-2026-09-25.json'
OUTPUT = SOURCE.with_name('context-dialogue-grounding-2026-09-25.json')


def evaluate(payload, data):
    body = '\n\n'.join(payload['draft'].values())
    evidence = grounding_input_evidence(payload)
    outcomes = {}
    for name, validate in (
        ('grounding', lambda: validate_grounding(data, payload['paragraphs'], evidence=evidence)),
        ('knowledge', lambda: validate_knowledge_access(data, body, evidence, payload['people'],
                                                       payload['playerId'], speaker_names=payload['people'])),
        ('scope', lambda: validate_scope(data, payload['requirements'], body)),
        ('boundaries', lambda: validate_scene_boundaries(data, body, payload['boundaries'])),
    ):
        try:
            validate()
            outcomes[name] = {'status': 'allowed'}
        except SceneReviewError as error:
            outcomes[name] = {'status': 'rejected', 'violations': error.violations, 'unlocated': error.unlocated}
        except ValueError as error:
            outcomes[name] = {'status': 'invalid_review', 'error': str(error)}
    return outcomes


def run(output):
    source = json.loads(SOURCE.read_text())
    template = json.loads(source['production']['calls'][5]['messages'][1]['content'])
    waiting = next(c for c in source['cases'] if c['caseId'] == 'request_then_wait')
    waiting_body = '\n\n'.join(json.loads(waiting['call']['messages'][1]['content'])['draft'].values())
    cases = [
        ('recorded_rule_and_facility', source['production']['result']['narrativeText'], ['封山令', '传事钟']),
        ('legal_wait', waiting_body, []),
        ('sourced_urgency', '“他还活着。山门快关了，请让他得到救助。”你对守门弟子说。\n\n'
                           '“你愿意担责吗？”守门弟子问。\n\n你没有回答，仍站在原地。', []),
    ]
    sample = _entry_samples(1)[0]
    config = writer_config_from_env()
    report = {'schemaVersion': 'context-dialogue-grounding/0.1', 'status': 'incomplete',
              'recordedAt': datetime.now(timezone.utc).isoformat(), 'promptVersion': catalog_version(),
              'sourceArtifact': str(SOURCE.relative_to(ROOT)), 'sourceSha256': hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              'model': {'route': config['route'], 'model': config['model']},
              'callLimit': 3, 'formalSessionWrites': 0, 'jevCalls': 0, 'acceptance': False,
              'boundary': '只验证独立grounding；两条正例为固定构造，不是新生产正文；匹配不等于全链验收。',
              'cases': []}
    with output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    recorder = _gateway(config, 3)
    for case_id, body, targets in cases:
        payload = copy.deepcopy(template)
        payload.update(draft={f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))},
                       paragraphs=grounding_claims(body), dialogueUnits=dialogue_units(body),
                       priorRepairIssues=None, repairTargets={})
        payload['people'] = scene_speaker_candidates(sample['context'], body, grounding_input_evidence(payload))
        row = {'caseId': case_id, 'expectedRejectedClaims': targets}
        try:
            response = recorder.complete_json([
                {'role': 'system', 'content': render_prompt('reader.scene_grounding')},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
            ])
            row['validations'] = evaluate(payload, parse_json_content(response.content))
            violations = [v for check in row['validations'].values() for v in check.get('violations', [])]
            row['detectedTargets'] = [t for t in targets if any(t in v['claim'] for v in violations)]
            invalid = any(check['status'] == 'invalid_review' for check in row['validations'].values())
            row['matched'] = (not invalid and (row['detectedTargets'] == targets if targets else
                              all(check['status'] == 'allowed' for check in row['validations'].values())))
        except Exception as error:
            row.update(matched=False, error=str(error), errorType=type(error).__name__)
        row['call'] = recorder.calls[-1] if recorder.calls else None
        report['cases'].append(row)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({k: v for k, v in row.items() if k != 'call'}, ensure_ascii=False), flush=True)
    report.update(status='completed', actualCalls=len(recorder.calls), matched=sum(c['matched'] for c in report['cases']))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    run(args.output)
