import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_support import context_assertion_scope as v
from test_support import context_assertion_scope_replay as replay


class AssertionScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.controls = replay.load_inputs()

    def sample(self, cid='gate:both_paraphrase'):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        row = next(c for c in self.controls if c['caseId'] == cid)
        return c, self.fixture['scenes'][c['sceneKey']], copy.deepcopy(row['control'])

    def test_frozen_controls_are_expressible_without_semantic_acceptance(self):
        counts, totals = {}, dict(narratorCandidates=0, reportedCandidates=0,
                                 absenceDecisions=0, unresolvedDecisions=0)
        for c, row in zip(self.fixture['cases'], self.controls):
            entities = self.fixture['scenes'][c['sceneKey']]
            original = copy.deepcopy(row['control'])
            result = v.inspect(row['control'], c, entities)
            self.assertNotEqual(result['status'], 'invalid_response', (c['id'], result))
            counts[result['status']] = counts.get(result['status'], 0)+1
            for key in totals:
                totals[key] += len(result[key])
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertFalse(result['productionEnablement'])
            self.assertFalse(result['acceptance'])
            self.assertFalse(result['legacyAttributionComparable'])
            self.assertNotIn('decodedProposal', result)
            self.assertEqual(row['control'], original)
            self.assertEqual(row['legacyAttributions'], c['frameAttributions'])
        self.assertEqual(counts, dict(structurally_valid=26, scope_pending=18))
        self.assertEqual(tuple(totals.values()), (38, 2, 8, 0))

    def test_model_input_stays_lossless_and_excludes_control_labels(self):
        prior = json.loads(replay.previous.OUTPUT.read_text())
        for c, old in zip(self.fixture['cases'], prior['cases']):
            entities = self.fixture['scenes'][c['sceneKey']]
            data = v.model_input(c, entities)
            self.assertEqual(data, json.loads(old['calls'][0]['messages'][1]['content']))
            hidden = dict(c, id='SECRET', targets=[], frameAttributions={},
                          coverageExpectation={}, selectionExpectations=[], control='SECRET')
            self.assertEqual(data, v.model_input(hidden, entities))
            self.assertEqual(set(data), {'source', 'entityRefs', 'tasks'})
            self.assertEqual(''.join(x if isinstance(x, str) else ''.join(x['segments'].values())
                                    for x in data['source']), c['draft'])

    def test_reports_keep_subject_speaker_and_full_wrapper_without_promotion(self):
        for cid in ('frame:self_report', 'frame:other_report'):
            c, entities, response = self.sample(cid)
            result = v.inspect(response, c, entities)
            self.assertEqual(result['status'], 'scope_pending')
            self.assertEqual(result['narratorCandidates'], [])
            report, = result['reportedCandidates']
            self.assertEqual(report['subject'] == report['origin']['speaker'], cid == 'frame:self_report')
            table = v.frame.extraction.segments(c, entities)
            self.assertEqual(''.join(table[s]['quote'] for s in report['origin']['scope']), c['draft'])
            self.assertNotIn('decodedProposal', result)

    def test_scope_and_speaker_errors_are_rejected_without_filling_gaps(self):
        for mode in ('missing', 'empty', 'foreign', 'duplicate', 'reverse', 'omit_speaker',
                     'speaker_unknown', 'speaker_boundary', 'speaker_ambiguous'):
            c, entities, response = self.sample('frame:other_report')
            item = response[v.VERSION][1]['items'][0]
            origin = item['origin']
            if mode == 'missing': del origin['scope']
            elif mode == 'empty': origin['scope'] = []
            elif mode == 'foreign': origin['scope'] = ['P2-U1-S1']
            elif mode == 'duplicate': origin['scope'] *= 2
            elif mode == 'reverse': origin['scope'].reverse()
            elif mode == 'omit_speaker': origin['scope'] = item['core'][:]
            elif mode == 'speaker_unknown': origin['speaker'] = 'unknown'
            elif mode == 'speaker_boundary': origin['speaker'] = item['boundary']
            data = v.model_input(c, entities)
            if mode == 'speaker_ambiguous': data['entityRefs'][origin['speaker']]['ambiguous'] = True
            with patch.object(v, 'model_input', return_value=data):
                self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response', mode)
        c, entities, response = self.sample('gate:both_paraphrase')
        item = response[v.VERSION][0]['items'][0]
        self.assertGreaterEqual(len(item['origin']['scope']), 3)
        item['origin']['scope'].pop(1)
        self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response')

    def test_conditions_stay_complete_and_unresolved_dependencies_stay_pending(self):
        c, entities, response = self.sample()
        for mode in ('omit_premise_scope', 'omit_condition', 'false_cue', 'overlap'):
            bad = copy.deepcopy(response)
            item = bad[v.VERSION][0]['items'][0]
            if mode == 'omit_premise_scope': item['origin']['scope'] = item['core'][:]
            elif mode == 'omit_condition': item['conditions'] = []
            elif mode == 'false_cue': item['conditions'][0]['cue']['quote'] = '伪造条件'
            else: item['nonPremiseUnits'] = item['conditions'][0]['units'][:]
            self.assertEqual(v.inspect(bad, c, entities)['status'], 'invalid_response', mode)
        item = response[v.VERSION][0]['items'][0]
        item['unresolvedUnits'] = item['conditions'][0]['units'][:]
        item['conditions'] = []
        with patch.object(v.frame, 'assess') as scoring:
            result = v.inspect(response, c, entities)
            scoring.assert_not_called()
        self.assertEqual(result['status'], 'scope_pending')
        self.assertTrue(result['pendingDependencies'])
        self.assertNotIn('decodedProposal', result)

    def test_valid_references_cannot_prove_origin_or_absence_semantics(self):
        c, entities, response = self.sample('frame:other_report')
        # Deliberately wrong origin: references alone must not imply semantic acceptance.
        response[v.VERSION][1]['items'][0]['origin']['speaker'] = None
        result = v.inspect(response, c, entities)
        self.assertEqual(result['status'], 'structurally_valid')
        self.assertEqual(len(result['narratorCandidates']), 1)
        self.assertEqual(result['semanticStatus'], 'unverified')
        self.assertFalse(result['acceptance'])
        c, entities, response = self.sample('hall:source_position')
        d = response[v.VERSION][0]
        d.update(resolution='absent', basis=d.pop('items')[0]['core'])
        result = v.inspect(response, c, entities)
        self.assertNotEqual(result['status'], 'invalid_response')
        self.assertEqual(result['semanticStatus'], 'unverified')
        self.assertFalse(result['productionEnablement'])

    def test_one_subject_can_keep_narrator_and_reported_candidates(self):
        c, entities, _ = self.sample('frame:self_report')
        # Design-only control using the same registered official-longform entities.
        c['draft'] = '沈砚秋站在议事殿内并说“我在殿外”。'
        data = v.model_input(c, entities)
        subject, task = next(iter(data['tasks'].items()))
        segments = list(v.frame.extraction.segments(c, entities))
        items = [dict(boundary=boundary, position=dict(value=position, polarity='positive'),
                      core=segments[:], conditions=[], modifiers=[], nonPremiseUnits=[],
                      unresolvedUnits=[], origin=dict(speaker=speaker, scope=segments[:]))
                 for boundary, position, speaker in zip(task['boundaries'],
                     ('inside', 'outside'), (None, subject))]
        response = {v.VERSION: [dict(subject=subject, resolution='positions', items=items)]}
        result = v.inspect(response, c, entities)
        self.assertEqual(result['status'], 'scope_pending')
        self.assertEqual(len(result['narratorCandidates']), 1)
        self.assertEqual(len(result['reportedCandidates']), 1)
        # This segmentation preserves text but does not isolate the two speech levels.
        self.assertEqual(len(segments), 1)
        self.assertEqual(result['semanticStatus'], 'unverified')
        self.assertNotIn('decodedProposal', result)

    def test_old_wire_and_real_segment_as_unit_failure_are_not_repaired(self):
        prior = json.loads(replay.previous.OUTPUT.read_text())
        old = next(x for x in prior['cases'] if x['caseId'] == 'two_people:first_operator')
        c, entities, response = self.sample(old['caseId'])
        raw = json.loads(old['calls'][0]['content'])
        self.assertEqual(v.inspect(raw, c, entities)['status'], 'invalid_response')
        for new, original in zip(response[v.VERSION], raw[v.frame.VERSION]):
            for item, bad in zip(new['items'], original['items']):
                item['nonPremiseUnits'] = bad['nonPremiseUnits'][:]
        unchanged = copy.deepcopy(response)
        self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response')
        self.assertEqual(response, unchanged)

    def test_missing_duplicate_unresolved_and_malformed_decisions(self):
        for mode in ('missing', 'duplicate', 'extra', 'malformed', 'budget', 'basis_foreign', 'basis_empty'):
            c, entities, response = self.sample()
            decision = response[v.VERSION][0]
            if mode == 'missing': response[v.VERSION] = []
            elif mode == 'duplicate': response[v.VERSION] *= 2
            elif mode == 'extra': decision['reason'] = 'redundant'
            elif mode == 'malformed': decision['items'][0]['conditions'] = None
            elif mode == 'budget': decision['items'] *= 65
            else:
                decision.clear()
                decision.update(subject=next(iter(v.model_input(c, entities)['tasks'])),
                    resolution='absent', basis=[] if mode == 'basis_empty' else ['P2-U1-S1'])
            self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response', mode)
        c, entities, response = self.sample('control:location_mention')
        response[v.VERSION][0]['resolution'] = 'unresolved'
        result = v.inspect(response, c, entities)
        self.assertEqual(result['status'], 'scope_pending')
        self.assertEqual(len(result['unresolvedDecisions']), 1)

    def test_replay_preserves_old_failures_and_detects_tamper_without_calls(self):
        # Full upstream audit is run by the real replay; avoid repeating it for mutations.
        saved_audit = json.loads(replay.previous.AUDIT.read_text())
        with patch.object(replay.previous, 'audit', return_value=saved_audit), \
                patch.object(replay, 'load_inputs', return_value=(self.fixture, self.controls)), \
                patch.object(replay.f, '_gateway') as gateway:
            report = replay.build()
            self.assertEqual(report['newModelCalls'], 0)
            self.assertEqual(report['originalSummary']['matched'], 37)
            self.assertEqual(len(report['originalFailures']), 5)
            self.assertFalse(report['readyForModelEvaluation'])
            self.assertGreater(report['summary']['controlResponseChars'], report['summary']['legacyControlResponseChars'])
            with TemporaryDirectory() as d:
                path = Path(d)/'result.json'
                replay.f.save_checkpoint(path, report, create=True)
                self.assertTrue(replay.audit(path)['allRowsReproduced'])
                report['semanticStatus'] = 'verified'
                path.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, '不可复现'): replay.audit(path)
            gateway.assert_not_called()

    def test_control_fixture_hash_drift_is_rejected(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'bad.json'
            path.write_text('{}')
            with patch.object(replay, 'FIXTURE', path), patch.object(replay.f, '_gateway') as gateway:
                with self.assertRaisesRegex(ValueError, '哈希变化'): replay.load_inputs()
                gateway.assert_not_called()
