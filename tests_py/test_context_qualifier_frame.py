import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_support import context_qualifier_frame as v
from test_support import context_qualifier_frame_replay as replay
from tests_py.test_context_qualifier_mention_contrast_eval import control as contrast_control
from tests_py.test_context_qualifier_unit_premises import proposal as regression_control


def control(case, entities):
    make = regression_control if case['cohort'] == 'regression' else contrast_control
    proposal = make(case, entities)
    for item, expected in zip(proposal[v.extraction.VERSION], case['selectionExpectations']):
        item['core'] = copy.deepcopy(min(expected['spans'], key=lambda span: len(span['core']))['core'])
    tasks = v.model_input(case, entities)['tasks']
    result = []
    for subject, task in tasks.items():
        items = []
        for original in proposal[v.extraction.VERSION]:
            if original['subject'] != subject:
                continue
            conditions = [dict(units=copy.deepcopy(x['premiseUnits']), cue=copy.deepcopy(x['cue']))
                          for x in original['limitations'] if x['kind'] == 'condition']
            selected = {u for c in conditions for u in c['units']}
            items.append(dict(boundary=original['boundary'], position=copy.deepcopy(original['position']),
                              core=copy.deepcopy(original['core']), conditions=conditions,
                              modifiers=[dict(kind=x['kind'], cue=copy.deepcopy(x['cue']))
                                         for x in original['limitations'] if x['kind'] != 'condition'],
                              nonPremiseUnits=[u for u in task['contextUnits'] if u not in selected],
                              unresolvedUnits=[]))
        result.append(dict(subject=subject, attribution=case['frameAttributions'][subject], items=items))
    return {v.VERSION: result}


class FrameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = replay.load_cases()

    def case(self, cid='gate:both_paraphrase', draft=None):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        if draft is not None:
            c['draft'] = draft
        return c, self.fixture['scenes'][c['sceneKey']]

    def test_all_frozen_controls_and_unchanged_prior_labels(self):
        for c, old in zip(self.fixture['cases'], replay.previous.load_cases()['cases']):
            self.assertEqual({k: x for k, x in c.items() if k != 'frameAttributions'}, old)
        counts, positions, tasks = {}, 0, 0
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            result = v.assess(control(c, entities), c, entities)
            expected = 'scope_pending' if c['cohort'] == 'scope_controls' else 'matched'
            self.assertEqual(result['status'], expected, (c['id'], result))
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertFalse(result['productionEnablement'])
            counts[expected] = counts.get(expected, 0)+1
            positions += len(result['decodedProposal'][v.extraction.VERSION])
            tasks += len(result['coverage']['attribution'])
        self.assertEqual(counts, dict(matched=40, scope_pending=4))
        self.assertEqual((positions, tasks), (38, 48))

    def test_lossless_projection_excludes_gold_and_redundant_subject_task_ids(self):
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            data = v.model_input(c, entities)
            hidden = dict(c, id='SECRET', frameAttributions={}, coverageExpectation={}, targets=[], selectionExpectations=[])
            self.assertEqual(data, v.model_input(hidden, entities))
            self.assertEqual(set(data), {'source', 'entityRefs', 'tasks'})
            self.assertEqual(''.join(x if isinstance(x, str) else ''.join(x['segments'].values()) for x in data['source']), c['draft'])
            for subject, task in data['tasks'].items():
                self.assertIn(subject, data['entityRefs'])
                self.assertEqual(set(task), {'boundaries', 'contextUnits'})
            self.assertNotIn('scene:', json.dumps(data))
        self.assertIn('json', replay.PROMPT.read_text().lower())
        self.assertLess(len(replay.PROMPT.read_text()), len(replay.previous.PROMPT.read_text()))

    def test_condition_cannot_include_conclusion_or_omit_classification(self):
        c, entities = self.case()
        for mode in ('own_unit', 'omitted', 'overlap', 'outside', 'duplicate'):
            response = control(c, entities)
            item = response[v.VERSION][0]['items'][0]
            if mode == 'own_unit': item['conditions'][0]['units'].append('P1-U2')
            elif mode == 'omitted': item['conditions'] = []
            elif mode == 'overlap': item['nonPremiseUnits'] = ['P1-U1']
            elif mode == 'outside': item['nonPremiseUnits'] = ['P1-U3']
            else: item['conditions'] *= 2
            self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response', mode)

    def test_wrong_non_premise_decision_still_fails_semantics(self):
        c, entities = self.case()
        response = control(c, entities)
        item = response[v.VERSION][0]['items'][0]
        item['conditions'] = []
        item['nonPremiseUnits'] = ['P1-U1']
        original = copy.deepcopy(response)
        self.assertEqual(v.inspect(response, c, entities)['status'], 'structurally_valid')
        result = v.assess(response, c, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertTrue(result['coverage']['dependencyPartitionComplete'])
        self.assertEqual(response, original)
        self.assertEqual(result['proposedResponse'], original)
        self.assertEqual(len(result['decodedProposal'][v.extraction.VERSION][0]['limitations']), 1)

    def test_unresolved_dependency_withholds_candidate_and_skips_strict_scoring(self):
        c, entities = self.case()
        response = control(c, entities)
        item = response[v.VERSION][0]['items'][0]
        item['conditions'] = []
        item['unresolvedUnits'] = ['P1-U1']
        with patch.object(v.extraction, 'assess') as scoring:
            result = v.assess(response, c, entities)
            scoring.assert_not_called()
        self.assertEqual(result['status'], 'dependency_pending')
        self.assertNotIn('decodedProposal', result)
        self.assertIn('withheldProposal', result)
        self.assertTrue(result['coverage']['unresolvedDependencies'])

    def test_wrong_attribution_cannot_mask_real_position_or_mention(self):
        c, entities = self.case('in_core:mention')
        response = control(c, entities)
        response[v.VERSION][0].update(attribution='word_mention', items=[])
        result = v.assess(response, c, entities)
        self.assertEqual(result['status'], 'mismatched')
        self.assertFalse(result['attributionMatches'])
        c, entities = self.case('control:quoted_words')
        response = control(c, entities)
        subject, task = next(iter(v.model_input(c, entities)['tasks'].items()))
        response[v.VERSION][0].update(attribution='position', items=[dict(boundary=task['boundaries'][0],
            position=dict(value='inside', polarity='positive'), core=['P1-U1-S1'], conditions=[],
            modifiers=[dict(kind='modality', cue=dict(segmentId='P1-U1-S1', quote='可能', occurrence=0))],
            nonPremiseUnits=[], unresolvedUnits=[])])
        self.assertEqual(v.assess(response, c, entities)['status'], 'mismatched')

    def test_non_position_subtype_is_scored_independently(self):
        c, entities = self.case('frame:portrait')
        response = control(c, entities)
        response[v.VERSION][0]['attribution'] = 'word_mention'
        result = v.assess(response, c, entities)
        self.assertEqual(result['extractionAssessment']['status'], 'matched')
        self.assertEqual(result['status'], 'mismatched')

    def test_reported_statements_and_pronouns_are_explicitly_pending(self):
        for cid in ('frame:self_report', 'frame:other_report', 'scope:pronoun', 'scope:cross_sentence'):
            c, entities = self.case(cid)
            result = v.assess(control(c, entities), c, entities)
            self.assertEqual(result['status'], 'scope_pending')
            self.assertTrue(result['coverage']['unresolvedSlots'] or result['coverage']['pendingUnits'])
            self.assertEqual(result['decodedProposal'][v.extraction.VERSION], [])

    def test_envelopes_respect_sentence_semicolon_and_paragraph_boundaries(self):
        for separator in ('。', '；', '\n\n'):
            c, entities = self.case(draft='守门弟子点头'+separator+'伤者在封山线内。')
            tasks = v.model_input(c, entities)['tasks']
            self.assertEqual([t['contextUnits'] for t in tasks.values()], [[]])
        c, entities = self.case(draft='守门弟子点头，伤者在封山线内，铁链响了。')
        tasks = v.model_input(c, entities)['tasks']
        self.assertEqual([t['contextUnits'] for t in tasks.values()], [['P1-U1', 'P1-U3']])

    def test_budgets_fail_before_projection_without_truncation(self):
        c, entities = self.case()
        for owner, name in ((v, 'MAX_CONTEXT_UNITS'), (v, 'MAX_TASK_CHARS'), (v.previous, 'MAX_SLOTS')):
            with patch.object(owner, name, 0):
                with self.assertRaisesRegex(ValueError, '预算超限'): v.model_input(c, entities)
        c, entities = self.case(draft=('铁链响了，'*8)+'伤者在封山线内。')
        self.assertEqual(len(next(iter(v.model_input(c, entities)['tasks'].values()))['contextUnits']), 8)
        c['draft'] = '铁链响了，'+c['draft']
        with self.assertRaisesRegex(ValueError, '预算超限'): v.model_input(c, entities)

    def test_missing_duplicate_foreign_and_redundant_output_rejected(self):
        c, entities = self.case()
        for mode in ('missing', 'duplicate', 'foreign', 'reason', 'subject', 'bad_type', 'empty', 'modifier_condition'):
            response = control(c, entities)
            d = response[v.VERSION][0]
            item = d['items'][0]
            if mode == 'missing': response[v.VERSION] = []
            elif mode == 'duplicate': response[v.VERSION] *= 2
            elif mode == 'foreign': d['subject'] = 'other-source'
            elif mode in ('reason', 'subject'): item[mode] = 'redundant'
            elif mode == 'bad_type': d['attribution'] = []
            elif mode == 'empty': d['items'] = []
            else: item['modifiers'][0]['kind'] = 'condition'
            self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response', mode)

    def test_cues_remain_exact_and_conditions_keep_all_premise_segments(self):
        c, entities = self.case()
        response = control(c, entities)
        result = v.assess(response, c, entities)
        view = replay.previous.review.build(result['decodedProposal'], c, entities)
        condition = next(x for x in view['items'][0]['sourceEvidence']['limitations'] if x['kind'] == 'condition')
        self.assertEqual(''.join(x['quote'] for x in condition['premise']), '只要守门弟子点头，')
        for change in (dict(quote='只要守门弟子点头'), dict(occurrence=1), dict(segmentId='P1-U2-S1')):
            bad = copy.deepcopy(response)
            bad[v.VERSION][0]['items'][0]['conditions'][0]['cue'].update(change)
            self.assertEqual(v.inspect(bad, c, entities)['status'], 'invalid_response')
        self.assertNotIn('structured_source_evidence', json.dumps(view))
        self.assertEqual(result['coverage']['legacyReasonOrigin'], 'adapter_sentinel_not_model_explanation')

    def test_previous_real_failures_stay_failures_and_old_wire_is_not_upgraded(self):
        report = json.loads(replay.previous.OUTPUT.read_text())
        failures = [r for r in report['cases'] if r['status'] not in ('matched', 'scope_pending')]
        self.assertEqual(len(failures), 3)
        for row in failures:
            c, entities = self.case(row['caseId'])
            self.assertEqual(v.inspect(row['proposedResponse'], c, entities)['status'], 'invalid_response')
            original = replay.previous.assess_content(row['calls'][0]['content'], c, entities)
            self.assertEqual(original['status'], row['status'])

    def test_offline_replay_preserves_live_results_and_detects_tamper_without_calls(self):
        with patch.object(replay.f, '_gateway') as gateway:
            result = replay.build()
            self.assertEqual(result['newModelCalls'], 0)
            self.assertEqual(result['priorSummary']['matched'], 33)
            self.assertEqual(len(result['priorFailures']), 3)
            self.assertEqual(result['summary']['tasks'], 48)
            self.assertLess(result['summary']['replacementInputChars'], result['summary']['priorInputChars'])
            with TemporaryDirectory() as d:
                path = Path(d)/'result.json'
                replay.f.save_checkpoint(path, result, create=True)
                self.assertTrue(replay.audit(path)['allRowsReproduced'])
                result['semanticCoverage'] = 'verified'
                path.write_text(json.dumps(result))
                with self.assertRaisesRegex(ValueError, '不可复现'): replay.audit(path)
            gateway.assert_not_called()

    def test_fixture_or_prompt_drift_rejected(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'bad'
            path.write_text('{}')
            for field in ('PROMPT', 'FIXTURE'):
                with patch.object(replay, field, path):
                    with self.assertRaisesRegex(ValueError, '哈希变化'): replay.load_cases()
