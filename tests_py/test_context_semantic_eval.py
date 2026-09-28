"""Keep semantic evaluation separate from transport and reference validity."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_semantic_eval as semantic


class SemanticEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.template = semantic.load_fixture()

    def case(self, name):
        return next(copy.deepcopy(c) for c in self.fixture['cases'] if c['id'] == name)

    def review(self, case):
        payload = semantic.case_payload(self.template, case)
        return {'checks': [dict(id=cid, kind='current', verdict='supported', sources=[],
                               reason='当前动作', propositions=[dict(quote=c['claim'], claim=c['claim'],
                                   kind='current', verdict='supported', sources=[], reason='当前动作')])
                           for cid, c in payload['paragraphs'].items()], 'knowledgeChecks': []}

    def assess(self, data, case, arm='entailment'):
        return semantic.assess(data, semantic.case_payload(self.template, case), case, arm)

    def test_fixture_covers_required_polarity_subject_condition_and_positive_cases(self):
        self.assertEqual(len(self.fixture['cases']), 12)
        self.assertEqual(sum(c['expected'] == 'allow' for c in self.fixture['cases']), 6)
        self.assertEqual(self.case('recorded_v11')['origin'], 'recorded')
        for name in ('negated_life', 'swapped_subject', 'expanded_permission', 'reported_to_completed'):
            self.assertEqual(self.case(name)['expected'], 'reject')

    def test_saved_false_negative_is_not_counted_as_a_passing_review(self):
        artifact = json.loads((semantic.ROOT / self.fixture['sourceArtifact']).read_text())
        response = json.loads(artifact['calls'][0]['content'])
        result = self.assess(response, self.case('recorded_v11'), 'baseline')
        self.assertEqual(result['status'], 'false_negative')

    def test_case_labels_never_enter_model_input_or_change_evidence(self):
        case = self.case('recorded_v11')
        payload = semantic.case_payload(self.template, case)
        self.assertFalse({'expected', 'targets', 'reason', 'origin'} & payload.keys())
        self.assertEqual(payload['contextProjection'], self.template['contextProjection'])
        self.assertEqual(payload['draft'], self.template['draft'])

    def test_clause_index_preserves_all_characters_and_paragraphs(self):
        for case in self.fixture['cases']:
            with self.subTest(case=case['id']):
                payload = semantic.case_payload(self.template, case, segmentation='clause')
                for pid, paragraph in payload['draft'].items():
                    self.assertEqual(''.join(c['claim'] for c in payload['paragraphs'].values()
                                            if c['paragraphId'] == pid), paragraph)
                self.assertEqual(payload['contextProjection'], self.template['contextProjection'])

    def test_clause_index_separates_compound_claim_without_guessing_semantics(self):
        body = '“他还活着，胸口还在动——也许能救。”\n\n如果门关了，就等在外面。'
        claims = semantic.clause_claims(body)
        self.assertEqual(claims['P1-C2']['claim'], '胸口还在动——')
        self.assertEqual(claims['P2-C1']['claim'], '如果门关了，')
        self.assertEqual(claims['P2-C2']['claim'], '就等在外面。')

    def test_invalid_verdict_not_counted_as_correct_rejection(self):
        case = self.case('negated_life')
        data = self.review(case)
        data['checks'][0]['verdict'] = 'error'
        self.assertEqual(self.assess(data, case)['status'], 'invalid_review')

    def test_missing_unit_and_duplicate_unit_are_invalid(self):
        case = self.case('legal_wait')
        data = self.review(case)
        data['checks'].pop()
        self.assertEqual(self.assess(data, case)['status'], 'invalid_review')
        data = self.review(case)
        data['checks'][-1] = copy.deepcopy(data['checks'][0])
        self.assertEqual(self.assess(data, case)['status'], 'invalid_review')

    def test_propositions_cannot_omit_negation_or_reorder_text(self):
        case = self.case('negated_life')
        data = self.review(case)
        data['checks'][0]['propositions'][0]['quote'] = '伤者已经活着。'
        self.assertEqual(self.assess(data, case)['status'], 'invalid_review')

    def test_supported_wrapper_does_not_erase_rejected_proposition(self):
        case = self.case('negated_life')
        data = self.review(case)
        data['checks'][0]['propositions'][0].update(verdict='contradicted', kind='background', reason='与仍活着相反')
        result = self.assess(data, case)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['detectedTargets'], case['targets'])

    def test_invalid_reference_not_counted_as_semantic_detection(self):
        case = self.case('derived_symptom')
        data = self.review(case)
        data['checks'][0]['propositions'][0].update(verdict='unsupported', sources=[{'id': 'fake', 'quote': '虚构来源内容'}])
        self.assertEqual(self.assess(data, case)['status'], 'invalid_review')

    def test_valid_reference_is_not_semantic_proof(self):
        case = self.case('derived_symptom')
        data = self.review(case)
        prop = data['checks'][0]['propositions'][0]
        prop.update(kind='background', sources=[{'id': 'module:opening:knownFacts:2', 'quote': '伤者仍活着'}])
        self.assertEqual(self.assess(data, case)['status'], 'false_negative')

    def test_rejecting_ordinary_action_counts_as_false_positive(self):
        case = self.case('ordinary_action')
        data = self.review(case)
        self.assertEqual(self.assess(data, case)['status'], 'matched')
        data['checks'][0]['propositions'][0]['verdict'] = 'unsupported'
        self.assertEqual(self.assess(data, case)['status'], 'false_positive')

    def test_existing_artifact_is_not_overwritten_or_sent_to_model(self):
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            output.write_text('old evidence')
            with patch.object(semantic, 'writer_config_from_env', return_value=config), \
                 patch.object(semantic, '_gateway') as gateway:
                with self.assertRaises(FileExistsError):
                    semantic.run(output)
                gateway.assert_not_called()
            self.assertEqual(output.read_text(), 'old evidence')

    def test_interruption_retains_first_call_without_enabling_production(self):
        case = self.case('ordinary_action')
        fixture = {**self.fixture, 'cases': [case]}
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        recorder = SimpleNamespace(calls=[])

        def complete(messages):
            if recorder.calls:
                raise KeyboardInterrupt
            content = json.dumps(self.review(case))
            recorder.calls.append({'messages': messages, 'content': content})
            return SimpleNamespace(content=content)

        recorder.complete_json = complete
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            with patch.object(semantic, 'load_fixture', return_value=(fixture, self.template)), \
                 patch.object(semantic, 'writer_config_from_env', return_value=config), \
                 patch.object(semantic, '_gateway', return_value=recorder):
                with self.assertRaises(KeyboardInterrupt):
                    semantic.run(output)
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(len(report['cases']), 1)
            self.assertEqual(report['cases'][0]['status'], 'matched')
            self.assertFalse(report['acceptance'])
            self.assertFalse(report['productionEnablement'])

    def test_model_override_is_local_to_experiment(self):
        case = self.case('ordinary_action')
        fixture = {**self.fixture, 'cases': [case]}
        config = dict(base_url='unused', api_key='test', model='configured', route='test')
        recorder = SimpleNamespace(calls=[], complete_json=lambda _: SimpleNamespace(content=json.dumps(self.review(case))))
        with TemporaryDirectory() as directory:
            with patch.object(semantic, 'load_fixture', return_value=(fixture, self.template)), \
                 patch.object(semantic, 'writer_config_from_env', return_value=config), \
                 patch.object(semantic, '_gateway', return_value=recorder) as gateway:
                result = semantic.run(Path(directory) / 'report.json', arms=('baseline',), model='candidate')
        self.assertEqual(config['model'], 'configured')
        self.assertEqual(gateway.call_args.args[0], {**config, 'model': 'candidate'})
        self.assertEqual(result['configuredModel'], 'configured')
        self.assertEqual(result['model']['model'], 'candidate')


if __name__ == '__main__':
    unittest.main()
