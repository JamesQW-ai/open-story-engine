"""Prevent experiment plumbing from corrupting planner quality evidence."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.llm import Completion, LlmError
from test_support import context_planner_smoke as smoke
from test_support.context_prose_ab import _entry_samples, _writer_prompt


CONFIG = {"base_url": "http://unused.invalid", "api_key": "test", "model": "test", "route": "relay"}


class ContextPlannerSmokeTests(unittest.TestCase):
    def test_transport_requests_keep_separate_json_and_text_budgets(self):
        for override, expected in ((None, 8192), ("4096", 4096)):
            env = {"STORY_LLM_MAX_TOKENS": "8192", "STORY_LLM_TEXT_MAX_TOKENS": "2048"}
            if override:
                env["STORY_LLM_JSON_MAX_TOKENS"] = override
            with self.subTest(override=override), patch.dict(os.environ, env, clear=True):
                recorder = smoke._gateway(CONFIG, 2)
                with patch.object(recorder.gateway, "_json", return_value=('{"ok":true}', '{}')) as request:
                    recorder.complete_json([])
                    self.assertEqual(request.call_args.args[0]["max_tokens"], expected)
                    recorder.complete_text([])
                    self.assertEqual(request.call_args.args[0]["max_tokens"], 2048)
                with self.assertRaisesRegex(LlmError, "调用上限"):
                    recorder.complete_json([])

    def test_actual_draft_calls_receive_plan_pacing_and_revision_feedback(self):
        sample = _entry_samples(1)[0]
        context = copy.deepcopy(sample['context'])
        # A prompt fixture for the official longform; it does not assert plan acceptance.
        context['resultContract'] = {
            'scenePlan': {'stop': '等待守门人回应，不替玩家继续决定',
                          'targetCjk': [120, 260], 'lengthReason': '只完成本轮请求'},
        }
        feedback = '不得擅自递出引荐文书。'
        controls_by_arm = []
        for variant in ('chapter', 'full'):
            recorder = smoke._gateway(CONFIG, 2)
            planner = PlayerNarrativePlanner(recorder, context_resolver=sample['resolver'])
            smoke._install_experimental_prompt(planner, variant)
            with patch.object(recorder.gateway, 'complete_text',
                              return_value=Completion('你留在原地。', '{}', [])):
                for revision, repair in enumerate(('', feedback)):
                    planner._scene_draft(context, sample['selected'], sample['state'],
                                         None, None, [], [], revision, repair)
            payloads = [json.loads(c['messages'][-1]['content'].split('\n\n', 1)[1])
                        for c in recorder.calls]
            for payload in payloads:
                self.assertEqual(payload['writerControls']['resultContract'], context['resultContract'])
                self.assertEqual(payload['writerControls']['pacing']['targetCjk'], [120, 260])
            self.assertEqual(payloads[0]['writerControls']['repair'], '')
            self.assertEqual(payloads[1]['writerControls']['repair'], feedback)
            controls_by_arm.append([p['writerControls'] for p in payloads])
        self.assertEqual(*controls_by_arm)

    def test_production_prompt_is_not_replaced(self):
        planner = PlayerNarrativePlanner(SimpleNamespace(model='test'))
        original = planner._prompt
        smoke._install_experimental_prompt(planner, 'production')
        self.assertEqual(planner._prompt, original)

    def test_standalone_first_draft_prompt_has_no_invented_controls(self):
        sample = _entry_samples(1)[0]
        prompt = _writer_prompt({}, sample['context'], sample['selected'])
        self.assertNotIn('writerControls', prompt)

    def test_gateway_failure_keeps_original_raw_evidence(self):
        error = LlmError('truncated response', 'empty_json')
        error.raw_response = '{"choices":[]}'
        error.observations = [{'outcome': 'failed'}]
        for method in ('complete_json', 'complete_text'):
            recorder = smoke._gateway(CONFIG, 1)
            with patch.object(recorder.gateway, method, side_effect=error):
                with self.assertRaises(LlmError):
                    getattr(recorder, method)([])
            self.assertEqual(recorder.calls[0]['rawResponse'], error.raw_response)
            self.assertEqual(recorder.calls[0]['observations'], error.observations)

    def test_success_records_result_and_audit_without_mutating_sample(self):
        sample = _entry_samples(1)[0]
        before = copy.deepcopy(sample['state'])
        audit = {'operation': 'branch_planner', 'callObservations': []}
        result = {'narrativeText': '你留在原地。'}

        def plan(_self, context, selected, state):
            state.clear()
            return result, audit

        with patch.object(PlayerNarrativePlanner, 'plan', plan):
            record = smoke._run_variant(sample, CONFIG, 'production')
        self.assertEqual(record['result'], result)
        self.assertEqual(record['audit'], audit)
        self.assertNotIn('statePatch', record)
        self.assertEqual(sample['state'], before)

    def test_existing_evidence_rejected_before_model_calls(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            output.write_text('previous evidence')
            with patch.object(smoke, '_run_variant') as run_variant:
                with self.assertRaises(FileExistsError):
                    smoke.run(output)
                run_variant.assert_not_called()
            self.assertEqual(output.read_text(), 'previous evidence')

    def test_interruption_preserves_completed_variant_checkpoint(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            with patch.object(smoke, 'writer_config_from_env', return_value=CONFIG), \
                 patch.object(smoke, '_entry_samples', return_value=[{'sampleId': 'checkpoint'}]), \
                 patch.object(smoke, '_run_variant', side_effect=[{'status': 'failed'}, KeyboardInterrupt]):
                with self.assertRaises(KeyboardInterrupt):
                    smoke.run(output)
            report = json.loads(output.read_text())
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(report['variants'], {'production': {'status': 'failed'}})

    def test_selected_production_does_not_run_experimental_arms(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / 'evidence.json'
            sample = {'sampleId': 'bounded-production'}
            with patch.object(smoke, 'writer_config_from_env', return_value=CONFIG), \
                 patch.object(smoke, '_entry_samples', return_value=[sample]), \
                 patch.object(smoke, '_run_variant', return_value={'status': 'failed'}) as run_variant:
                result = smoke.run(output, variants=('production',))
            run_variant.assert_called_once_with(sample, CONFIG, 'production')
            self.assertEqual(result['selectedVariants'], ['production'])
            self.assertEqual(list(result['variants']), ['production'])
            self.assertFalse(result['acceptance']['passed'])

    def test_invalid_selection_rejected_before_reserving_evidence(self):
        for variants in ((), ('production', 'production'), ('unknown',)):
            with self.subTest(variants=variants), TemporaryDirectory() as directory:
                output = Path(directory) / 'evidence.json'
                with patch.object(smoke, '_run_variant') as run_variant:
                    with self.assertRaises(ValueError):
                        smoke.run(output, variants=variants)
                    run_variant.assert_not_called()
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
