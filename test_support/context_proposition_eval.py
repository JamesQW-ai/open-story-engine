"""Evaluate support only on frozen reviewed propositions, not automatic extraction."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from open_story_engine.llm import LlmError, parse_json_content, writer_config_from_env
from open_story_engine.reader_scene_review import grounding_input_evidence
from test_support.context_claim_pipeline import CheckpointError, save_checkpoint, validate_verification
from test_support.context_planner_smoke import _gateway
from test_support.context_prose_ab import ROOT, _load_env
from test_support.context_semantic_eval import FIXTURE, load_fixture, sha

LABELS = ROOT / 'test_support/fixtures/context-proposition-labels-2026-09-26.json'
PROMPT = ROOT / 'test_support/prompts/context_proposition_support.md'
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-proposition-eval-2026-09-26.json'


def load_labels(path=LABELS):
    fixture, template = load_fixture()
    labels = json.loads(path.read_text())
    if labels['fixtureSha256'] != sha(FIXTURE) or labels['sourceSha256'] != fixture['sourceSha256']:
        raise ValueError('命题标注未绑定当前固定稿和来源')
    cases = {c['id']: c for c in fixture['cases']}
    seen = set()
    evidence = grounding_input_evidence(template)
    for annotated in labels['cases']:
        cid = annotated['caseId']
        if cid not in cases or cid in seen:
            raise ValueError('标注案例缺失、重复或未知')
        seen.add(cid)
        case = cases[cid]
        if annotated['draftSha256'] != hashlib.sha256(case['draft'].encode()).hexdigest():
            raise ValueError('命题标注原稿变化')
        covered, ids = set(), set()
        for prop in annotated['propositions']:
            if (not isinstance(prop['id'], str) or not prop['id'] or prop['id'] in ids
                    or type(prop['start']) is not int or type(prop['end']) is not int
                    or not 0 <= prop['start'] < prop['end'] <= len(case['draft'])
                    or case['draft'][prop['start']:prop['end']] != prop['quote']
                    or prop['paragraphId'] != f'P{case["draft"][:prop["start"]].count(chr(10) * 2) + 1}'
                    or '\n\n' in prop['quote']
                    or not isinstance(prop['statement'], str) or not prop['statement'].strip()
                    or not isinstance(prop['rationale'], str) or not prop['rationale'].strip()
                    or prop['expectedVerdict'] not in ('supported', 'unsupported', 'contradicted', 'nonfactual', 'needs_review')
                    or not isinstance(prop['allowedBases'], list) or not prop['allowedBases']
                    or prop['expectedBasis'] not in prop['allowedBases']):
                raise ValueError('命题定位或标注字段无效')
            ids.add(prop['id'])
            covered.update(range(prop['start'], prop['end']))
            if prop['expectedVerdict'] == 'nonfactual':
                if any(b not in ('authorized_action', 'ordinary_reaction', 'expressed_uncertainty') for b in prop['allowedBases']):
                    raise ValueError('非事实边界无效')
            elif prop['allowedBases'] != ['']:
                raise ValueError('事实或待复核命题不能预设非事实豁免')
            if prop['expectedVerdict'] in ('supported', 'contradicted') and not prop['evidence']:
                raise ValueError('支持或矛盾标注必须有来源')
            for ref in prop['evidence']:
                if ref['id'] not in evidence or len(ref['quote'].strip()) < 4 or ref['quote'] not in evidence[ref['id']]:
                    raise ValueError('标注来源无效')
        if any(not char.isspace() and i not in covered for i, char in enumerate(case['draft'])):
            raise ValueError('标注遗漏原文覆盖；字符覆盖仍不代表语义完整')
    if seen != set(cases):
        raise ValueError('标注案例缺失')
    return labels, cases, template


def model_input(case, annotation, template):
    # Explicit allowlist: no verdict, rationale, source selection, or expected label.
    return {'draft': case['draft'], 'input': case['input'],
            'statements': {p['id']: {k: p[k] for k in ('statement', 'quote', 'paragraphId')}
                           for p in annotation['propositions']},
            'publicEvidence': grounding_input_evidence(template)}


def assess(data, payload, annotation):
    if (not isinstance(data, dict) or set(data) != {'checks'} or not isinstance(data['checks'], list)
            or any(not isinstance(c, dict) or set(c) != {'id', 'verdict', 'basis', 'sources', 'reason'} for c in data['checks'])):
        return {'status': 'invalid_review', 'error': '来源核对字段无效'}
    try:
        validate_verification({'checks': [dict(c, extraction='faithful') for c in data['checks']]}, payload)
        if any(c['verdict'] == 'contradicted' and not c['sources'] for c in data['checks']):
            raise ValueError('矛盾判定必须给出相反来源')
    except ValueError as error:
        return {'status': 'invalid_review', 'error': str(error)}
    checks = {c['id']: c for c in data['checks']}
    mismatches, pending, matched, confusion = [], [], [], []
    for prop in annotation['propositions']:
        actual = checks[prop['id']]
        item = dict(id=prop['id'], statement=prop['statement'], expectedVerdict=prop['expectedVerdict'],
                    actualVerdict=actual['verdict'], allowedBases=prop['allowedBases'], actualBasis=actual['basis'])
        if prop['expectedVerdict'] == 'needs_review':
            pending.append(item)
            continue
        confusion.append(dict(expected=prop['expectedVerdict'], actual=actual['verdict']))
        if actual['verdict'] != prop['expectedVerdict'] or actual['basis'] not in prop['allowedBases']:
            mismatches.append(item)
        else:
            matched.append(prop['id'])
    return dict(status='mismatched' if mismatches else ('annotation_pending' if pending else 'matched'),
                matchedPropositions=matched, mismatches=mismatches, pendingAnnotations=pending,
                verdictPairs=confusion, requiresSemanticReview=True)


def run(output):
    labels, cases, template = load_labels()
    config = writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')):
        raise ValueError('缺少模型配置')
    prompt = PROMPT.read_text()
    if 'json' not in prompt.lower():
        raise ValueError('提示缺少 JSON 声明')
    report = dict(schemaVersion='context-proposition-eval/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(), labelsSha256=sha(LABELS),
                  fixtureSha256=sha(FIXTURE), sourceSha256=labels['sourceSha256'], promptSha256=sha(PROMPT),
                  harnessSha256=sha(Path(__file__)), sharedValidatorSha256=sha(ROOT/'test_support/context_claim_pipeline.py'),
                  model={k: config[k] for k in ('route', 'model')},
                  scope='经复核固定命题的来源核对专项；不包含自动提取，不是完整链路验收。',
                  callLimit=len(cases), actualCalls=0, cases=[], acceptance=False, productionEnablement=False,
                  formalSessionWrites=0, temporarySessionWrites=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(output, report, create=True)
    recorder = _gateway(config, report['callLimit'])

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            save_checkpoint(output, report)
        except OSError as error:
            raise CheckpointError('证据保存失败，停止评测') from error

    blocked = False
    for annotation in labels['cases']:
        row = dict(caseId=annotation['caseId'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        payload = model_input(cases[annotation['caseId']], annotation, template)
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json([dict(role='system', content=prompt),
                                                   dict(role='user', content=json.dumps(payload, ensure_ascii=False))])
            finally:
                if len(recorder.calls) > before:
                    row['calls'].append(recorder.calls[-1])
                save()
            try:
                row.update(assess(parse_json_content(response.content), payload, annotation))
            except (ValueError, LlmError) as error:
                row.update(status='invalid_review', error=str(error))
        except CheckpointError:
            raise
        except LlmError as error:
            row.update(status='model_error', error=str(error))
            blocked = True
        except Exception as error:
            row.update(status='harness_error', error=str(error))
            blocked = True
        save()
        print(json.dumps({k: row[k] for k in ('caseId', 'status')}, ensure_ascii=False), flush=True)
    report['status'] = 'blocked' if blocked else 'completed'
    report['summary'] = {s: sum(r['status'] == s for r in report['cases']) for s in
                         ('matched', 'mismatched', 'annotation_pending', 'invalid_review', 'model_error', 'harness_error', 'not_run')}
    report['propositionCounts'] = dict(matched=sum(len(r.get('matchedPropositions', [])) for r in report['cases']),
                                     mismatched=sum(len(r.get('mismatches', [])) for r in report['cases']),
                                     pending=sum(len(r.get('pendingAnnotations', [])) for r in report['cases']))
    save()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    _load_env(ROOT / '.env')
    print(json.dumps(run(args.output)['summary'], ensure_ascii=False))
