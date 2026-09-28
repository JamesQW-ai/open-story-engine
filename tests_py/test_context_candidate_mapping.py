import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_candidate_mapping as m


class CandidateMappingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = m.load_cases()

    def case(self, cid):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))

    def response(self, case):
        return dict(status=case['expectedStatus'], normal=copy.deepcopy(case['expectedNormal']),
                    limitations=[dict(kind=k,quote=case['limitationQuote']) for k in case['expectedLimitations']],
                    reason='原文与指定关系对应，保留限定。')

    def assess(self, data, case):
        return m.assess(data,case,self.fixture['entities'])

    def test_payload_excludes_source_truth_gold_review_and_case_names(self):
        case = self.case('original_C10')
        payload = m.model_input(case,self.fixture['entities'])
        self.assertEqual(set(payload), {'draft','target','entities'})
        self.assertEqual(set(payload['target']), {'start','end','quote'})
        for descriptor in payload['entities'].values():
            self.assertEqual(set(descriptor), {'kind','mentions'})
        case.update(id='secret',expectedNormal=None,expectedStatus='needs_review',rationale='secret')
        self.assertEqual(payload,m.model_input(case,self.fixture['entities']))

    def test_five_mappings_and_five_abstentions_scored_separately(self):
        counts = {}
        for case in self.fixture['cases']:
            result = self.assess(self.response(case),case)
            counts[result['status']] = counts.get(result['status'],0)+1
            self.assertTrue(result['requiresSemanticReview'])
        self.assertEqual(counts,dict(mapped=5,abstained=5))

    def test_rewriting_original_to_true_position_is_mapping_failure(self):
        case = self.case('original_C10')
        data = self.response(case)
        data['normal']['value'] = 'outside'
        result = self.assess(data,case)
        self.assertEqual(result['status'],'mismatched')
        self.assertEqual(result['differences'],['value'])

    def test_negation_is_not_replaced_with_opposite_side(self):
        case = self.case('position_not_in')
        data = self.response(case)
        data['normal'].update(value='outside',polarity='positive')
        result = self.assess(data,case)
        self.assertEqual(result['status'],'mismatched')
        self.assertEqual(set(result['differences']),{'value','polarity'})

    def test_dropping_qualifiers_and_blanket_abstention_both_fail(self):
        for cid in ('past','condition','possible','unknown_subject','unknown_boundary'):
            case = self.case(cid)
            self.assertEqual(self.assess(self.response(self.case('position_in')),case)['status'],'mismatched')
        case = self.case('position_in')
        data = dict(status='needs_review',normal=None,limitations=[dict(kind='ambiguity',quote='伤者')],reason='不确定')
        self.assertEqual(self.assess(data,case)['status'],'mismatched')

    def test_malformed_and_unbound_limitations_are_invalid(self):
        case = self.case('past')
        for data in (None, [], {}, dict(self.response(case),authority='authoritative')):
            self.assertEqual(self.assess(data,case)['status'],'invalid_response')
        data = self.response(case)
        data['limitations'][0]['quote'] = '三年前'
        self.assertEqual(self.assess(data,case)['status'],'invalid_response')
        data['limitations'] = [dict(kind='time',quote='昨天')]*2
        self.assertEqual(self.assess(data,case)['status'],'invalid_response')

    def test_fixture_changes_fail_preflight(self):
        for kind in ('hash','gold','entity','target'):
            data = copy.deepcopy(self.fixture)
            if kind=='hash': data['bindings'][next(iter(data['bindings']))]='0'*64
            elif kind=='gold': data['cases'][0]['expectedNormal']['value']='inside'
            elif kind=='entity': data['entities']['scene:wounded_stranger']['side']='outside'
            else: data['cases'][0]['target']['end']-=1
            with self.subTest(kind=kind),TemporaryDirectory() as directory:
                path=Path(directory)/'cases.json'
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):m.load_cases(path)

    def recorder(self, *, fail=False):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            case = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(self.response(case))
            recorder.calls.append(dict(messages=messages,content=content))
            if fail:raise m.LlmError('HTTP 400',code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json=complete
        return recorder

    def test_one_call_each_and_no_overwrite_or_review_record_mutation(self):
        recorder=self.recorder()
        before=m.REVIEW.read_bytes()
        with TemporaryDirectory() as directory,patch.object(m,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')),patch.object(m,'_gateway',return_value=recorder):
            output=Path(directory)/'report.json'
            report=m.run(output)
            self.assertEqual(report['actualCalls'],10)
            self.assertEqual(report['summary']['mapped'],5)
            self.assertEqual(report['summary']['abstained'],5)
            self.assertEqual(json.loads(output.read_text()),report)
            with self.assertRaises(FileExistsError):m.run(output)
        self.assertEqual(m.REVIEW.read_bytes(),before)

    def test_transport_failure_stops_remaining_cases(self):
        recorder=self.recorder(fail=True)
        with TemporaryDirectory() as directory,patch.object(m,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')),patch.object(m,'_gateway',return_value=recorder):
            report=m.run(Path(directory)/'report.json')
        self.assertEqual(report['actualCalls'],1)
        self.assertEqual(report['summary']['not_run'],9)

    def test_checkpoint_failure_stops_before_next_model_request(self):
        recorder=self.recorder()
        with TemporaryDirectory() as directory,patch.object(m,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')),patch.object(m,'_gateway',return_value=recorder),patch.object(m,'save_checkpoint',side_effect=[None,OSError('disk full')]):
            with self.assertRaises(m.CheckpointError):m.run(Path(directory)/'report.json')
        self.assertEqual(len(recorder.calls),1)
