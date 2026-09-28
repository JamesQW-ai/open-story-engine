"""Fixed complete propositions isolate semantic support from extraction errors."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_proposition_eval as p


class PropositionEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labels, cls.cases, cls.template = p.load_labels()

    def annotation(self, name):
        return copy.deepcopy(next(c for c in self.labels['cases'] if c['caseId'] == name))

    def payload(self, annotation):
        return p.model_input(self.cases[annotation['caseId']], annotation, self.template)

    def response(self, annotation):
        return {'checks': [dict(id=x['id'], verdict=x['expectedVerdict'] if x['expectedVerdict'] != 'needs_review' else 'unsupported',
                                basis=x['expectedBasis'], sources=x['evidence'], reason=x['rationale'])
                           for x in annotation['propositions']]}

    def test_labels_cover_originals_without_changing_fixture(self):
        self.assertEqual(len(self.labels['cases']), 12)
        self.assertEqual(sum(len(a['propositions']) for a in self.labels['cases']), 34)
        self.assertEqual(sum(x['expectedVerdict'] == 'needs_review' for a in self.labels['cases'] for x in a['propositions']), 1)
        a = self.annotation('recorded_v11')
        location = next(x for x in a['propositions'] if '伤者在封山线内' in x['statement'])
        self.assertEqual(location['expectedVerdict'], 'contradicted')
        self.assertTrue(location['evidence'])

    def test_model_input_excludes_gold_and_selected_references(self):
        a = self.annotation('recorded_v11')
        payload = self.payload(a)
        self.assertEqual(set(payload), {'draft', 'input', 'statements', 'publicEvidence'})
        for statement in payload['statements'].values():
            self.assertEqual(set(statement), {'statement', 'quote', 'paragraphId'})
        changed = copy.deepcopy(a)
        for prop in changed['propositions']:
            prop.update(expectedVerdict='unsupported', evidence=[], rationale='different', allowedBases=[''])
        self.assertEqual(self.payload(changed), payload)

    def test_time_and_condition_remain_in_complete_single_events(self):
        for name, qualifier in [('reported_to_completed', '三年前'), ('expanded_permission', '只要赶在落锁前')]:
            a = self.annotation(name)
            self.assertEqual(len(a['propositions']), 1)
            self.assertIn(qualifier, a['propositions'][0]['statement'])
            self.assertEqual(a['propositions'][0]['expectedVerdict'], 'unsupported')

    def test_correct_refusals_with_wrong_verdict_do_not_match(self):
        a = self.annotation('reported_to_completed')
        data = self.response(a)
        data['checks'][0].update(verdict='contradicted', sources=[dict(id='module:opening:evidence:1', quote='说要替宗门押送一批引路灯')])
        result = p.assess(data, self.payload(a), a)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(result['mismatches'][0]['expectedVerdict'], 'unsupported')

    def test_shared_quote_does_not_merge_opposite_proposition_labels(self):
        a = self.annotation('recorded_v11')
        data = self.response(a)
        location = next(x for x in a['propositions'] if '伤者在封山线内' in x['statement'])
        check = next(x for x in data['checks'] if x['id'] == location['id'])
        check.update(verdict='nonfactual', basis='ordinary_reaction', sources=[])
        result = p.assess(data, self.payload(a), a)
        self.assertEqual([x['id'] for x in result['mismatches']], [location['id']])

    def test_pending_annotation_cannot_be_counted_as_pass(self):
        a = self.annotation('recorded_v11')
        result = p.assess(self.response(a), self.payload(a), a)
        self.assertEqual(result['status'], 'annotation_pending')
        self.assertEqual(len(result['matchedPropositions']), 15)
        self.assertEqual(len(result['pendingAnnotations']), 1)

    def test_action_rejection_is_mismatch_and_missing_checks_are_invalid(self):
        a = self.annotation('ordinary_action')
        data = self.response(a)
        data['checks'][0].update(verdict='unsupported', basis='')
        self.assertEqual(p.assess(data, self.payload(a), a)['status'], 'mismatched')
        self.assertEqual(p.assess({'checks': []}, self.payload(a), a)['status'], 'invalid_review')
        data['checks'][0].update(verdict='contradicted')
        self.assertEqual(p.assess(data, self.payload(a), a)['status'], 'invalid_review')

    def test_invalid_label_offsets_and_evidence_fail_before_requests(self):
        for kind in ('offset', 'source', 'missing_case'):
            labels = copy.deepcopy(self.labels)
            if kind == 'offset': labels['cases'][0]['propositions'][0]['start'] += 1
            elif kind == 'source': labels['cases'][0]['propositions'][0]['evidence'][0]['quote'] = '不存在的来源'
            else: labels['cases'].pop()
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                path = Path(directory)/'labels.json'
                path.write_text(json.dumps(labels))
                with self.assertRaises(ValueError): p.load_labels(path)

    def test_run_saves_one_call_per_case_and_protects_existing_output(self):
        a = self.annotation('ordinary_action')
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            content = json.dumps(self.response(a))
            recorder.calls.append(dict(messages=messages, content=content))
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory, patch.object(p, 'load_labels', return_value=({**self.labels, 'cases':[a]}, {'ordinary_action':self.cases['ordinary_action']}, self.template)), \
             patch.object(p, 'writer_config_from_env', return_value=config), patch.object(p, '_gateway', return_value=recorder):
            output = Path(directory)/'report.json'
            report = p.run(output)
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['propositionCounts'], dict(matched=2, mismatched=0, pending=0))
            self.assertEqual(json.loads(output.read_text()), report)
            with self.assertRaises(FileExistsError): p.run(output)

    def test_transport_failure_stops_remaining_cases(self):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            recorder.calls.append(dict(messages=messages, error='HTTP 400'))
            raise p.LlmError('HTTP 400', code='transport_error')
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory, patch.object(p, 'writer_config_from_env', return_value=config), \
             patch.object(p, '_gateway', return_value=recorder):
            report = p.run(Path(directory)/'report.json')
        self.assertEqual(report['actualCalls'], 1)
        self.assertEqual(report['summary']['not_run'], 11)
        self.assertEqual(report['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
