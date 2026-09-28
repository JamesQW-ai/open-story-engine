import copy
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_selection_strict_eval as v
from tests_py import test_context_qualifier_selection_policy as controls


def proposal(case, entities):
    result = controls.proposal(case, entities)
    result['schemaVersion'] = v.e.VERSION
    for item, expected in zip(result['items'], case['selectionExpectations']):
        item['position'] = copy.deepcopy(expected['position'])
    return result


class SelectionStrictEvalTests(unittest.TestCase):
    def test_prompt_example_separates_premise_and_conclusion(self):
        content = v.PROMPT.read_text()
        example, _ = json.JSONDecoder().raw_decode(content[content.index('{"schemaVersion"'):])
        self.assertEqual(example['schemaVersion'], v.e.VERSION)
        item = example['items'][0]
        condition = item['limitations'][0]
        self.assertFalse(set(condition['premise']) & set(item['core']))
        self.assertTrue(set(condition['premise'] + item['core']) <= set(item['qualified']))
        self.assertIsNotNone(item['position'])
        self.assertIn(item['subject']['segmentId'], item['core'])

    def recorder(self, fail=False):
        fixture = v.load_cases()
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            case = fixture['cases'][len(recorder.calls)]
            content = json.dumps(proposal(case, fixture['scenes'][case['sceneKey']]))
            recorder.calls.append(dict(messages=messages, content=content, rawResponse=json.dumps({'choices':[{'message':{'content':content}}]})))
            if fail:
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_scene_inputs_stay_blind_and_manifest_cannot_change_budget(self):
        fixture = v.load_cases()
        self.assertEqual(sum(len(c['targets']) for c in fixture['cases']), 19)
        for case in fixture['cases']:
            entities = fixture['scenes'][case['sceneKey']]
            expected = v.messages(case, entities)
            self.assertEqual(set(json.loads(expected[1]['content'])), {'sourceBlocks','entities'})
            changed = copy.deepcopy(case)
            changed.update(targets=[], selectionExpectations=[], labelRationale='secret', id='secret')
            self.assertEqual(v.messages(changed, entities), expected)
        manifest = json.loads(v.FIXTURE.read_text())
        manifest['callLimit'] = 19
        with TemporaryDirectory() as directory:
            path = Path(directory)/'manifest.json'
            path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                v.load_cases(path)

    def test_eighteen_once_only_calls_audit_and_no_overwrite(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
            path = Path(directory)/'report.json'
            report = v.run(path)
            self.assertEqual(report['actualCalls'],18)
            self.assertEqual(report['summary']['matched'],18)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                v.run(path)
            self.assertEqual(len(recorder.calls),18)
            original = copy.deepcopy(report)
            report['cases'][0]['calls'][0]['content']='{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'原始响应'):
                v.audit(path)
            report = original
            report['cases'][0]['calls'][0]['messages'][1]['content']='{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'实际输入'):
                v.audit(path)

    def test_transport_failure_stops_both_scenes(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
            report = v.run(Path(directory)/'report.json')
            self.assertEqual(report['actualCalls'],1)
            self.assertEqual(report['summary']['not_run'],17)

    def test_save_failure_stops_before_next_request(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder), patch.object(
                v.f,'save_checkpoint',side_effect=[None,OSError('disk full')]):
            with self.assertRaises(v.f.CheckpointError):
                v.run(Path(directory)/'report.json')
            self.assertEqual(len(recorder.calls),1)

    def test_linear_input_preserves_all_source_and_marks_every_segment_once(self):
        fixture = v.load_cases()
        for case in fixture['cases']:
            entities = fixture['scenes'][case['sceneKey']]
            source = ''.join(v.model_input(case, entities)['sourceBlocks'])
            self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', source), case['draft'])
            self.assertEqual(re.findall(r'⟦([^⟧]+)⟧', source), list(v.e.segments(case, entities)))
        case = copy.deepcopy(fixture['cases'][0])
        case['draft'] += '⟦P1-U1-S1⟧'
        with self.assertRaises(ValueError):
            v.model_input(case, entities)

    def test_old_prompt_is_rejected_before_gateway_or_output_creation(self):
        with TemporaryDirectory() as directory, patch.object(v, 'PROMPT', v.f.ROOT/'test_support/prompts/context_qualifier_selection_policy.md'), patch.object(v.f, '_gateway') as gateway:
            path = Path(directory)/'report.json'
            with self.assertRaisesRegex(ValueError, '提示绑定'):
                v.run(path)
            gateway.assert_not_called()
            self.assertFalse(path.exists())

    def test_schema_version_drift_is_rejected_even_with_same_prompt_hash(self):
        with patch.object(v.e, 'VERSION', 'wrong-version'), patch.object(v.f, '_gateway') as gateway:
            with self.assertRaisesRegex(ValueError, '提示绑定'):
                v.check_prompt_binding()
            gateway.assert_not_called()

    def test_punctuation_groups_keep_condition_with_result_and_isolate_next_sentence(self):
        fixture = v.load_cases()
        by_id = {c['id']: c for c in fixture['cases']}
        for cid, count in [('gate:both_paraphrase', 1), ('gate:unrelated_condition', 2)]:
            case = by_id[cid]
            blocks = v.model_input(case, fixture['scenes'][case['sceneKey']])['sourceBlocks']
            self.assertEqual(len(blocks), count)
        self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', blocks[1]), '伤者在封山线外。')

    def test_missing_reference_and_scope_error_are_both_reported_without_repair(self):
        fixture = v.load_cases()
        case = next(c for c in fixture['cases'] if c['id']=='gate:unrelated_condition')
        entities = fixture['scenes'][case['sceneKey']]
        proposed = proposal(case, entities)
        item = proposed['items'][0]
        del item['subject']
        item['qualified'] = list(v.e.segments(case, entities))
        item['limitations'] = [dict(kind='condition', cue=dict(segmentId='P1-U1-S1',quote='如果',occurrence=0),premise=['P1-U1-S1'])]
        original = copy.deepcopy(proposed)
        result = v.e.assess(proposed, case, entities)
        self.assertEqual(result['status'], 'invalid_response')
        self.assertEqual(result['fieldDiagnostics'], [dict(itemIndex=0, missing=['subject'], extra=[])])
        self.assertEqual(result['scopeDiagnostics'][0]['code'], 'qualified_position_crosses_sentence')
        self.assertEqual(result['proposedResponse'], original)
        self.assertEqual(proposed, original)
        self.assertNotIn('projection', result)
        item['subject'] = proposal(case, entities)['items'][0]['subject']
        result = v.e.assess(proposed, case, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertTrue(result['scopeDiagnostics'])

    def test_extra_fields_are_named_and_null_is_not_a_missing_key(self):
        fixture = v.load_cases()
        case = fixture['cases'][0]
        entities = fixture['scenes'][case['sceneKey']]
        proposed = proposal(case, entities)
        proposed['items'][0]['status'] = 'candidate'
        result = v.e.inspect(proposed, case, entities)
        self.assertEqual(result['fieldDiagnostics'][0], dict(itemIndex=0, missing=[], extra=['status']))
        del proposed['items'][0]['status']
        proposed['items'][0]['subject'] = None
        self.assertEqual(v.e.inspect(proposed, case, entities)['fieldDiagnostics'], [])

    def test_source_roundtrip_preserves_blank_lines_and_spacing(self):
        fixture = v.load_cases()
        case = copy.deepcopy(fixture['cases'][0])
        entities = fixture['scenes'][case['sceneKey']]
        case['draft'] = '  伤者在封山线内。\n\n\n伤者在封山线外；  守门弟子在封山线内。\n'
        source = ''.join(v.model_input(case, entities)['sourceBlocks'])
        self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', source), case['draft'])

    def test_missing_or_wrong_position_does_not_hide_behind_limitations(self):
        fixture = v.load_cases()
        case = next(c for c in fixture['cases'] if c['id']=='gate:both')
        entities = fixture['scenes']['gate']
        for field, value in [('value','outside'), ('polarity','negative')]:
            r = proposal(case, entities)
            r['items'][0]['position'][field] = value
            result = v.e.assess(r, case, entities)
            self.assertEqual(result['status'], 'mismatched')
            self.assertFalse(result['contentScore']['items'][0]['position'])
            self.assertIsNone(result['projection']['items'][0]['normal'])
        r = proposal(case, entities)
        r['items'][0]['position'] = None
        result = v.e.assess(r, case, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertIn('position_interpretation_missing', [i['code'] for i in result['issues']])

    def test_premise_cannot_include_conclusion_or_drop_condition_prefix(self):
        fixture = v.load_cases()
        case = next(c for c in fixture['cases'] if c['id']=='gate:condition_paraphrase')
        entities = fixture['scenes']['gate']
        for mode, code in [('overlap','condition_premise_overlaps_core'),('prefix','condition_premise_partial_unit')]:
            r = proposal(case, entities)
            item = r['items'][0]
            if mode == 'overlap':
                item['limitations'][0]['premise'] = list(item['qualified'])
            else:
                item['qualified'] = item['qualified'][1:]
                item['limitations'][0]['premise'] = ['P1-U1-S2']
                item['limitations'][0]['cue'] = dict(segmentId='P1-U1-S2',quote='守门弟子点头',occurrence=0)
            result = v.e.assess(r, case, entities)
            self.assertEqual(result['status'], 'mismatched')
            self.assertIn(code, [i['code'] for i in result['issues']])
            self.assertFalse(result['contentScore']['items'][0]['scope'])
            self.assertEqual(result['proposedResponse'],r)

    def test_display_brackets_are_not_silently_removed(self):
        fixture = v.load_cases()
        case = next(c for c in fixture['cases'] if c['id']=='gate:plain_emphasis')
        entities = fixture['scenes']['gate']
        r = proposal(case, entities)
        r['items'][0]['core'] = ['⟦P1-U1-S1⟧']
        result = v.e.assess(r, case, entities)
        self.assertEqual(result['status'],'invalid_response')
        self.assertEqual(result['proposedResponse'],r)

    def test_null_interpretation_remains_unverified_without_reading_gold(self):
        fixture = v.load_cases()
        case = fixture['cases'][0]
        entities = fixture['scenes']['gate']
        r = proposal(case, entities)
        r['items'][0]['position'] = None
        baseline = v.e.inspect(r,case,entities)
        changed = dict(case, targets=[],selectionExpectations=[],id='hidden')
        self.assertEqual(v.e.inspect(r,changed,entities),baseline)
        self.assertFalse(baseline['productionEnablement'])
        self.assertEqual(baseline['semanticStatus'],'unverified')

    def test_duplicate_and_missing_positions_are_not_complete_coverage(self):
        fixture = v.load_cases()
        case = next(c for c in fixture['cases'] if c['id']=='hall:two_people')
        entities = fixture['scenes']['hall']
        for indices in [(0,), (0,0)]:
            r = proposal(case, entities)
            r['items'] = [copy.deepcopy(r['items'][i]) for i in indices]
            result = v.e.assess(r,case,entities)
            self.assertNotEqual(result['status'],'matched')

    def test_label_drift_stops_before_gateway(self):
        with TemporaryDirectory() as directory, patch.object(v.f,'_gateway') as gateway:
            labels = json.loads(v.LABELS.read_text())
            labels['cases'][0]['items'][0]['position']['value'] = 'outside'
            path=Path(directory)/'labels.json'
            path.write_text(json.dumps(labels))
            with patch.object(v,'LABELS',path), self.assertRaisesRegex(ValueError,'标签哈希'):
                v.run(Path(directory)/'report.json')
            gateway.assert_not_called()

    def test_old_schema_is_not_automatically_upgraded(self):
        fixture = v.load_cases();case=fixture['cases'][0];entities=fixture['scenes']['gate']
        result = v.e.inspect(controls.proposal(case,entities),case,entities)
        self.assertEqual(result['status'],'invalid_response')
