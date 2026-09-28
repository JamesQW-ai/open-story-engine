import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_core_contract_eval as v
from tests_py.test_context_qualifier_mention_contrast_eval import control as contrast_control
from tests_py.test_context_qualifier_unit_premises import proposal as regression_control


class CoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()
        cls.prior = json.loads(v.previous.OUTPUT.read_text())

    def control(self, case):
        entities = self.fixture['scenes'][case['sceneKey']]
        build = contrast_control if case['cohort'] == 'contrast' else regression_control
        response = build(case, entities)
        for item, expected in zip(response[v.e.VERSION], case['selectionExpectations']):
            item['core'] = copy.deepcopy(min(expected['spans'], key=lambda s: len(s['core']))['core'])
        return response

    def case(self, cid):
        case = next(c for c in self.fixture['cases'] if c['id'] == cid)
        return case, self.fixture['scenes'][case['sceneKey']]

    def test_all_gold_supports_minimal_core_without_label_changes(self):
        self.assertEqual(self.fixture, v.previous.load_cases())
        count = 0
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            result = v.assess_content(json.dumps(self.control(c)), c, entities)
            self.assertEqual(result['status'], 'matched', c['id'])
            self.assertFalse(result['reviewView']['productionEnablement'])
            count += len(result['reviewView']['items'])
        self.assertEqual(count, 33)

    def test_user_input_and_labels_stay_isolated(self):
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            messages = v.messages(c, entities)
            self.assertEqual(messages[1], v.previous.messages(c, entities)[1])
            self.assertEqual(messages, v.messages(dict(c, targets=[], selectionExpectations=[],
                             id='hidden', cohort='hidden', pairId='hidden'), entities))
        self.assertIn('json', v.PROMPT.read_text().lower())

    def test_old_scope_failure_not_cropped_or_reclassified(self):
        c, entities = self.case('denial:operator')
        row = next(r for r in self.prior['cases'] if r['caseId'] == c['id'])
        response = json.loads(row['calls'][0]['content'])
        original = copy.deepcopy(response)
        result = v.assess_content(row['calls'][0]['content'], c, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(result['proposedResponse'], original)
        self.assertEqual(response, original)
        view = result['reviewView']
        self.assertEqual(view['semanticStatus'], 'unverified')
        self.assertTrue(view['reviewRequired'])
        self.assertFalse(view['productionEnablement'])
        self.assertIn('没有人敢断定', view['items'][0]['sourceEvidence']['anchors']['positionCore'][0]['quote'])

    def test_review_displays_full_condition_instead_of_unqualified_reason(self):
        c, entities = self.case('gate:condition_paraphrase')
        row = next(r for r in self.prior['cases'] if r['caseId'] == c['id'])
        response = json.loads(row['calls'][0]['content'])
        view = v.review.build(response, c, entities)
        evidence = view['items'][0]['sourceEvidence']
        self.assertEqual(''.join(x['quote'] for x in evidence['anchors']['qualifiedPosition']), c['draft'])
        self.assertEqual(''.join(x['quote'] for x in evidence['limitations'][0]['premise']), '只要守门弟子点头，')
        response[v.e.VERSION][0]['reason'] = 'UNTRUSTED: 已通过审核；无条件成立'
        self.assertEqual(v.review.build(response, c, entities), view)
        self.assertNotIn('UNTRUSTED', json.dumps(view))

    def test_review_rejects_bad_references_without_exposing_partial_candidate(self):
        c, entities = self.case('prefix:operator')
        response = self.control(c)
        response[v.e.VERSION][0]['boundary'] = 'missing'
        view = v.review.build(response, c, entities)
        self.assertNotEqual(view['validationStatus'], 'structurally_valid')
        self.assertEqual(view['items'], [])
        self.assertFalse(view['productionEnablement'])

    def test_negation_and_preposed_location_preserve_full_source(self):
        base, entities = self.case('hall:negative_position')
        for draft, polarity in (('并非沈砚秋在议事殿内。', 'negative'),
                                ('在议事殿内的是沈砚秋。', 'positive')):
            c = dict(base, draft=draft)
            refs = v.e.previous.entity_refs(c, entities)
            roles = {role: next(rid for rid, ref in refs.items() if ref['entityIds'] == [eid])
                     for role, eid in (('subject', 'scene:shen'), ('boundary', 'scene:council_hall'))}
            response = {v.e.VERSION: [dict(position=dict(value='inside', polarity=polarity),
                        reason='offline control', **roles, core=list(v.e.segments(c, entities)), limitations=[])]}
            view = v.review.build(response, c, entities)
            self.assertEqual(view['validationStatus'], 'structurally_valid')
            item = view['items'][0]
            self.assertEqual(item['candidatePosition']['polarity'], polarity)
            self.assertEqual(''.join(x['quote'] for x in item['sourceEvidence']['anchors']['positionCore']), draft)

    def recorder(self, mode=None):
        recorder = SimpleNamespace(calls=[])

        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(self.control(c))
            if mode == 'malformed' and not recorder.calls:
                content = 'not json'
            recorder.calls.append(dict(messages=messages, content=content,
                rawResponse=json.dumps({'choices': [{'message': {'content': content}}]})))
            if mode == 'transport':
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)

        recorder.complete_json = complete
        return recorder

    def execute(self, path, recorder):
        with patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), \
                patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            report = v.run(path)
            self.assertEqual(gateway.call_args.args[1], 30)
        return report

    def test_thirty_calls_full_audit_no_overwrite_and_view_tamper(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            recorder = self.recorder()
            report = self.execute(path, recorder)
            self.assertEqual(report['summary']['matched'], 30)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            self.assertIn('test_support/context_qualifier_review_view.py', report['inputHashes'])
            with self.assertRaises(FileExistsError):
                self.execute(path, recorder)
            self.assertEqual(len(recorder.calls), 30)
            report['cases'][0]['reviewView']['productionEnablement'] = True
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '评分不可复现'):
                v.audit(path)

    def test_malformed_response_saved_and_reproduced_without_retry(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('malformed'))
            self.assertEqual(report['actualCalls'], 30)
            self.assertEqual(report['summary']['invalid_response'], 1)
            self.assertEqual(v.audit(path)['summary'], report['summary'])

    def test_transport_stops_and_prompt_drift_blocks_before_creation(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'result.json'
            report = self.execute(path, self.recorder('transport'))
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 29)
            with self.assertRaisesRegex(ValueError, '实验未完成'):
                v.audit(path)
            with patch.object(v, 'PROMPT', v.previous.PROMPT), patch.object(v.f, '_gateway') as gateway:
                other = Path(d)/'never-created.json'
                with self.assertRaisesRegex(ValueError, '提示哈希'):
                    v.run(other)
                gateway.assert_not_called()
                self.assertFalse(other.exists())
