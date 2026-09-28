"""Stored-byte history fingerprints preserve complete reads and fail closed."""
import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from open_story_engine.api_read import ReadService
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore
from test_support.dynamic_memory_read_benchmark import ROOT, fixture, pipeline, seed_database
from test_support.longform import longform_cases


class LineageFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'story.sqlite'
        self.context, self.selected, _ = fixture(longform_cases()[0], 8, 'thread')
        seed_database(self.path, self.context)
        self.read = ReadService(ROOT / 'content/packages', self.path)
        self.sid = self.context['parent']['sessionId']
        self.bid = self.context['parent']['id']

    def read_history(self):
        with self.read.store() as store:
            return store.lineage_with_fingerprint(self.sid, self.bid)

    def update(self, sql, params):
        store = SessionStore(str(self.path))
        try:
            with store.connection:
                store.connection.execute(sql, params)
        finally:
            store.close()

    def test_complete_projection_equivalence_and_no_write_or_initialization(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        resolver = ModuleContextResolver.for_package(longform_cases()[0]['path'], self.context['package'])
        with patch.object(SessionStore, '_initialize', side_effect=AssertionError('read migration')):
            nodes, fingerprint = self.read_history()
            self.assertEqual(nodes, self.context['lineage'])
            self.assertEqual((nodes, fingerprint), self.read_history())
            _, old = pipeline(self.read, self.context, self.selected, resolver)
            with patch.object(SessionStore, 'lineage', lambda store, sid, bid: store.lineage_with_fingerprint(sid, bid)[0]):
                _, new = pipeline(self.read, self.context, self.selected, resolver)
            self.assertEqual(old, new)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_format_only_change_conservatively_invalidates(self):
        nodes, before = self.read_history()
        root = nodes[0]
        self.update('UPDATE branch_nodes SET node_json=? WHERE id=?',
                    (json.dumps(root, ensure_ascii=False, indent=2), root['id']))
        after_nodes, after = self.read_history()
        self.assertEqual(nodes, after_nodes)
        self.assertNotEqual(before, after)

    def test_content_and_effective_sequence_changes_invalidate(self):
        nodes, before = self.read_history()
        root = nodes[0]
        root['summary'] = '临时库来源变化'
        self.update('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(root), root['id']))
        _, changed = self.read_history()
        self.assertNotEqual(before, changed)
        self.update('UPDATE branch_nodes SET sequence=99 WHERE id=?', (root['id'],))
        nodes, sequence_changed = self.read_history()
        self.assertEqual(nodes[0]['sequence'], 99)
        self.assertNotEqual(changed, sequence_changed)

    def test_sibling_does_not_change_history_fingerprint(self):
        before = self.read_history()
        node = copy.deepcopy(self.context['lineage'][1])
        node.update(id='sibling-fingerprint')
        node.pop('contextMemory')
        store = SessionStore(str(self.path))
        try:
            store.append_branch(self.sid, node['parentId'], node)
        finally:
            store.close()
        self.assertEqual(before, self.read_history())

    def test_missing_and_cross_session_nodes_fail(self):
        with self.read.store() as store:
            for sid, bid in ((self.sid, 'missing'), ('other-session', self.bid)):
                with self.assertRaises(ValueError):
                    store.lineage_with_fingerprint(sid, bid)
        parent = self.context['parent']['parentId']
        # Simulate an already damaged legacy DB; normal writes enforce this FK.
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                connection.execute('DELETE FROM branch_nodes WHERE id=?', (parent,))
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            self.read_history()

    def test_cycle_and_row_json_relation_mismatch_fail(self):
        root = copy.deepcopy(self.context['lineage'][0])
        root['parentId'] = self.bid
        self.update('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(root), root['id']))
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.read_history()
        self.update('UPDATE branch_nodes SET parent_id=? WHERE id=?', (self.bid, root['id']))
        with self.assertRaisesRegex(ValueError, '循环'):
            self.read_history()

    def test_transaction_snapshot_then_next_request_observes_change(self):
        store = SessionStore(str(self.path))
        try:
            store.connection.execute('PRAGMA journal_mode=WAL')
        finally:
            store.close()
        with self.read.store() as reader:
            before = reader.lineage_with_fingerprint(self.sid, self.bid)
            root = copy.deepcopy(before[0][0])
            root['summary'] = '下一次请求应看到此更改'
            self.update('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(root), root['id']))
            self.assertEqual(before, reader.lineage_with_fingerprint(self.sid, self.bid))
        self.assertNotEqual(before[1], self.read_history()[1])

    def test_rollback_restores_fingerprint(self):
        before = self.read_history()
        store = SessionStore(str(self.path))
        try:
            store.connection.execute('UPDATE branch_nodes SET node_json=? WHERE id=?', ('{}', self.bid))
            store.connection.rollback()
        finally:
            store.close()
        self.assertEqual(before, self.read_history())

    def test_legacy_empty_root_parent_is_preserved(self):
        root = copy.deepcopy(self.context['lineage'][0])
        root['parentId'] = ''
        self.update('UPDATE branch_nodes SET node_json=? WHERE id=?', (json.dumps(root), root['id']))
        with self.read.store() as store:
            expected = store.lineage(self.sid, self.bid)
        self.assertEqual(self.read_history()[0], expected)

    def test_cross_session_parent_and_forged_id_are_rejected(self):
        root = copy.deepcopy(self.context['lineage'][0])
        other_root = dict(root, id='other-root', sessionId='other-session')
        store = SessionStore(str(self.path))
        try:
            store.create_session(self.context['package'], 'other-session', initial_state=root['branchState'])
            store.create_branch_root('other-session', other_root)
        finally:
            store.close()
        root['parentId'] = other_root['id']
        self.update('UPDATE branch_nodes SET node_json=?,parent_id=? WHERE id=?',
                    (json.dumps(root), other_root['id'], root['id']))
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.read_history()
        self.update('UPDATE branch_nodes SET node_json=?,parent_id=NULL WHERE id=?',
                    (json.dumps(dict(root, id='forged', parentId=None)), root['id']))
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.read_history()

    def test_paired_snapshot_benchmark_preserves_semantics(self):
        from test_support.history_fingerprint_benchmark import run
        result = run((8,), 2)
        self.assertFalse(result['semanticAcceptance'])
        self.assertEqual(result['modelCalls'], 0)
        for row in result['rows']:
            self.assertTrue(row['databaseUnchanged'])
            self.assertTrue(row['semanticProjectionEqual'])
            self.assertEqual(set(row['metrics']), {'canonical', 'stored_bytes'})
