"""Disposable SQLite -> modules -> memory -> bundle/preview measurements.

Ledger/review fixtures are synthetic; distinct prose comes from eligible
official novels. No semantic acceptance, production writes or model calls.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
from tempfile import TemporaryDirectory
import time

from open_story_engine import dynamic_memory as memory, reader_threads
from open_story_engine.api_read import ReadService
from open_story_engine.content import expand_state_visibility
from open_story_engine.context_bundle import ContextBundleBuilder
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore
from test_support.dynamic_memory_benchmark import fixture as item_fixture
from test_support.longform import longform_cases

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ('goal', 'thread')


def fixture(case, turns, profile):
    if profile not in PROFILES or turns < 1:
        raise ValueError('仅支持正数长度的 goal/thread 夹具')
    context, _, _ = item_fixture(case, turns)
    root = context['lineage'][0]
    player = context['contract']['persona']['sourceCharacterId']
    latest_id = None
    for index, node in enumerate(context['lineage'][1:]):
        parent = context['lineage'][index]
        state = copy.deepcopy(parent['branchState'])
        body = node['narrativeText']
        update = dict(outcomes=[], stateChanges=[], goalUpdates=[], threadUpdates=[])
        ledger_key = 'goalLedger' if profile == 'goal' else 'threadLedger'
        # Three additions followed by closing the latest one: both accumulation
        # and historical supersession are represented, without resetting IDs.
        closing = index % 4 == 3
        prior = next((g for g in state.get(ledger_key, []) if g['id'] == latest_id), None)
        title = prior['title'] if closing else '离线测量' + ('目标' if profile == 'goal' else '问题') + f'第{index:05d}项'
        status = ('completed' if closing else 'active') if profile == 'goal' else ('resolved' if closing else 'open')
        entry = dict(id=latest_id if closing else ('new' if profile == 'goal' else 'new-1'),
                     title=title, status=status, reason='仅测量结构成本', evidence=body)
        if profile == 'goal':
            entry['dependencies'] = []
            update['goalUpdates'] = [entry]
            record = dict(entry, characterId=player, source='player_branch', causeBranchId=node['id'])
            if closing:
                prior.update(record)
            else:
                latest_id = 'goal-' + hashlib.sha256((node['id'] + ':0').encode()).hexdigest()[:16]
                state.setdefault('goalLedger', []).append(dict(record, id=latest_id))
        else:
            entry['stepIds'] = ['S1']
            update['threadUpdates'] = [entry]
            state[ledger_key] = reader_threads.commit(context['package'], context['contract'], state, [entry], node['id'])
            if not closing:
                latest_id = state[ledger_key][-1]['id']
        node.update(branchState=state, consequenceUpdate=update, nextDirections=root['nextDirections'],
                    selectedDirectionId=root['nextDirections'][0]['id'], summary=body,
                    createdAt='2026-09-27T00:00:00Z', sequence=index + 1, kind='generated')
        node['contextMemory'] = memory.receipt_for(context['package'], node)
    context['playerDirection'] = '查看' + title
    root['sequence'] = 0
    selected = dict(id='memory-inspect', title='查看记录', summary=context['playerDirection'], statePatch={})
    expected = context['parent']['contextMemory']['records'][0]['memory']['sourceIds'][0]
    return context, selected, expected


def seed_database(path, context):
    """Use only a newly created disposable DB. This is fixture setup, not gameplay."""
    if Path(path).exists():
        raise ValueError('测量夹具只能写入不存在的新临时数据库')
    sid = context['parent']['sessionId']
    store = SessionStore(str(path))
    try:
        root = context['lineage'][0]
        store.create_session(context['package'], sid, initial_state=root['branchState'])
        store.create_branch_root(sid, root)
        for node in context['lineage'][1:]:
            store.append_branch(sid, node['parentId'], node)
    finally:
        store.close()


def pipeline(read, context, selected, resolver):
    started = time.perf_counter_ns()
    with read.store() as store:
        lineage = store.lineage(context['parent']['sessionId'], context['parent']['id'])
    loaded = time.perf_counter_ns()
    live = dict(context, parent=lineage[-1], lineage=lineage)
    state = live['parent']['branchState']
    module = resolver.resolve(live, selected, state)
    resolved = time.perf_counter_ns()
    memories, proof, audit = memory.select(live, state, module)
    chosen = time.perf_counter_ns()
    bundle = ContextBundleBuilder().build(context=live, selected=selected, state=state,
        branch=dict(sessionId=live['parent']['sessionId'], parentBranchId=live['parent']['id']),
        module_context=module, state_visibility=expand_state_visibility(context['package']['stateVisibility'], state, context['package']),
        state_visibility_mode='formal_required', dynamic_memory=memories, memory_evidence=proof)
    built = time.perf_counter_ns()
    chapter = bundle.project('chapter')
    chapter_json = json.dumps(chapter, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    ended = time.perf_counter_ns()
    times = dict(readMs=(loaded-started)/1e6, resolveMs=(resolved-loaded)/1e6,
                 selectMs=(chosen-resolved)/1e6, bundleMs=(built-chosen)/1e6,
                 projectMs=(ended-built)/1e6, totalMs=(ended-started)/1e6)
    return times, dict(lineage=lineage, memories=memories, audit=audit,
                      chapter=chapter, chapterJson=chapter_json, bundleSha256=bundle.context_sha256)


def metrics(samples):
    return {key: dict(medianMs=statistics.median(row[key] for row in samples),
                     p95Ms=sorted(row[key] for row in samples)[math.ceil(.95 * len(samples))-1])
            for key in samples[0]}


def run(lengths=(32, 128, 512), repeats=5, profiles=PROFILES):
    if repeats < 1 or not lengths or any(n < 1 for n in lengths) or not profiles or any(p not in PROFILES for p in profiles):
        raise ValueError('无效的测量长度、次数或类型')
    rows = []
    for case in longform_cases():
        for profile in profiles:
            for turns in lengths:
                context, selected, expected = fixture(case, turns, profile)
                fixture_sha = memory._sha({k: v for k, v in context.items() if k != 'package'})
                resolver = ModuleContextResolver.for_package(case['path'], context['package'])
                with TemporaryDirectory(prefix='story-memory-read-') as tmp:
                    path = Path(tmp) / 'fixture.sqlite'
                    seed_database(path, context)
                    database_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    database_bytes = path.stat().st_size
                    read = ReadService(ROOT / 'content/packages', path)
                    with read.store() as store:
                        json_bytes = store.connection.execute(
                            'SELECT SUM(length(CAST(node_json AS BLOB))) FROM branch_nodes').fetchone()[0]
                        query_counts = []
                        store.connection.set_trace_callback(lambda sql: query_counts.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
                        store.lineage(context['parent']['sessionId'], context['parent']['id'])
                        store.connection.set_trace_callback(None)
                    samples, stable = [], None
                    for _ in range(repeats):
                        times, result = pipeline(read, context, selected, resolver)
                        samples.append(times)
                        if [m['sourceIds'][0] for m in result['memories']] != [expected]:
                            raise AssertionError('记忆选择遗漏或添加了无关来源')
                        if memory._sha(result['lineage']) != memory._sha(context['lineage']):
                            raise AssertionError('数据库还原改变了输入')
                        if stable is not None and stable != result['bundleSha256']:
                            raise AssertionError('相同读取输入产生不同 bundle')
                        stable = result['bundleSha256']
                        if result['audit']['contentChars'] > memory.MAX_CONTENT_CHARS:
                            raise AssertionError('记忆超出注入预算')
                        projected = result['chapter']
                        if projected['dynamicMemory'] != result['memories']:
                            raise AssertionError('写作投影丢失了选中的记忆')
                        if any(e['sourceId'] == expected for e in projected['allowedEvidence']):
                            raise AssertionError('同源记忆在写作证据中重复注入')
                    if hashlib.sha256(path.read_bytes()).hexdigest() != database_sha:
                        raise AssertionError('只读路径写入数据库')
                if memory._sha({k: v for k, v in context.items() if k != 'package'}) != fixture_sha:
                    raise AssertionError('测量修改了内存输入')
                rows.append(dict(packageId=case['package_id'], packageVersion=case['version'], novelCjk=case['cjk'],
                    sourceSha256=case['sha256'], profile=profile, turns=turns, repeats=repeats,
                    fixtureInputSha256=fixture_sha, databaseBytes=database_bytes, databaseUnchanged=True,
                    storedNodeJsonBytes=json_bytes, lineageSelectQueries=len(query_counts),
                    finalLedgerEntries=len(context['parent']['branchState']['goalLedger' if profile == 'goal' else 'threadLedger']),
                    timings=samples, metrics=metrics(samples), selectedSources=[expected],
                    selectedCount=len(result['memories']), memoryChars=result['audit']['contentChars'],
                    chapterChars=len(result['chapterJson']), chapterSha256=hashlib.sha256(result['chapterJson'].encode()).hexdigest(),
                    bundleSha256=stable, uniqueBodies=turns))
    paths = ('test_support/dynamic_memory_read_benchmark.py', 'test_support/dynamic_memory_benchmark.py',
             'open_story_engine/dynamic_memory.py', 'open_story_engine/storage.py',
             'open_story_engine/module_context.py', 'open_story_engine/context_bundle.py', 'open_story_engine/api_read.py',
             'open_story_engine/reader_threads.py', 'open_story_engine/content.py', 'test_support/longform.py')
    return dict(schemaVersion='memory-read-benchmark/1', semanticAcceptance=False, databaseMeasured=True,
                modelCalls=0, mode='offline_synthetic_ledgers_official_text',
                cachePolicy='new read-only connection each sample; OS cache uncontrolled; first sample has cold resolver, later samples reuse it',
                scope='read-only SQLite lineage + module resolve + memory select + bundle + chapter JSON; excludes API turn setup, budget and model',
                python=platform.python_version(), platform=platform.platform(),
                sourceHashes={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lengths', type=int, nargs='+', default=[32, 128, 512])
    parser.add_argument('--profiles', choices=PROFILES, nargs='+', default=list(PROFILES))
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    report = run(args.lengths, args.repeats, args.profiles)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps([dict(profile=r['profile'], turns=r['turns'], metrics=r['metrics'],
                           chapterChars=r['chapterChars']) for r in report['rows']]))


if __name__ == '__main__':
    main()
