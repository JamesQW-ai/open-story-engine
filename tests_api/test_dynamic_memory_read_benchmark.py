"""Temporary database boundary tests for the ledger read benchmark."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from open_story_engine import dynamic_memory as memory
from open_story_engine.api_read import ReadService
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore
from test_support.dynamic_memory_read_benchmark import fixture, pipeline, run, seed_database, ROOT
from test_support.longform import longform_cases


class DynamicMemoryReadBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.case = longform_cases()[0]
        self.temp = TemporaryDirectory(prefix='memory-read-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'

    def setup_pipeline(self, profile='goal'):
        context, selected, expected = fixture(self.case, 8, profile)
        seed_database(self.path, context)
        read = ReadService(ROOT / 'content/packages', self.path)
        resolver = ModuleContextResolver.for_package(self.case['path'], context['package'])
        return context, selected, expected, read, resolver

    def test_accumulating_ledger_fixture_has_reproducible_identity_and_distinct_official_bodies(self):
        for case in longform_cases():
            for profile in ('goal', 'thread'):
                a, selected, expected = fixture(case, 8, profile)
                b, _, _ = fixture(case, 8, profile)
                self.assertEqual(a['lineage'], b['lineage'])
                self.assertEqual(a['contract'], b['contract'])
                bodies = [n['narrativeText'] for n in a['lineage'][1:]]
                self.assertEqual(len(set(bodies)), 8)
                source = case['source'].read_text()
                self.assertTrue(all(body in source for body in bodies))
                field = 'goalLedger' if profile == 'goal' else 'threadLedger'
                entries = [e for e in a['parent']['branchState'][field] if e.get('causeBranchId')]
                self.assertEqual(len(entries), 6)
                self.assertEqual(sum(e['status'] in ('completed', 'resolved') for e in entries), 2)
                records = memory.select(a, a['parent']['branchState'], {})[0]
                self.assertEqual([m['sourceIds'][0] for m in records], [expected])

    def test_read_to_projection_is_stable_and_cannot_initialize_or_write(self):
        context, selected, expected, read, resolver = self.setup_pipeline()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with patch.object(SessionStore, '_initialize', side_effect=AssertionError('read path migration')):
            _, a = pipeline(read, context, selected, resolver)
            _, b = pipeline(read, context, selected, resolver)
        self.assertEqual(a['lineage'], context['lineage'])
        self.assertEqual(a['bundleSha256'], b['bundleSha256'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertEqual([m['sourceIds'][0] for m in a['chapter']['dynamicMemory']], [expected])
        self.assertNotIn(expected, [e['sourceId'] for e in a['chapter']['allowedEvidence']])
        self.assertNotIn('goalLedger', a['chapter']['authoritativeState'])

    def test_invalid_current_receipt_never_revives_previously_open_thread(self):
        context, selected, _, read, resolver = self.setup_pipeline('thread')
        store = SessionStore(str(self.path))
        try:
            with store.connection:
                damaged = copy.deepcopy(context['parent'])
                damaged['contextMemory']['records'][0]['memory']['content'] = '篡改当前来源'
                store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?',
                                         (json.dumps(damaged, ensure_ascii=False), damaged['id']))
        finally:
            store.close()
        _, result = pipeline(read, context, selected, resolver)
        self.assertEqual(result['memories'], [])
        self.assertEqual(result['audit']['lifecycle']['counts']['source_invalid'], 1)
        self.assertGreater(result['audit']['lifecycle']['counts']['state_unverified'], 0)

    def test_other_route_does_not_enter_ancestor_read_or_memory(self):
        context, selected, _, read, resolver = self.setup_pipeline()
        _, before = pipeline(read, context, selected, resolver)
        root = context['lineage'][0]
        sibling = copy.deepcopy(context['lineage'][1])
        sibling.update(id='benchmark-sibling', parentId=root['id'])
        sibling.pop('contextMemory')
        store = SessionStore(str(self.path))
        try:
            store.append_branch(root['sessionId'], root['id'], sibling)
        finally:
            store.close()
        _, after = pipeline(read, context, selected, resolver)
        self.assertEqual(after['lineage'], before['lineage'])
        self.assertEqual(after['bundleSha256'], before['bundleSha256'])

    def test_report_records_read_cost_without_claiming_semantic_or_api_acceptance(self):
        report = run(lengths=(8,), repeats=2)
        self.assertFalse(report['semanticAcceptance'])
        self.assertEqual(report['modelCalls'], 0)
        self.assertTrue(report['databaseMeasured'])
        self.assertEqual({r['profile'] for r in report['rows']}, {'goal', 'thread'})
        for row in report['rows']:
            self.assertEqual(row['lineageSelectQueries'], 9)
            self.assertGreater(row['storedNodeJsonBytes'], 0)
            self.assertEqual(row['selectedCount'], 1)
            self.assertTrue(row['databaseUnchanged'])
            self.assertEqual(len(row['timings']), 2)
            for sample in row['timings']:
                parts = [v for k, v in sample.items() if k != 'totalMs']
                self.assertAlmostEqual(sum(parts), sample['totalMs'])

    def test_invalid_measurement_request_fails_before_creating_fixture(self):
        for kwargs in ({'lengths': ()}, {'lengths': (0,)}, {'repeats': 0}, {'profiles': ()}, {'profiles': ('unsupported',)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run(**kwargs)

    def test_seed_refuses_to_reuse_existing_database(self):
        context, _, _, _, _ = self.setup_pipeline()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '新临时数据库'):
            seed_database(self.path, context)
        self.assertEqual(self.path.read_bytes(), before)
