"""Exercise independent stage inputs and conservative experimental scoring."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_claim_pipeline as pipeline


class ClaimPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.template = pipeline.load_fixture()

    def case(self, name='derived_symptom'):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == name))

    def extraction(self, case):
        payload = pipeline.extraction_input(case)
        return {'units': [{'id': uid, 'parts': [{'quote': unit['claim'], 'statement': unit['claim']}]}
                          for uid, unit in payload['units'].items()]}

    def payload(self, case):
        statements = pipeline.validate_extraction(self.extraction(case), pipeline.extraction_input(case))
        return pipeline.verification_input(case, statements, self.template)

    def verdict(self, payload, verdict='unsupported'):
        return {'checks': [dict(id=cid, extraction='faithful', verdict=verdict, basis='',
                                sources=[], reason='来源未确认具体体征') for cid in payload['statements']]}

    def test_extractor_never_receives_evidence_labels_or_old_reviews(self):
        for case in self.fixture['cases']:
            payload = pipeline.extraction_input(case)
            self.assertEqual(set(payload), {'draft', 'units'})
            self.assertEqual(payload['draft'], case['draft'])

    def test_verifier_receives_only_required_current_material(self):
        case = self.case()
        payload = self.payload(case)
        self.assertEqual(set(payload), {'draft', 'input', 'statements', 'publicEvidence'})
        self.assertEqual(payload['publicEvidence'], pipeline.grounding_input_evidence(self.template))
        self.assertFalse({'contextProjection', 'expected', 'targets', 'kind'} & payload.keys())

    def test_extractor_cannot_supply_a_verdict_or_kind(self):
        case = self.case()
        data = self.extraction(case)
        data['units'][0]['parts'][0]['kind'] = 'current'
        with self.assertRaises(ValueError):
            pipeline.validate_extraction(data, pipeline.extraction_input(case))

    def test_omitted_negation_and_missing_unit_are_invalid(self):
        case = self.case('negated_life')
        data = self.extraction(case)
        data['units'][0]['parts'][0]['quote'] = data['units'][0]['parts'][0]['quote'].replace('不再', '')
        with self.assertRaises(ValueError):
            pipeline.validate_extraction(data, pipeline.extraction_input(case))
        with self.assertRaises(ValueError):
            pipeline.validate_extraction({'units': []}, pipeline.extraction_input(case))

    def test_duplicates_and_missing_verification_are_invalid(self):
        case = self.case()
        payload = self.payload(case)
        data = self.verdict(payload)
        data['checks'] *= 2
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'invalid_review')
        self.assertEqual(pipeline.assess({'checks': []}, payload, case)['status'], 'invalid_review')

    def test_lossy_extraction_cannot_count_as_correct_rejection(self):
        case = self.case()
        payload = self.payload(case)
        data = self.verdict(payload)
        data['checks'][0]['extraction'] = 'lossy'
        result = pipeline.assess(data, payload, case)
        self.assertEqual(result['status'], 'extraction_error')
        self.assertTrue(result['violations'])

    def test_unsupported_exact_target_is_detected(self):
        case = self.case()
        payload = self.payload(case)
        result = pipeline.assess(self.verdict(payload), payload, case)
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['detectedTargets'], case['targets'])

    def test_valid_but_insufficient_source_is_still_a_false_negative(self):
        case = self.case()
        payload = self.payload(case)
        data = self.verdict(payload, 'supported')
        data['checks'][0]['sources'] = [{'id': 'module:opening:knownFacts:2', 'quote': '伤者仍活着'}]
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'false_negative')
        data['checks'][0]['sources'][0]['id'] = 'fake'
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'invalid_review')

    def test_target_hit_does_not_hide_additional_rejection(self):
        case = self.case('recorded_v11')
        payload = self.payload(case)
        data = self.verdict(payload, 'nonfactual')
        for check in data['checks']:
            check['basis'] = 'ordinary_reaction'
            quote = payload['statements'][check['id']]['quote']
            if '胸口还在动' in quote or '他还活着' in quote:
                check.update(verdict='unsupported', basis='')
        result = pipeline.assess(data, payload, case)
        self.assertEqual(result['status'], 'unexpected_rejection')
        self.assertEqual(result['detectedTargets'], case['targets'])
        self.assertEqual(result['missedTargets'], [])
        self.assertEqual([v['claim'] for v in result['additionalRejections']], ['“他还活着。”'])
        self.assertTrue(result['requiresReview'])

    def test_target_miss_and_additional_rejection_are_both_retained(self):
        case = self.case('recorded_v11')
        payload = self.payload(case)
        data = self.verdict(payload, 'nonfactual')
        for check in data['checks']:
            check['basis'] = 'ordinary_reaction'
            if '他还活着' in payload['statements'][check['id']]['quote']:
                check.update(verdict='unsupported', basis='')
        result = pipeline.assess(data, payload, case)
        self.assertEqual(result['status'], 'false_negative')
        self.assertEqual(result['detectedTargets'], [])
        self.assertEqual(result['missedTargets'], case['targets'])
        self.assertEqual(len(result['additionalRejections']), 1)
        data['checks'][0]['extraction'] = 'lossy'
        result = pipeline.assess(data, payload, case)
        self.assertEqual(result['status'], 'extraction_error')
        self.assertEqual(result['missedTargets'], case['targets'])
        self.assertEqual(len(result['additionalRejections']), 1)

    def test_false_positive_and_nonfactual_boundary(self):
        case = self.case('ordinary_action')
        payload = self.payload(case)
        data = self.verdict(payload)
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'false_positive')
        for check in data['checks']:
            check.update(verdict='nonfactual', basis='authorized_action')
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'matched')
        data['checks'][0]['basis'] = 'reasonable_fact'
        self.assertEqual(pipeline.assess(data, payload, case)['status'], 'invalid_review')

    def test_invalid_field_types_are_format_errors(self):
        case = self.case()
        payload = self.payload(case)
        for field in ('extraction', 'verdict', 'id', 'sources', 'reason'):
            with self.subTest(field=field):
                data = self.verdict(payload)
                data['checks'][0][field] = {}
                self.assertEqual(pipeline.assess(data, payload, case)['status'], 'invalid_review')

    def test_invalid_extraction_skips_verifier_without_retry(self):
        case = self.case()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            recorder.calls.append({'messages': messages, 'content': '{"units": []}'})
            return SimpleNamespace(content='{"units": []}')
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            with patch.object(pipeline, 'load_fixture', return_value=({**self.fixture, 'cases': [case]}, self.template)), \
                 patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway', return_value=recorder):
                report = pipeline.run(Path(directory) / 'result.json')
        self.assertEqual(report['actualCalls'], 1)
        self.assertEqual(report['summary']['invalid_extraction'], 1)
        self.assertFalse(report['acceptance'])

    def test_interruption_preserves_extraction_before_verification(self):
        case = self.case()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            if recorder.calls:
                raise KeyboardInterrupt
            content = json.dumps(self.extraction(case))
            recorder.calls.append({'messages': messages, 'content': content})
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with patch.object(pipeline, 'load_fixture', return_value=({**self.fixture, 'cases': [case]}, self.template)), \
                 patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway', return_value=recorder):
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.run(output)
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(report['actualCalls'], 1)
            self.assertTrue(report['cases'][0]['statements'])
            self.assertEqual(report['cases'][0]['calls'][0]['stage'], 'extract')
            with patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway') as gateway:
                with self.assertRaises(FileExistsError):
                    pipeline.run(output)
                gateway.assert_not_called()

    def test_missing_json_instruction_stops_before_calls_or_output(self):
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            prompt = Path(directory) / 'prompt.md'
            prompt.write_text('只返回结构化对象')
            output = Path(directory) / 'result.json'
            with patch.object(pipeline, 'PROMPTS', {'extract': prompt, 'verify': prompt}), \
                 patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway') as gateway:
                with self.assertRaisesRegex(ValueError, 'JSON'):
                    pipeline.run(output)
                gateway.assert_not_called()
                self.assertFalse(output.exists())

    def test_checkpoint_interrupted_serialization_preserves_previous_bytes(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            pipeline.save_checkpoint(output, {'status': 'incomplete', 'calls': [1]}, create=True)
            previous = output.read_bytes()
            def interrupted_dump(data, handle, **kwargs):
                handle.write('{"status":')
                raise KeyboardInterrupt
            with patch.object(pipeline.json, 'dump', side_effect=interrupted_dump):
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.save_checkpoint(output, {'status': 'completed'})
            self.assertEqual(output.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_checkpoint_fsync_and_replace_failures_preserve_previous_bytes(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            pipeline.save_checkpoint(output, {'calls': [1]}, create=True)
            previous = output.read_bytes()
            for operation in ('fsync', 'replace'):
                with self.subTest(operation=operation), patch.object(pipeline.os, operation, side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        pipeline.save_checkpoint(output, {'calls': [1, 2]})
                self.assertEqual(output.read_bytes(), previous)
                self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_interrupted_initial_checkpoint_publishes_nothing(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with patch.object(pipeline.os, 'fsync', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.save_checkpoint(output, {'calls': []}, create=True)
            self.assertFalse(output.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_checkpoint_initial_publish_is_exclusive_and_updates_are_complete(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            pipeline.save_checkpoint(output, {'calls': []}, create=True)
            previous = output.read_bytes()
            with self.assertRaises(FileExistsError):
                pipeline.save_checkpoint(output, {'calls': [99]}, create=True)
            self.assertEqual(output.read_bytes(), previous)
            pipeline.save_checkpoint(output, {'calls': [1]})
            self.assertEqual(json.loads(output.read_text()), {'calls': [1]})
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_failed_checkpoint_stops_before_another_model_call(self):
        case = self.case()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            content = json.dumps(self.extraction(case))
            recorder.calls.append({'messages': messages, 'content': content})
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'result.json'
            with patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway', return_value=recorder), \
                 patch.object(pipeline.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(pipeline.CheckpointError):
                    pipeline.run(output)
            self.assertEqual(len(recorder.calls), 1)
            self.assertEqual(json.loads(output.read_text())['status'], 'incomplete')
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_run_summary_retains_additional_rejections(self):
        case = self.case('recorded_v11')
        payload = self.payload(case)
        data = self.verdict(payload, 'nonfactual')
        for check in data['checks']:
            check['basis'] = 'ordinary_reaction'
            if any(q in payload['statements'][check['id']]['quote'] for q in ('胸口还在动', '他还活着')):
                check.update(verdict='unsupported', basis='')
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            content = json.dumps(data if recorder.calls else self.extraction(case))
            recorder.calls.append({'messages': messages, 'content': content})
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            with patch.object(pipeline, 'load_fixture', return_value=({**self.fixture, 'cases': [case]}, self.template)), \
                 patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway', return_value=recorder):
                report = pipeline.run(Path(directory) / 'result.json')
        self.assertEqual(report['summary']['matched'], 0)
        self.assertEqual(report['summary']['unexpected_rejection'], 1)
        self.assertEqual(report['scoringCounts'], dict(scoredCases=1, unscoredCases=0, detectedTargets=1,
                         missedTargets=0, additionalRejections=1, casesRequiringReview=1))

    def test_transport_failure_stops_remaining_cases_without_retry(self):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            recorder.calls.append({'messages': messages, 'error': 'HTTP 400'})
            raise pipeline.LlmError('HTTP 400', code='transport_error')
        recorder.complete_json = complete
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        with TemporaryDirectory() as directory:
            with patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                 patch.object(pipeline, '_gateway', return_value=recorder):
                report = pipeline.run(Path(directory) / 'result.json')
        self.assertEqual(report['status'], 'blocked_model_error')
        self.assertEqual(report['actualCalls'], 1)
        self.assertEqual(report['summary']['model_error'], 1)
        self.assertEqual(report['summary']['not_run'], len(self.fixture['cases']) - 1)

    def test_malformed_json_is_not_a_transport_failure(self):
        case = self.case()
        config = dict(base_url='unused', api_key='test', model='test', route='test')
        for stage in ('extract', 'verify'):
            recorder = SimpleNamespace(calls=[])
            def complete(messages):
                content = json.dumps(self.extraction(case)) if stage == 'verify' and not recorder.calls else 'invalid'
                recorder.calls.append({'messages': messages, 'content': content})
                return SimpleNamespace(content=content)
            recorder.complete_json = complete
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                with patch.object(pipeline, 'load_fixture', return_value=({**self.fixture, 'cases': [case]}, self.template)), \
                     patch.object(pipeline, 'writer_config_from_env', return_value=config), \
                     patch.object(pipeline, '_gateway', return_value=recorder):
                    report = pipeline.run(Path(directory) / 'result.json')
                self.assertEqual(report['status'], 'completed')
                self.assertEqual(report['summary']['invalid_extraction' if stage == 'extract' else 'invalid_review'], 1)
                self.assertEqual(report['summary']['model_error'], 0)


if __name__ == '__main__':
    unittest.main()
