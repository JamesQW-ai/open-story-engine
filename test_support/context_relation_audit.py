"""Offline capability audit: exact public state IDs, never inferred text relations."""
import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from open_story_engine.content import expand_state_visibility, load_runtime_story_package
from open_story_engine.reader_scene_review import grounding_input_evidence
from test_support.context_claim_pipeline import save_checkpoint
from test_support.context_proposition_eval import LABELS
from test_support.context_semantic_eval import FIXTURE as PROSE_FIXTURE, ROOT, load_fixture, sha

FIXTURE = ROOT / 'test_support/fixtures/context-relation-cases-2026-09-26.json'
OUTPUT = ROOT / 'docs/evidence/context-management-2026-09-22/context-relation-audit-2026-09-26.json'
PUBLIC = {'player_known', 'public_world_fact'}


@dataclass(frozen=True)
class PublicLocation:
    subject: str
    value: str
    paths: tuple


@dataclass(frozen=True)
class Snapshot:
    # Only constructed from the verified artifact and package; no model fields.
    snapshot_id: str
    branch_id: str
    locations: tuple


def public_locations(state, visibility):
    """Internal adapter: accept explicit public leaves, not all reviewer state."""
    player = state.get('playerCharacterId')
    if (visibility.get('/playerCharacterId') not in PUBLIC
            or not isinstance(player, str) or not player):
        return ()
    escaped = player.replace('~', '~0').replace('/', '~1')
    candidates = [('/playerLocationId', state.get('playerLocationId')),
                  ('/characterLocationIds/' + escaped, state.get('characterLocationIds', {}).get(player))]
    visible = [(path, value) for path, value in candidates if visibility.get(path) in PUBLIC]
    if not visible:
        return ()
    if any(not isinstance(value, str) or not value for _, value in visible):
        raise ValueError('公开位置字段无效')
    if len({value for _, value in visible}) != 1:
        raise ValueError('公开玩家位置字段冲突，不能选择一个字段放行')
    return (PublicLocation(player, visible[0][1], tuple(path for path, _ in visible)),)


def load_inputs(path=FIXTURE):
    fixture = json.loads(path.read_text())
    prose, payload = load_fixture()
    package_path = ROOT / fixture['packageArtifact']
    required = {str(p.relative_to(ROOT)) for p in (
        PROSE_FIXTURE, LABELS, ROOT / prose['sourceArtifact'], package_path,
        package_path.parent / 'modules/package-index.json', package_path.parent / 'modules/state-schema.json',
        ROOT / fixture['previousReportArtifact'])}
    if set(fixture['bindings']) != required:
        raise ValueError('关系对照缺少完整来源绑定')
    for name, digest in fixture['bindings'].items():
        if sha(ROOT / name) != digest:
            raise ValueError('关系对照来源已变化：' + name)
    package = load_runtime_story_package(package_path, lazy=True)
    projection = payload['contextProjection']
    if projection['package'] != dict(id=package['id'], version=package['version']):
        raise ValueError('快照与当前官方包不匹配')
    state = projection['authoritativeState']
    visibility = expand_state_visibility(package.get('stateVisibility'), state, package)
    binding = json.dumps(dict(source=prose['sourceSha256'], package=projection['package'],
                              branch=projection['branch'], visibility=visibility,
                              declaration=fixture['bindings'][str((package_path.parent / 'modules/state-schema.json').relative_to(ROOT))]),
                         sort_keys=True, ensure_ascii=False)
    snapshot = Snapshot(hashlib.sha256(binding.encode()).hexdigest(), projection['branch']['lineageHead'],
                        public_locations(state, visibility))
    evidence, seen = grounding_input_evidence(payload), set()
    for case in fixture['cases']:
        ref = case['reference']
        if (not isinstance(case['id'], str) or not case['id'] or case['id'] in seen
                or case['axis'] not in {'position', 'negation', 'subject', 'time', 'completion', 'order'}
                or case['expectedVerdict'] not in {'supported', 'contradicted', 'unsupported'}
                or not isinstance(case['statement'], str) or not case['statement'].strip()
                or not isinstance(case['rationale'], str) or not case['rationale'].strip()
                or ref['id'] not in evidence or len(ref['quote'].strip()) < 4
                or ref['quote'] not in evidence[ref['id']]):
            raise ValueError('关系对照标签或来源无效')
        seen.add(case['id'])
    if not seen:
        raise ValueError('关系对照不能为空')
    return fixture, snapshot, evidence


def compare_location(claim, snapshot):
    """Compare a reviewed typed hypothesis; this does NOT parse or approve prose."""
    def pending(reason):
        return dict(verdict='needs_review', reason=reason, sources=[], proseAcceptance=False)

    keys = {'snapshotId', 'branchId', 'subject', 'relation', 'object', 'polarity', 'time', 'condition'}
    if (not isinstance(claim, dict) or set(claim) != keys
            or any(not isinstance(claim[k], str) or not claim[k] for k in keys - {'condition'})
            or claim['polarity'] not in {'positive', 'negative'}):
        return pending('invalid_claim')
    if claim['snapshotId'] != snapshot.snapshot_id or claim['branchId'] != snapshot.branch_id:
        return pending('snapshot_mismatch')
    if claim['time'] != 'snapshot' or claim['condition'] is not None:
        return pending('qualifier_not_covered')
    if claim['relation'] != 'exact_location_id':
        return pending('relation_not_covered')
    facts = [fact for fact in snapshot.locations if fact.subject == claim['subject']]
    if len(facts) != 1:
        return pending('public_subject_not_covered')
    # This adapter only resolves location IDs present in verified public facts.
    # Unknown names/IDs must not produce a "supported" negative by inequality.
    if claim['object'] not in {fact.value for fact in snapshot.locations}:
        return pending('public_object_not_covered')
    fact = facts[0]
    agrees = (claim['object'] == fact.value) == (claim['polarity'] == 'positive')
    return dict(verdict='supported' if agrees else 'contradicted', reason='exact_public_field_comparison',
                sources=[dict(snapshotId=snapshot.snapshot_id, branchId=snapshot.branch_id,
                              path=path, value=fact.value) for path in fact.paths], proseAcceptance=False)


def text_request(case, evidence):
    # A future semantic check may consume this, but never the prelabel/reference.
    return dict(statement=case['statement'], publicEvidence=dict(evidence))


def audit(path=FIXTURE):
    fixture, snapshot, evidence = load_inputs(path)
    text_cases = [dict(id=case['id'], axis=case['axis'], expectedVerdict=case['expectedVerdict'],
                       localVerdict='needs_review', reason='no_trusted_text_to_relation_mapping')
                  for case in fixture['cases']]
    # These are explicit diagnostic hypotheses, not model-extracted claims.
    fact = next(iter(snapshot.locations), None)
    controls = []
    if fact:
        base = dict(snapshotId=snapshot.snapshot_id, branchId=snapshot.branch_id, subject=fact.subject,
                    relation='exact_location_id', object=fact.value, polarity='positive', time='snapshot', condition=None)
        for name, changes, expected in (
            ('same_location', {}, 'supported'),
            ('negated_location', dict(polarity='negative'), 'contradicted'),
            ('other_subject', dict(subject='scene:wounded_stranger'), 'needs_review'),
            ('boundary_relation', dict(relation='side_of_boundary', object='inside'), 'needs_review'),
            ('historical_time', dict(time='three_years_ago'), 'needs_review'),
            ('conditional_location', dict(condition='before_lock'), 'needs_review'),
            ('sibling_branch', dict(branchId='diagnostic-sibling'), 'needs_review'),
            ('stale_snapshot', dict(snapshotId='diagnostic-stale'), 'needs_review'),
        ):
            claim = dict(base, **changes)
            result = compare_location(claim, snapshot)
            controls.append(dict(id=name, claim=claim, expectedVerdict=expected, result=result,
                                 matched=result['verdict'] == expected))
    previous = json.loads((ROOT / fixture['previousReportArtifact']).read_text())
    row = next(r for r in previous['cases'] if r['caseId'] == 'recorded_v11')
    raw = json.loads(row['calls'][0]['content'])
    original = next(c for c in raw['checks'] if c['id'] == 'C10')
    return dict(schemaVersion='context-relation-audit/0.1', fixtureSha256=sha(path),
                harnessSha256=sha(Path(__file__)), bindings=fixture['bindings'], snapshot=asdict(snapshot),
                scope=fixture['scope'], textCases=text_cases, structuredControls=controls,
                summary=dict(textCases=len(text_cases), textNeedsReview=len(text_cases),
                             structuredControls=len(controls), structuredMatches=sum(c['matched'] for c in controls)),
                priorFailure=dict(artifact=fixture['previousReportArtifact'], caseId='recorded_v11', propositionId='C10',
                                  rawVerdict=original['verdict'], rawSources=original['sources'],
                                  rawReason=original['reason'], localVerdict='needs_review',
                                  reason='伤者与封山线的关系未建模；未用文本标签或模型字段代替权威状态。'),
                modelCalls=0, formalSessionWrites=0, temporarySessionWrites=0, jevCalls=0,
                acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_checkpoint(args.output, report, create=True)
    print(json.dumps(report['summary'], ensure_ascii=False))
