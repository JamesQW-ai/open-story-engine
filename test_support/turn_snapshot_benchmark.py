"""Offline paired turn preparation costs; both paths enforce the same history binding."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

from open_story_engine import api_play, dynamic_memory
from open_story_engine.api_journey import journey
from open_story_engine.api_read import ReadService
from open_story_engine.api_turn_drafts import digest
from open_story_engine.storage import SessionStore
from test_support.dynamic_memory_read_benchmark import ROOT, fixture, metrics, seed_database
from test_support.longform import longform_cases


def sample(play, sid, bid, repeated=False):
    """Measure a cold application snapshot cache, never a model call.

    The control restores the old repeated-read/journal work, while retaining
    today's binding checks. It is not an executable historical release.
    Stage durations are exclusive of nested lineage reads.
    """
    timings = {key: 0.0 for key in ('lineageMs', 'packageMs', 'routeMs', 'closingMs',
                                   'snapshotMs')}
    calls = []
    original_lineage = SessionStore.lineage
    original_fingerprint_read = SessionStore.lineage_with_fingerprint
    original_route = api_play.route_status
    original_closing = api_play.read_closing_intent

    def read_lineage(store, session, branch):
        start = time.perf_counter_ns()
        try:
            nodes = original_lineage(store, session, branch)
            calls.append(len(nodes))
            return nodes
        finally:
            timings['lineageMs'] += (time.perf_counter_ns() - start) / 1e6

    def timed(key, fn):
        def call(*args, **kwargs):
            prior_read = timings['lineageMs']
            start = time.perf_counter_ns()
            try:
                return fn(*args, **kwargs)
            finally:
                timings[key] += (time.perf_counter_ns() - start) / 1e6 - (timings['lineageMs'] - prior_read)
        return call

    def read_fingerprint(store, session, branch):
        start = time.perf_counter_ns()
        try:
            nodes, fingerprint = original_fingerprint_read(store, session, branch)
            calls.append(len(nodes))
            return nodes, fingerprint
        finally:
            timings['lineageMs'] += (time.perf_counter_ns() - start) / 1e6

    def status(store, session, nodes, package):
        return (journey(store, session, nodes[-1]['id'], package)['status'] if repeated
                else original_route(store, session, nodes, package))

    def closing(store, session, nodes):
        return (store.closing_intent(session, nodes[-1]['id']) if repeated
                else original_closing(store, session, nodes))

    play._turn_context_cache.clear()
    with ExitStack() as stack:
        stack.enter_context(patch.object(SessionStore, 'lineage', read_lineage))
        stack.enter_context(patch.object(SessionStore, 'lineage_with_fingerprint', read_fingerprint))
        stack.enter_context(patch.object(play.read, 'load_package', timed('packageMs', play.read.load_package)))
        stack.enter_context(patch.object(api_play, 'route_status', timed('routeMs', status)))
        stack.enter_context(patch.object(api_play, 'read_closing_intent', timed('closingMs', closing)))
        stack.enter_context(patch.object(api_play, 'TurnSnapshot', timed('snapshotMs', api_play.TurnSnapshot)))
        start = time.perf_counter_ns()
        binding, snapshot = play._turn_snapshot(sid, bid)
        total = (time.perf_counter_ns() - start) / 1e6
    timings['otherMs'] = total - sum(timings.values())
    timings['totalMs'] = total
    return timings, calls, binding, snapshot


def run(lengths=(32, 128, 512), repeats=5, profiles=('goal', 'thread')):
    if not lengths or any(n < 1 for n in lengths) or repeats < 1 or not profiles or any(p not in ('goal', 'thread') for p in profiles):
        raise ValueError('无效的测量长度、次数或类型')
    rows = []
    for case in longform_cases():
        for profile in profiles:
            for turns in lengths:
                context, _, expected = fixture(case, turns, profile)
                input_sha = digest({k: v for k, v in context.items() if k != 'package'})
                with TemporaryDirectory(prefix='story-snapshot-benchmark-') as tmp:
                    path = Path(tmp) / 'fixture.sqlite'
                    seed_database(path, context)
                    store = SessionStore(str(path))
                    try:
                        store.save_contract(context['contract'])
                    finally:
                        store.close()
                    database_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    read = ReadService(ROOT / 'content/packages', path)
                    # The temporary directory has no .env; no real provider is configured.
                    with patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
                        play = api_play.PlayService(read, Path(tmp))
                    samples = {'shared_reads': [], 'repeated_reads_control': []}
                    stable_binding = None
                    try:
                        for index in range(repeats):
                            modes = (False, True) if index % 2 == 0 else (True, False)
                            for repeated in modes:
                                timing, calls, binding, snapshot = sample(play, context['parent']['sessionId'],
                                                                          context['parent']['id'], repeated)
                                if calls != [turns + 1] * (3 if repeated else 1):
                                    raise AssertionError('历史读取次数与配对条件不符')
                                if snapshot.history != context['lineage']:
                                    raise AssertionError('回合快照改变历史内容')
                                if stable_binding is not None and stable_binding != binding:
                                    raise AssertionError('相同输入的配对路径产生不同绑定')
                                stable_binding = binding
                                live = dict(context, lineage=snapshot.history, parent=snapshot.history[-1])
                                selected, _, audit = dynamic_memory.select(live, live['parent']['branchState'], {})
                                if [m['sourceIds'][0] for m in selected] != [expected]:
                                    raise AssertionError('配对路径选取了不同记忆')
                                samples['repeated_reads_control' if repeated else 'shared_reads'].append(timing)
                    finally:
                        play.drafts.close()
                        play._jev_shadow_executor.shutdown(wait=True)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != database_sha:
                        raise AssertionError('测量修改了临时故事库')
                if digest({k: v for k, v in context.items() if k != 'package'}) != input_sha:
                    raise AssertionError('测量修改了原始输入')
                rows.append(dict(packageId=case['package_id'], packageVersion=case['version'], novelCjk=case['cjk'],
                    sourceSha256=case['sha256'], profile=profile, turns=turns, repeats=repeats,
                    fixtureInputSha256=input_sha, databaseUnchanged=True, bindingSha256=digest(stable_binding),
                    selectedSources=[expected], memoryChars=audit['contentChars'],
                    timings=samples, metrics={k: metrics(v) for k, v in samples.items()},
                    lineageReads={'shared_reads': 1, 'repeated_reads_control': 3}))
    sources = ('test_support/turn_snapshot_benchmark.py', 'test_support/dynamic_memory_read_benchmark.py',
               'test_support/dynamic_memory_benchmark.py', 'test_support/longform.py',
               'open_story_engine/api_play.py', 'open_story_engine/api_turn_drafts.py',
               'open_story_engine/api_read.py', 'open_story_engine/api_journey.py',
               'open_story_engine/storage.py', 'open_story_engine/route_lifecycle.py',
               'open_story_engine/dynamic_memory.py')
    return dict(schemaVersion='turn-snapshot-benchmark/2', modelCalls=0, semanticAcceptance=False,
                historyFingerprintTiming='included in lineageMs',
                scope='actual _turn_snapshot, including package verification and complete lineage binding; excludes enqueue, generation, review and commit',
                control='current history validation plus reconstructed old repeated reads and journal rendering, not a historical release',
                cachePolicy='application snapshot cache cleared per sample; alternating paired order; OS cache uncontrolled',
                python=platform.python_version(), platform=platform.platform(),
                sourceHashes={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources}, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--lengths', type=int, nargs='+', default=[32, 128, 512])
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，不能覆盖历史证据')
    result = run(args.lengths, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps([dict(profile=r['profile'], turns=r['turns'], metrics=r['metrics']) for r in result['rows']]))


if __name__ == '__main__':
    main()
