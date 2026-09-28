"""Experimental reviewed scene relations; not a production state authority."""
import argparse
import hashlib
import json
from pathlib import Path

from test_support.context_claim_pipeline import save_checkpoint
from test_support.context_proposition_eval import load_labels
from test_support.context_relation_audit import FIXTURE as RELATION_FIXTURE, load_inputs
from test_support.context_semantic_eval import ROOT, sha

DATA = ROOT / 'test_support/fixtures/context-scene-relations-2026-09-26.json'
REVIEW = ROOT / 'test_support/fixtures/context-scene-relations-review-2026-09-26.json'
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-scene-relations-2026-09-26.json'
VALUES = {'inside', 'outside', 'on_boundary'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _span(span, text):
    if (not isinstance(span, dict) or set(span) != {'start', 'end', 'quote'}
            or type(span['start']) is not int or type(span['end']) is not int
            or not 0 <= span['start'] < span['end'] <= len(text)
            or text[span['start']:span['end']] != span['quote']):
        raise ValueError('引文与字符位置不一致')


def _index(rows):
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in result:
            raise ValueError('实体、关系或映射编号无效/重复')
        result[row['id']] = row
    return result


def _normal(value, entities):
    keys = {'subject', 'relation', 'object', 'value', 'polarity', 'time', 'condition'}
    if (not isinstance(value, dict) or set(value) != keys
            or any(not isinstance(value[k], str) for k in keys - {'condition'})
            or value['relation'] != 'boundary_side' or value['value'] not in VALUES
            or value['polarity'] not in {'positive', 'negative'}
            or value['time'] != 'snapshot' or value['condition'] is not None
            or entities.get(value['subject'], {}).get('kind') != 'scene_person'
            or entities.get(value['object'], {}).get('kind') != 'scene_boundary'):
        raise ValueError('超出受审关系契约：主体、边界、肯否或限定无效')


def validate(data, review, evidence, originals):
    """Review is supplied by the local authoring path, never by the model."""
    if (not all(isinstance(v, dict) for v in (data, review, evidence, originals))
            or data.get('schemaVersion') != 'scene-relation-experiment/0.1'
            or review.get('schemaVersion') != 'scene-relation-review/0.1'
            or review.get('artifactDigest') != digest(data)
            or review.get('reviewer') != 'Codex' or review.get('scope') != 'offline_experiment'):
        raise ValueError('缺少绑定当前内容的离线复核记录')
    scope = data['scope']
    if (set(scope) != {'snapshotId', 'branchId', 'packageId', 'packageVersion', 'sceneId'}
            or any(not isinstance(v, str) or not v for v in scope.values())):
        raise ValueError('场景范围不完整')
    entities, facts, mappings = (_index(data[k]) for k in ('entities', 'facts', 'mappings'))
    for row in [*entities.values(), *facts.values()]:
        ref = row['source']
        if (set(ref) != {'id', 'span', 'contentSha256'} or ref['id'] not in evidence
                or not isinstance(evidence[ref['id']], str)
                or hashlib.sha256(evidence[ref['id']].encode()).hexdigest() != ref['contentSha256']):
            raise ValueError('不是当前公开来源')
        _span(ref['span'], evidence[ref['id']])
    for entity in entities.values():
        if entity['kind'] not in {'scene_person', 'scene_boundary'}:
            raise ValueError('不支持的场景实体类型')
    for fact in facts.values():
        _normal(fact['normal'], entities)
        if fact['normal']['polarity'] != 'positive':
            raise ValueError('本版事实只接受明确肯定位置，不从否定补全位置')
    for mapping in mappings.values():
        if mapping['origin'] not in originals:
            raise ValueError('正文映射缺少冻结原稿')
        original = originals[mapping['origin']]
        if (mapping['draftSha256'] != hashlib.sha256(original['draft'].encode()).hexdigest()
                or mapping['statement'] != original['statement'] or mapping['span'] != original['span']):
            raise ValueError('正文映射与冻结命题不一致')
        _span(mapping['span'], original['draft'])
        _normal(mapping['normal'], entities)
    for field, rows in (('entityIds', entities), ('factIds', facts), ('mappingIds', mappings)):
        ids = review[field]
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or not set(ids) <= rows.keys():
            raise ValueError('复核编号不存在或重复')
    # Only reviewed facts constrain a result. Conflicts invalidate the manifest.
    positions = {}
    for fid in review['factIds']:
        value = facts[fid]['normal']
        if not {value['subject'], value['object']} <= set(review['entityIds']):
            raise ValueError('关系引用了未复核的实体')
        key = (value['subject'], value['object'])
        if key in positions and positions[key] != value['value']:
            raise ValueError('同一快照的受审位置冲突')
        positions[key] = value['value']
    return entities, facts, mappings


def compare(mapping_id, data, review, evidence, originals, active_scope):
    pending = lambda reason: dict(verdict='needs_review', reason=reason, sources=[], proseAcceptance=False)
    if not isinstance(mapping_id, str) or not mapping_id:
        return pending('映射编号无效')
    try:
        entities, facts, mappings = validate(data, review, evidence, originals)
    except (ValueError, KeyError, TypeError) as error:
        return pending(str(error))
    if active_scope != data['scope']:
        return pending('快照、分支、包或场景变化；必须重新生成并复核关系')
    if mapping_id not in mappings or mapping_id not in review['mappingIds']:
        return pending('正文映射尚未复核')
    claim = mappings[mapping_id]['normal']
    if not {claim['subject'], claim['object']} <= set(review['entityIds']):
        return pending('正文实体尚未复核')
    matching = [facts[fid] for fid in review['factIds']
                if all(facts[fid]['normal'][k] == claim[k] for k in ('subject', 'relation', 'object', 'time', 'condition'))]
    if not matching:
        return pending('没有同范围的受审来源关系')
    agrees = (matching[0]['normal']['value'] == claim['value']) == (claim['polarity'] == 'positive')
    return dict(verdict='supported' if agrees else 'contradicted', reason='reviewed_boundary_relation_comparison',
                sources=[f['source'] for f in matching], proseAcceptance=False)


def load_contract():
    fixture, snapshot, evidence = load_inputs()
    labels, cases, payload = load_labels()
    originals = {}
    for case in fixture['cases']:
        text = case['statement']
        originals[case['id']] = dict(draft=text, statement=text, span=dict(start=0, end=len(text), quote=text))
    label = next(p for a in labels['cases'] if a['caseId'] == 'recorded_v11'
                 for p in a['propositions'] if p['id'] == 'C10')
    originals['recorded_v11:C10'] = dict(draft=cases['recorded_v11']['draft'], statement=label['statement'],
                                        span={k:label[k] for k in ('start', 'end', 'quote')})
    data, review = json.loads(DATA.read_text()), json.loads(REVIEW.read_text())
    scope = dict(snapshotId=snapshot.snapshot_id, branchId=snapshot.branch_id,
                 packageId=payload['contextProjection']['package']['id'],
                 packageVersion=payload['contextProjection']['package']['version'],
                 sceneId=payload['contextProjection']['hardConstraints']['currentBeat']['id'])
    if data['relationFixtureSha256'] != sha(RELATION_FIXTURE) or data['scope'] != scope:
        raise ValueError('关系契约与已绑定场景不一致')
    validate(data, review, evidence, originals)
    return data, review, evidence, originals, scope


def audit():
    data, review, evidence, originals, scope = load_contract()
    fixture, _, _ = load_inputs()
    expected = {c['id']:c['expectedVerdict'] for c in fixture['cases']}
    expected['recorded_v11:C10'] = 'contradicted'
    rows = [dict(id=m['id'], origin=m['origin'], expected=expected[m['origin']],
                 result=compare(m['id'], data, review, evidence, originals, scope)) for m in data['mappings']]
    return dict(schemaVersion='scene-relation-audit/0.1', dataSha256=sha(DATA), reviewSha256=sha(REVIEW),
                harnessSha256=sha(Path(__file__)), scope=scope, cases=rows,
                summary=dict(reviewedMappings=len(rows), matched=sum(r['result']['verdict'] == r['expected'] for r in rows),
                             unmappedContrasts=len(set(expected) - {m['origin'] for m in data['mappings']})),
                automaticExtractionVerified=False, modelCalls=0, formalSessionWrites=0, jevCalls=0,
                acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(args.output, result, create=True)
    print(json.dumps(result['summary'], ensure_ascii=False))
