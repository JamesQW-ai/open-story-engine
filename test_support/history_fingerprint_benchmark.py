"""Paired real snapshot preparation: canonical reserialization vs stored-byte fingerprint."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
from tempfile import TemporaryDirectory
from unittest.mock import patch

from open_story_engine.api_play import PlayService
from open_story_engine.api_read import ReadService
from open_story_engine.api_turn_drafts import digest, lineage_digest
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore
from test_support.dynamic_memory_read_benchmark import ROOT, fixture, metrics, pipeline, seed_database
from test_support.longform import longform_cases
from test_support.turn_snapshot_benchmark import sample


def run(lengths=(32, 128, 512), repeats=5):
    if not lengths or any(n < 1 for n in lengths) or repeats < 1:
        raise ValueError('无效的测量长度或次数')
    rows = []
    legacy_read = SessionStore.lineage

    def canonical_read(store, sid, bid):
        nodes = legacy_read(store, sid, bid)
        return nodes, lineage_digest(nodes)

    for case in longform_cases():
        for profile in ('goal', 'thread'):
            for turns in lengths:
                context, selected, expected = fixture(case, turns, profile)
                input_sha = digest({k: v for k, v in context.items() if k != 'package'})
                with TemporaryDirectory(prefix='history-fingerprint-') as tmp:
                    path = Path(tmp) / 'story.sqlite'
                    seed_database(path, context)
                    store = SessionStore(str(path))
                    try:
                        store.save_contract(context['contract'])
                    finally:
                        store.close()
                    database_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                    read = ReadService(ROOT / 'content/packages', path)
                    with patch.dict(os.environ, {'STORY_PLANNER': 'mock'}):
                        play = PlayService(read, Path(tmp))
                    samples = {'canonical': [], 'stored_bytes': []}
                    bindings, base_binding = {}, None
                    try:
                        for index in range(repeats):
                            modes = ('canonical', 'stored_bytes') if index % 2 == 0 else ('stored_bytes', 'canonical')
                            for mode in modes:
                                method = canonical_read if mode == 'canonical' else SessionStore.lineage_with_fingerprint
                                with patch.object(SessionStore, 'lineage_with_fingerprint', method):
                                    timing, calls, binding, snapshot = sample(play, context['parent']['sessionId'], context['parent']['id'])
                                if calls != [turns + 1] or snapshot.history != context['lineage']:
                                    raise AssertionError('读取次数或历史内容改变')
                                if mode in bindings and bindings[mode] != binding:
                                    raise AssertionError('相同输入绑定不稳定')
                                bindings[mode] = binding
                                comparable = {k: v for k, v in binding.items() if k != 'lineage_digest'}
                                if base_binding is not None and comparable != base_binding:
                                    raise AssertionError('指纹格式以外的回合绑定改变')
                                base_binding = comparable
                                samples[mode].append(timing)
                        resolver = ModuleContextResolver.for_package(case['path'], context['package'])
                        _, original = pipeline(read, context, selected, resolver)
                        with patch.object(SessionStore, 'lineage', lambda s, sid, bid: s.lineage_with_fingerprint(sid, bid)[0]):
                            _, alternative = pipeline(read, context, selected, resolver)
                        if original != alternative or [m['sourceIds'][0] for m in alternative['memories']] != [expected]:
                            raise AssertionError('记忆审计或 bundle/写作投影不等价')
                    finally:
                        play.drafts.close()
                        play._jev_shadow_executor.shutdown(wait=True)
                    if hashlib.sha256(path.read_bytes()).hexdigest() != database_sha:
                        raise AssertionError('只读测量修改故事库')
                if digest({k: v for k, v in context.items() if k != 'package'}) != input_sha:
                    raise AssertionError('输入被修改')
                rows.append(dict(packageId=case['package_id'], packageVersion=case['version'], novelCjk=case['cjk'],
                    sourceSha256=case['sha256'], profile=profile, turns=turns, repeats=repeats,
                    fixtureInputSha256=input_sha, timings=samples, metrics={k: metrics(v) for k, v in samples.items()},
                    databaseUnchanged=True, semanticProjectionEqual=True, selectedSources=[expected],
                    bundleSha256=alternative['bundleSha256'], chapterChars=len(alternative['chapterJson'])))
    sources = ('test_support/history_fingerprint_benchmark.py', 'test_support/turn_snapshot_benchmark.py',
               'test_support/dynamic_memory_read_benchmark.py', 'test_support/dynamic_memory_benchmark.py',
               'test_support/longform.py', 'open_story_engine/storage.py', 'open_story_engine/api_read.py',
               'open_story_engine/api_play.py', 'open_story_engine/api_turn_drafts.py',
               'open_story_engine/dynamic_memory.py', 'open_story_engine/context_bundle.py',
               'open_story_engine/module_context.py')
    return dict(schemaVersion='history-fingerprint-benchmark/1', modelCalls=0, semanticAcceptance=False,
                scope='actual _turn_snapshot; excludes enqueue, model, review and commit',
                comparison='both paths read full history once; fingerprint format intentionally differs; lineageMs includes hashing',
                cachePolicy='snapshot cache cleared each sample; alternating order; OS cache uncontrolled',
                python=platform.python_version(), platform=platform.platform(),
                sourceHashes={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources}, rows=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--lengths', type=int, nargs='+', default=[32, 128, 512])
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('输出文件已存在，不能覆盖历史证据')
    report = run(args.lengths, args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps([dict(profile=r['profile'], turns=r['turns'], metrics=r['metrics']) for r in report['rows']]))


if __name__ == '__main__':
    main()
