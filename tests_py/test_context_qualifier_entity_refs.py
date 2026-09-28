import copy
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_entity_refs_eval as v
from tests_py.test_context_qualifier_selection_strict_eval import proposal as old_proposal


def proposal(case, entities):
    r = old_proposal(case, entities)
    refs = v.e.entity_refs(case, entities)
    for item in r['items']:
        for role in ('subject', 'boundary'):
            anchor = item[role]
            item[role] = next(rid for rid, ref in refs.items()
                              if all(ref[k] == anchor[k] for k in anchor))
    return {v.e.VERSION: r['items']}


class EntityRefsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid='hall:source_position'):
        case = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        return case, copy.deepcopy(self.fixture['scenes'][case['sceneKey']])

    def test_all_controls_keep_strict_scores_and_unverified_state(self):
        for case in self.fixture['cases']:
            entities = self.fixture['scenes'][case['sceneKey']]
            r = proposal(case, entities)
            result = v.e.assess(r, case, entities)
            self.assertEqual(result['status'], 'matched', (case['id'], result))
            self.assertEqual(result['proposedResponse'], r)
            self.assertFalse(result['productionEnablement'])
            self.assertTrue(result['reviewRequired'])
            self.assertEqual(result['semanticStatus'], 'unverified')

    def test_occurrences_use_registered_aliases_not_direction_phrases(self):
        c, e = self.case()
        refs = v.e.entity_refs(c, e)
        boundary = [r for r in refs.values() if r['kinds'] == ['scene_boundary']]
        self.assertEqual([r['quote'] for r in boundary], ['殿'])
        c['draft'] = '沈砚秋在议事殿外。'
        refs = v.e.entity_refs(c, e)
        self.assertEqual([r['quote'] for r in refs.values() if r['kinds'] == ['scene_boundary']], ['议事殿'])

    def test_repeated_aliases_have_distinct_exact_occurrences(self):
        c, e = self.case()
        c['draft'] = '沈砚秋看着议事殿又指向议事殿。'
        refs = v.e.entity_refs(c, e)
        occurrences = [r for r in refs.values() if r['quote'] == '议事殿']
        self.assertEqual([r['occurrence'] for r in occurrences], [0, 1])
        self.assertEqual(len(refs), 3)

    def test_wrong_role_unknown_ref_and_free_quote_are_rejected(self):
        c, e = self.case()
        r = proposal(c, e)
        for bad in (r[v.e.VERSION][0]['subject'], 'unknown', {'quote': '殿外'}):
            changed = copy.deepcopy(r)
            changed[v.e.VERSION][0]['boundary'] = bad
            result = v.e.assess(changed, c, e)
            self.assertEqual(result['status'], 'invalid_response')
            self.assertEqual(result['proposedResponse'], changed)

    def test_entity_identity_and_selected_occurrence_must_agree(self):
        c, e = self.case('hall:two_people')
        r = proposal(c, e)
        r[v.e.VERSION][1]['subject'] = r[v.e.VERSION][0]['subject']
        self.assertNotEqual(v.e.assess(r, c, e)['status'], 'matched')
        c, e = self.case('gate:both')
        r = proposal(c, e)
        r[v.e.VERSION][0]['position']['value'] = 'outside'
        self.assertEqual(v.e.assess(r, c, e)['status'], 'mismatched')

    def test_shared_and_nested_alias_ambiguity_cannot_be_resolved_by_model(self):
        c, e = self.case()
        e['scene:other_hall'] = dict(kind='scene_boundary', mentions=['殿'])
        r = proposal(c, e)
        self.assertIn('歧义', v.e.inspect(r, c, e)['error'])
        c['draft'] = '沈砚秋站在议事殿外。'
        refs = v.e.entity_refs(c, e)
        nested = next(r for r in refs.values() if r['quote'] == '殿')
        self.assertEqual(nested['entityIds'], ['scene:council_hall', 'scene:other_hall'])

    def test_reference_binding_is_stable_and_rejects_other_source(self):
        c, e = self.case()
        refs = v.e.entity_refs(c, e)
        self.assertEqual(v.e.entity_refs(c, dict(reversed(list(e.items())))), refs)
        r = proposal(c, e)
        c['draft'] += '纸页响了。'
        self.assertEqual(v.e.inspect(r, c, e)['status'], 'invalid_response')

    def test_missing_wrong_or_extra_envelope_is_rejected_without_upgrade(self):
        c, e = self.case()
        r = proposal(c, e)
        for bad in ({'items': r[v.e.VERSION]}, {'wrong-version': r[v.e.VERSION]},
                    dict(r, schemaVersion=v.e.VERSION), {v.e.VERSION: None}, old_proposal(c,e)):
            self.assertEqual(v.e.inspect(bad, c, e)['status'], 'invalid_response')
        empty_case = dict(c, draft='纸页响了。', targets=[], selectionExpectations=[])
        self.assertEqual(v.e.assess({v.e.VERSION: []}, empty_case, e)['status'], 'matched')

    def test_missing_field_does_not_hide_cross_sentence_diagnostic(self):
        c, e = self.case('gate:unrelated_condition')
        r = proposal(c, e)
        del r[v.e.VERSION][0]['subject']
        r[v.e.VERSION][0]['qualified'] = list(v.e.segments(c, e))
        result = v.e.inspect(r, c, e)
        self.assertEqual(result['status'], 'invalid_response')
        self.assertEqual(result['fieldDiagnostics'][0]['missing'], ['subject'])
        self.assertTrue(result['scopeDiagnostics'])

    def test_inputs_remain_blind_and_source_roundtrips(self):
        for c in self.fixture['cases']:
            e = self.fixture['scenes'][c['sceneKey']]
            data = v.model_input(c,e)
            self.assertEqual(set(data), {'sourceBlocks', 'entityRefs'})
            self.assertEqual(re.sub(r'⟦[^⟧]+⟧', '', ''.join(data['sourceBlocks'])), c['draft'])
            changed = dict(c, targets=[],selectionExpectations=[],id='secret',labelRationale='secret')
            self.assertEqual(v.messages(changed,e),v.messages(c,e))
            self.assertEqual(v.e.entity_refs(changed,e),v.e.entity_refs(c,e))

    def test_reference_overflow_fails_without_truncation(self):
        c, e = self.case()
        c['draft'] = '殿' * 257
        with self.assertRaisesRegex(ValueError, '预算'):
            v.model_input(c,e)

    def test_prompt_example_and_binding_use_required_versioned_container(self):
        v.check_prompt_binding()
        text = v.PROMPT.read_text()
        example, _ = json.JSONDecoder().raw_decode(text[text.index('{"'+v.e.VERSION+'"'):])
        self.assertEqual(set(example), {v.e.VERSION})
        item = example[v.e.VERSION][0]
        self.assertFalse(set(item['core']) & set(item['limitations'][0]['premise']))
        with TemporaryDirectory() as directory, patch.object(v,'PROMPT',v.previous.PROMPT), patch.object(v.f,'_gateway') as gateway:
            path = Path(directory)/'report.json'
            with self.assertRaisesRegex(ValueError,'提示绑定'): v.run(path)
            gateway.assert_not_called()
            self.assertFalse(path.exists())

    def recorder(self, fail=False):
        fixture = self.fixture
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = fixture['cases'][len(recorder.calls)]
            content = json.dumps(proposal(c,fixture['scenes'][c['sceneKey']]))
            recorder.calls.append(dict(messages=messages,content=content,
                                      rawResponse=json.dumps({'choices':[{'message':{'content':content}}]})))
            if fail: raise v.f.LlmError('HTTP 400',code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_once_only_eighteen_calls_audit_and_no_overwrite(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
            path=Path(directory)/'report.json'
            report=v.run(path)
            self.assertEqual(report['summary']['matched'],18)
            self.assertEqual(v.audit(path)['summary'],report['summary'])
            with self.assertRaises(FileExistsError):v.run(path)
            self.assertEqual(len(recorder.calls),18)
            report['cases'][0]['calls'][0]['messages'][1]['content']='{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError,'实际输入'):v.audit(path)

    def test_transport_and_checkpoint_failures_stop_further_calls(self):
        for mode in ('transport','checkpoint'):
            recorder=self.recorder(fail=mode=='transport')
            with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                    base_url='unused',api_key='test',model='test',route='test')), patch.object(v.f,'_gateway',return_value=recorder):
                path=Path(directory)/'report.json'
                if mode=='transport':
                    self.assertEqual(v.run(path)['summary']['not_run'],17)
                else:
                    with patch.object(v.f,'save_checkpoint',side_effect=[None,OSError('disk full')]):
                        with self.assertRaises(v.f.CheckpointError):v.run(path)
                self.assertEqual(len(recorder.calls),1)
