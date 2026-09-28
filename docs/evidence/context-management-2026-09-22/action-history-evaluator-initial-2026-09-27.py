"""Labeled offline selection controls; no models, sessions or semantic acceptance."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from open_story_engine.action_review_context import action_review_history
from open_story_engine import dynamic_memory
from open_story_engine.content import expand_state_visibility, load_runtime_story_package
from open_story_engine.context_bundle import ContextBundleBuilder
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.reader_scene_review import grounding_input_evidence
from test_support.dynamic_memory_read_benchmark import fixture as memory_fixture
from test_support.longform import ROOT, longform_cases

FIXTURE = ROOT / 'test_support/fixtures/action-history-selection-2026-09-27.json'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def score(expected, selected):
    expected, selected = set(expected), set(selected)
    return dict(truePositive=len(expected & selected), falsePositive=len(selected - expected),
                falseNegative=len(expected - selected), missing=sorted(expected - selected),
                unexpected=sorted(selected - expected), exactMatch=expected == selected)


def aggregate(rows):
    tp = sum(r['truePositive'] for r in rows)
    fp = sum(r['falsePositive'] for r in rows)
    fn = sum(r['falseNegative'] for r in rows)
    return dict(cases=len(rows), matched=sum(r['exactMatch'] for r in rows), truePositive=tp,
                falsePositive=fp, falseNegative=fn, precision=tp / (tp + fp) if tp + fp else None,
                recall=tp / (tp + fn) if tp + fn else None)


def build_bundle(context, selected, memories=(), proofs=()):
    state = context['parent']['branchState']
    return ContextBundleBuilder().build(
        context=context, selected=selected, state=state, module_context={},
        branch={'parentBranchId': context['parent']['id']},
        state_visibility=expand_state_visibility(context['package']['stateVisibility'], state, context['package']),
        validated_state_patch={}, dynamic_memory=memories, memory_evidence=proofs)


def reference_text(row, contract, root):
    field, index = row['source'].split('.')
    if field == 'paragraph':
        text = root['narrativeText'].split('\n\n')[int(index)]
    else:
        text = contract['openingContext'][field][int(index)]
    if row.get('quote'):
        if row['quote'] not in text:
            raise ValueError('标注引用已不在官方开场：' + row['id'])
        text = row['quote']
    return text


def evaluate(fixture_path=FIXTURE):
    labels = json.loads(Path(fixture_path).read_text())
    cases = longform_cases()
    case = next(c for c in cases if (c['package_id'], c['version']) ==
                (labels['packageId'], labels['packageVersion']))
    if case['sha256'] != labels['novelSha256']:
        raise ValueError('标注母本哈希已变化')
    package = load_runtime_story_package(case['path'], lazy=True)
    contract = create_contract(package, 'selection-controls', {
        'kind': 'source_character', 'sourceCharacterId': labels['characterId']})
    root = entry_node(package, contract)
    history_rows, memory_rows = [], []
    for row in labels['historyCases']:
        # Gold fields are never passed to the selector or bundle builder.
        text = reference_text(row, contract, root)
        prior = {**root, 'id': 'control-prior', 'summary': text}
        parent = {**root, 'id': 'control-parent', 'summary': row.get('parent', '你停下脚步。')}
        context = dict(package=package, contract=contract, parent=parent, lineage=[prior, parent],
                       playerDirection=row['action'])
        source = 'branch:lineage:control-prior'
        if row.get('explicit'):
            context['resultContract'] = {'scenePlan': {'knowledge': [{'sources': [{'id': source, 'quote': text}]}]}}
        selected = dict(title=row['action'], summary=row['action'], statePatch={})
        bundle = build_bundle(context, selected)
        evidence = grounding_input_evidence({'contextProjection': bundle.project('grounding_review')})
        before = bundle.as_dict()
        history, audit = action_review_history(context, row['action'], bundle, evidence)
        if before != bundle.as_dict():
            raise AssertionError('选择器修改上下文快照')
        actual = [x['sourceId'] for x in history['sourceReferences']]
        expected = [source] if row['expectedPrior'] else []
        # Score discretionary retrieval separately; a mandatory parent must not
        # inflate recall or precision on the candidate being evaluated.
        checked = score(expected, [s for s in actual if s != 'branch:lineage:control-parent'])
        history_rows.append(dict(caseId=row['id'], input=dict(action=row['action'], prior=text,
            parent=parent['summary'], explicit=row.get('explicit', False)), expectedSources=expected,
            selectedSources=actual, rationale=row['reason'], **checked,
            parentRetained='branch:lineage:control-parent' in actual,
            missingFromFullPublicEvidence=sorted(set(expected) - evidence.keys()),
            contextSha256=bundle.context_sha256, audit=audit))
    for row in labels['memoryCases']:
        context, selected, current = memory_fixture(case, row['turns'], row['profile'])
        if row['actionMode'] == 'unrelated':
            context['playerDirection'] = '停下脚步'
        if row.get('tamper'):
            context['parent']['narrativeText'] += '来源变更控制'
        inputs = {k: v for k, v in context.items() if k != 'package'}
        before = copy.deepcopy(inputs)
        memories, proofs, lifecycle = dynamic_memory.select(context, context['parent']['branchState'], {})
        bundle = build_bundle(context, selected, memories, proofs)
        evidence = grounding_input_evidence({'contextProjection': bundle.project('grounding_review')})
        history, audit = action_review_history(context, context['playerDirection'], bundle, evidence)
        actual = [r['sourceId'] for r in history['sourceReferences'] if r['reason'] == 'selected_dynamic_memory']
        expected = [current] if row['expectedCurrent'] else []
        if before != inputs:
            raise AssertionError('记忆选择修改原输入')
        memory_rows.append(dict(caseId=row['id'], expectedSources=expected, selectedSources=actual,
            rationale=row['reason'], **score(expected, actual), lifecycle=lifecycle,
            missingFromFullPublicEvidence=sorted(set(expected) - evidence.keys()), audit=audit))
    paths = [Path(__file__), FIXTURE, ROOT / 'open_story_engine/action_review_context.py',
             ROOT / 'open_story_engine/context_bundle.py', ROOT / 'open_story_engine/dynamic_memory.py',
             ROOT / 'test_support/dynamic_memory_benchmark.py',
             ROOT / 'test_support/dynamic_memory_read_benchmark.py', case['path']]
    return dict(schemaVersion='action-history-selection-eval/1', labelOrigin=labels['labelOrigin'],
        packageId=case['package_id'], packageVersion=case['version'], novelSha256=case['sha256'], novelCjk=case['cjk'],
        scope='synthetic_controls_on_official_longform_not_live_story_or_independent_heldout',
        inputHashes={str(p.relative_to(ROOT)): sha(p) for p in paths},
        history=history_rows, memory=memory_rows,
        summary=dict(history=aggregate(history_rows), memory=aggregate(memory_rows)),
        newModelCalls=0, formalSessionWrites=0, semanticAcceptance=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps(report['summary'], ensure_ascii=False))


if __name__ == '__main__':
    main()
