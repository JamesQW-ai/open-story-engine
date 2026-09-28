import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_eval as q


class QualifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = q.load_cases()

    def response(self, case):
        units = q.f.indexed_input(case)['units']
        return dict(items=[dict(unitIds=list(t['unitIds']), status=t['expectedStatus'],
                                normal=copy.deepcopy(t['expectedNormal']), reason='test',
                                limitations=[dict(kind=k, quote=units[t['unitIds'][0]]['quote'])
                                             for k in t['expectedLimitations']]) for t in case['targets']])

    def recorder(self, fail=False):
        recorder = SimpleNamespace(calls=[])
        order = list(q.schedule(self.fixture))
        def complete(messages):
            case, _ = order[len(recorder.calls)]
            content = json.dumps(self.response(case))
            recorder.calls.append(dict(messages=messages, content=content))
            if fail:
                raise q.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_blind_identical_inputs_and_only_contract_paragraph_changes(self):
        for c in self.fixture['cases']:
            baseline = q.messages(c, self.fixture['entities'], 'baseline')
            changed = q.messages(c, self.fixture['entities'], 'clarified')
            self.assertEqual(baseline[1], changed[1])
            payload = json.loads(baseline[1]['content'])
            self.assertEqual(set(payload), {'draft', 'units', 'entities'})
            altered = copy.deepcopy(c)
            altered.update(targets=[], labelRationale='secret label', id='secret id')
            self.assertEqual(baseline, q.messages(altered, self.fixture['entities'], 'baseline'))
        parts = [p.read_text().strip().split('\n\n') for p in q.PROMPTS.values()]
        self.assertEqual(parts[0][:-1], parts[1][:-1])

    def test_extra_missing_and_coexisting_qualifiers_are_scored_strictly(self):
        c = self.fixture['cases'][0]
        response = self.response(c)
        response['items'][0]['limitations'].append(dict(kind='modality', quote='就在'))
        score = q.score(json.dumps(response), c, self.fixture['entities'])
        self.assertEqual(score['status'], 'mismatched')
        self.assertEqual(score['alignment']['missingLocations'], [])
        self.assertEqual(score['alignment']['semanticDifferences'][0]['extraLimitations'], ['modality'])
        both = next(c for c in self.fixture['cases'] if c['id'] == 'both')
        response = self.response(both)
        self.assertEqual(q.score(json.dumps(response), both, self.fixture['entities'])['status'], 'matched')
        response['items'][0]['limitations'].pop()
        self.assertEqual(q.score(json.dumps(response), both, self.fixture['entities'])['status'], 'mismatched')

    def test_unrelated_qualifier_cannot_be_borrowed_or_force_pending(self):
        c = next(c for c in self.fixture['cases'] if c['id'] == 'unrelated_possibility')
        response = self.response(c)
        response['items'][0].update(status='needs_review', normal=None,
                                   limitations=[dict(kind='modality', quote='可能')])
        self.assertEqual(q.score(json.dumps(response), c, self.fixture['entities'])['status'], 'invalid_response')
        response['items'][0]['unitIds'].insert(0, 'P1-U1')
        self.assertEqual(q.score(json.dumps(response), c, self.fixture['entities'])['status'], 'mismatched')

    def test_frozen_parent_and_original_labels_reject_tampering(self):
        for kind in ('parent', 'gold'):
            data = copy.deepcopy(self.fixture)
            if kind == 'parent':
                data['parentFixtureSha256'] = '0'*64
            else:
                data['cases'][0]['targets'][0]['expectedLimitations'].append('modality')
            with TemporaryDirectory() as directory:
                path = Path(directory)/'cases.json'
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    q.load_cases(path)

    def test_once_per_pair_reproducible_audit_and_no_overwrite(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(q.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(q.f, '_gateway', return_value=recorder):
            path = Path(directory)/'report.json'
            report = q.run(path)
            self.assertEqual(report['actualCalls'], 18)
            self.assertTrue(all(report['summary'][a]['matched'] == 9 for a in q.PROMPTS))
            self.assertFalse(report['productionEnablement'])
            self.assertEqual(q.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                q.run(path)
            self.assertEqual(len(recorder.calls), 18)
            report['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '实际输入'):
                q.audit(path)

    def test_transport_failure_stops_both_arms(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as directory, patch.object(q.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(q.f, '_gateway', return_value=recorder):
            report = q.run(Path(directory)/'report.json')
            self.assertEqual(report['status'], 'blocked')
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(sum(s['not_run'] for s in report['summary'].values()), 17)

    def test_save_failure_stops_before_next_call(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(q.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(q.f, '_gateway', return_value=recorder), patch.object(
                q.f, 'save_checkpoint', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(q.f.CheckpointError):
                q.run(Path(directory)/'report.json')
            self.assertEqual(len(recorder.calls), 1)
