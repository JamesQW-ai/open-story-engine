"""Offline selection timings on official novel text with synthetic state receipts.

This measures in-memory selection only, not narrative quality, DB loading or
semantic review. Every body is a distinct paragraph from an eligible novel.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import time

from open_story_engine import dynamic_memory as memory
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.reader_consequences import VERSION as REVIEW_VERSION
from test_support.longform import longform_cases


def fixture(case, turns):
    package = load_runtime_story_package(case['path'], lazy=True)
    entry = next(iter(package['story']['entryModel']['entryPoints']))
    player = entry['sourceCharacterIds'][0]
    other = next(c['id'] for c in package['characters'] if c['id'] != player)
    contract = create_contract(package, 'memory-benchmark', dict(kind='source_character', sourceCharacterId=player))
    contract.update(id='memory-benchmark-contract', createdAt='2026-09-27T00:00:00Z')
    root = {**entry_node(package, contract), 'sessionId': 'memory-benchmark', 'parentId': None}
    root.update(id='memory-benchmark-root', createdAt='2026-09-27T00:00:00Z')
    bodies = list(dict.fromkeys(line.strip() for line in case['source'].read_text().splitlines() if len(line.strip()) >= 20))
    if turns > len(bodies):
        raise ValueError('官方母本独立段落不足，不能重复模板补足长链')
    lineage, latest = [root], {}
    items = list(package['items'])[:12]
    if not items:
        raise ValueError('物品长链测量需要已登记物品')
    for index, body in enumerate(bodies[:turns]):
        parent = lineage[-1]
        state = copy.deepcopy(parent['branchState'])
        iid = items[index % len(items)]['id']
        before = state.get('itemOwnerCharacterIds', {}).get(iid)
        owner = other if before == player else player
        state.setdefault('itemOwnerCharacterIds', {})[iid] = owner
        state.setdefault('itemLocationIds', {}).pop(iid, None)
        node = dict(id='memory-bench-' + str(index), parentId=parent['id'], sessionId='memory-benchmark',
                    sourceNodeRef=root['sourceNodeRef'], branchState=state, narrativeText=body,
                    consequenceReview=REVIEW_VERSION, reviewedNarrativeSha256=hashlib.sha256(body.encode()).hexdigest(),
                    authorityReview={'decision': 'allow'}, observedEvents=[], eventChecks=[],
                    consequenceUpdate=dict(outcomes=[], goalUpdates=[], threadUpdates=[], stateChanges=[
                        dict(entityId=iid, attribute='ownerCharacterId', before=before, value=owner, evidence=body)]))
        # Deliberately synthetic review structures: do not submit these nodes to
        # a story session or interpret unrelated source prose as causal evidence.
        node['contextMemory'] = memory.receipt_for(package, node)
        lineage.append(node)
        latest[iid] = node['contextMemory']['records'][0]['memory']['sourceIds'][0]
    context = dict(package=package, contract=contract, parent=lineage[-1], lineage=lineage, playerDirection='')
    target = items[0]
    return context, {'items': [target]}, latest[target['id']]


def run(lengths=(32, 128, 512), repeats=7):
    if repeats < 1 or not lengths or any(n < 1 for n in lengths):
        raise ValueError('长度与重复次数必须为正整数')
    rows = []
    for case in longform_cases():
        for turns in lengths:
            context, module, expected = fixture(case, turns)
            state = context['parent']['branchState']
            input_data = {key: value for key, value in context.items() if key != 'package'}
            before = memory._sha(input_data)
            durations = []
            for _ in range(repeats):
                started = time.perf_counter_ns()
                selected, proof, audit = memory.select(context, state, module)
                durations.append((time.perf_counter_ns() - started) / 1e6)
                if [m['sourceIds'][0] for m in selected] != [expected]:
                    raise AssertionError('未按实际最新原因选择唯一相关记忆')
                if len(selected) > memory.MAX_SELECTED or audit['contentChars'] > memory.MAX_CONTENT_CHARS:
                    raise AssertionError('注入越过固定预算')
            if memory.select(context, state, {})[0]:
                raise AssertionError('无关记忆进入上下文')
            if memory._sha(input_data) != before:
                raise AssertionError('只读选择修改了输入')
            rows.append(dict(packageId=case['package_id'], packageVersion=case['version'], sourceSha256=case['sha256'],
                novelCjk=case['cjk'], turns=turns, lineageNodes=len(context['lineage']), repeats=repeats,
                uniqueBodies=turns, timingsMs=durations, medianMs=statistics.median(durations),
                p95Ms=sorted(durations)[math.ceil(.95 * repeats) - 1], selectedCount=len(selected),
                contentChars=audit['contentChars'], fixtureInputSha256=before,
                selectedSources=[m['sourceIds'][0] for m in selected], expectedSources=[expected],
                auditDetails=len(audit.get('lifecycle', {}).get('details', [])),
                auditOmittedDetails=audit.get('lifecycle', {}).get('omittedDetails', 0)))
    base = Path(__file__).resolve().parents[1]
    paths = ('open_story_engine/dynamic_memory.py', 'open_story_engine/context_bundle.py',
             'test_support/dynamic_memory_benchmark.py')
    return dict(schemaVersion='memory-selection-benchmark/1', mode='offline_synthetic_state_official_text',
                semanticAcceptance=False, databaseMeasured=False, python=platform.python_version(),
                platform=platform.platform(), sourceHashes={p: hashlib.sha256((base / p).read_bytes()).hexdigest() for p in paths}, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lengths', type=int, nargs='+', default=[32, 128, 512])
    parser.add_argument('--repeats', type=int, default=7)
    args = parser.parse_args()
    result = run(args.lengths, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps([dict(turns=r['turns'], medianMs=r['medianMs'], p95Ms=r['p95Ms']) for r in result['rows']]))


if __name__ == '__main__':
    main()
