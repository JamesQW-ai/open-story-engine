"""Verify indexing, blind fidelity, fail-closed stage ordering, and scoring."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_indexed_pipeline as p


class IndexedPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.template = p.load_fixture()

    def case(self, name='derived_symptom'):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == name))

    def extraction(self, payload):
        return {'units': [{'id': uid, 'statements': [unit['quote']]} for uid, unit in payload['units'].items()]}

    def fidelity(self, payload, verdict='faithful'):
        return {'units': [dict(id=uid, verdict=verdict, reason='逐单元对比原稿') for uid in payload['units']]}

    def test_program_spans_preserve_every_paragraph_character_and_offsets(self):
        for case in self.fixture['cases']:
            payload = p.indexed_input(case)
            for number, paragraph in enumerate(case['draft'].split('\n\n'), 1):
                units = [u for u in payload['units'].values() if u['paragraphId'] == f'P{number}']
                self.assertEqual(''.join(u['quote'] for u in units), paragraph)
                for unit in units:
                    self.assertEqual(case['draft'][unit['start']:unit['end']], unit['quote'])
                    self.assertLess(unit['start'], unit['end'])

    def test_multiple_claims_can_share_program_owned_span(self):
        payload = p.indexed_input(self.case())
        data = self.extraction(payload)
        data['units'][0]['statements'] = ['伤者的胸口在动。', '伤者的胸口此前也在动。']
        statements = p.extract_statements(data, payload)
        self.assertEqual(len(statements), 2)
        self.assertEqual(len({s['quote'] for s in statements.values()}), 1)

    def test_model_cannot_override_quote_or_offset_or_supply_judgment(self):
        payload = p.indexed_input(self.case())
        for key in ('quote', 'start', 'verdict'):
            data = self.extraction(payload)
            data['units'][0][key] = 'forged'
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.extract_statements(data, payload)

    def test_missing_duplicate_and_empty_claims_are_rejected(self):
        payload = p.indexed_input(self.case())
        valid = self.extraction(payload)
        for data in ({'units': []}, {'units': valid['units'] * 2},
                     {'units': [{'id': next(iter(payload['units'])), 'statements': []}]}):
            with self.assertRaises(ValueError):
                p.extract_statements(data, payload)

    def test_fidelity_never_receives_evidence_intent_or_expected_answers(self):
        case = self.case('reported_to_completed')
        payload = p.indexed_input(case)
        statements = p.extract_statements(self.extraction(payload), payload)
        blind = p.fidelity_input(payload, statements)
        self.assertEqual(set(blind), {'draft', 'units', 'statements'})
        self.assertEqual(blind['draft'], case['draft'])
        support = p.support_input(case, statements, self.template)
        self.assertEqual(set(support), {'draft', 'input', 'statements', 'publicEvidence'})
        self.assertEqual(support['publicEvidence'], p.grounding_input_evidence(self.template))

    def test_fidelity_requires_all_units_and_disallows_fact_verdict(self):
        payload = p.indexed_input(self.case('recorded_v11'))
        self.assertEqual(p.fidelity_issues(self.fidelity(payload), payload), [])
        for verdict in ('lossy', 'uncertain'):
            self.assertTrue(p.fidelity_issues(self.fidelity(payload, verdict), payload))
        for data in ({'units': []}, self.fidelity(payload, 'supported')):
            with self.assertRaises(ValueError):
                p.fidelity_issues(data, payload)

    def test_shared_span_rejection_cannot_automatically_pass_target_match(self):
        case = self.case()
        payload = p.indexed_input(case)
        data = self.extraction(payload)
        data['units'][0]['statements'] *= 2
        statements = p.extract_statements(data, payload)
        support = p.support_input(case, statements, self.template)
        checks = [dict(id=sid, verdict='unsupported', basis='', sources=[], reason='缺证') for sid in statements]
        result = p.assess_support({'checks': checks}, support, case)
        self.assertEqual(result['status'], 'ambiguous_rejection')
        self.assertTrue(result['requiresReview'])
        checks[0]['extraction'] = 'faithful'
        self.assertEqual(p.assess_support({'checks': checks}, support, case)['status'], 'invalid_review')

    def run_mock(self, verdict='faithful', fail_stage=None):
        case = self.case()
        payload = p.indexed_input(case)
        statements = p.extract_statements(self.extraction(payload), payload)
        responses = [self.extraction(payload), self.fidelity(payload, verdict),
                     {'checks': [dict(id=sid, verdict='unsupported', basis='', sources=[], reason='缺证')
                                 for sid in statements]}]
        recorder = SimpleNamespace(calls=[], gateway=SimpleNamespace(max_tokens=8192, reasoning_effort=None, timeout_seconds=120))
        def complete(messages):
            number = len(recorder.calls)
            content = json.dumps(responses[number])
            recorder.calls.append(dict(messages=messages, content=content))
            if number == fail_stage:
                raise p.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with patch.object(p, 'load_fixture', return_value=({**self.fixture, 'cases': [case]}, self.template)), \
                 patch.object(p, 'writer_config_from_env', return_value=config), \
                 patch.object(p, '_gateway', return_value=recorder):
                report = p.run(output)
                self.assertEqual(json.loads(output.read_text()), report)
                with self.assertRaises(FileExistsError):
                    p.run(output)
        return report

    def test_lossy_and_uncertain_fidelity_skip_support_without_retry(self):
        for verdict in ('lossy', 'uncertain'):
            report = self.run_mock(verdict)
            self.assertEqual(report['actualCalls'], 2)
            self.assertEqual(report['summary']['extraction_error'], 1)

    def test_successful_fidelity_allows_one_support_request(self):
        report = self.run_mock()
        self.assertEqual(report['actualCalls'], 3)
        self.assertEqual(report['callLimit'], 3)
        self.assertEqual(report['summary']['matched'], 1)
        self.assertFalse(report['acceptance'])
        support = json.loads(report['cases'][0]['calls'][2]['messages'][1]['content'])
        self.assertNotIn('fidelity', support)

    def test_transport_error_stops_at_each_stage(self):
        for stage in range(3):
            report = self.run_mock(fail_stage=stage)
            self.assertEqual(report['actualCalls'], stage + 1)
            self.assertEqual(report['status'], 'blocked_model_error')


if __name__ == '__main__':
    unittest.main()
