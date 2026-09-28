from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_support import context_assertion_scoring as v
from test_support import context_assertion_scoring_replay as replay


class AssertionScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.controls = replay.previous.load_inputs()

    def sample(self, cid):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        ref = copy.deepcopy(next(r for r in self.controls if r['caseId'] == cid))
        return c, self.fixture['scenes'][c['sceneKey']], ref

    def test_references_and_empty_tasks_have_separate_denominators(self):
        counts, legacy = Counter(), Counter()
        for c, ref in zip(self.fixture['cases'], self.controls):
            result = v.assess(ref['control'], c, self.fixture['scenes'][c['sceneKey']], ref)
            counts[result['status']] += 1
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertFalse(result['productionEnablement'])
            self.assertFalse(result['acceptance'])
            self.assertNotIn('decodedProposal', result)
            for d in result['legacyDiagnostics']:
                legacy[d['expectedLegacySubtype']] += 1
                self.assertIsNone(d['predictedLegacySubtype'])
                self.assertFalse(d['subtypeScored'])
            if result['status'] == 'out_of_scope':
                self.assertIsNone(result['dimensions'])
                self.assertIsNone(result['referenceAgreement'])
                self.assertTrue(result['pendingUnits'])
        self.assertEqual(counts, dict(reference_match=42, out_of_scope=2))
        self.assertEqual(sum(legacy.values()), 48)
        self.assertEqual(set(legacy), {'position', 'unresolved', 'no_position', 'word_mention', 'other_entity'})

    def test_deliberate_faults_preserve_input_and_expose_independent_signals(self):
        probes = replay.probes(self.fixture, self.controls)
        before = copy.deepcopy(probes)
        for p in probes:
            reference = next(r for r in self.controls if r['caseId'] == p['caseId'])
            self.assertNotEqual(p['response'], reference['control'], p['probeId'])
        with patch.object(v.scope.frame, 'assess') as old_scoring:
            results = {p['probeId']: replay.evaluate_probe(p, self.fixture, self.controls)['assessment']
                       for p in probes}
            old_scoring.assert_not_called()
        self.assertEqual(probes, before)
        wrong_origin = results['promote:frame:other_report']
        self.assertEqual(wrong_origin['dimensions']['positionBindings']['matched'], 1)
        self.assertEqual(wrong_origin['dimensions']['originBindings']['missing'], 1)
        lost_condition = results['drop_condition']
        self.assertEqual(lost_condition['dimensions']['positionBindings']['matched'], 1)
        self.assertEqual(lost_condition['dimensions']['originBindings']['matched'], 1)
        self.assertEqual(lost_condition['dimensions']['qualifierBindings']['missing'], 1)
        self.assertEqual(results['wrong_subject']['dimensions']['subjects']['matched'], 0)

    def test_invalid_structure_is_unscored_and_not_zero_errors(self):
        c, entities, ref = self.sample('hall:source_position')
        for response in ({v.scope.VERSION: []}, {v.scope.frame.VERSION: []}, None):
            result = v.assess(response, c, entities, ref)
            self.assertEqual(result['status'], 'invalid_response')
            self.assertIsNone(result['dimensions'])
            self.assertIsNone(result['referenceAgreement'])
            self.assertNotIn('diagnostics', result)
            self.assertIsNone(result['legacyDiagnostics'][0]['actualResolution'])

    def test_reordered_decisions_do_not_change_agreement(self):
        c, entities, ref = self.sample('two_people:first_operator')
        response = copy.deepcopy(ref['control'])
        response[v.scope.VERSION].reverse()
        self.assertEqual(v.assess(response, c, entities, ref)['status'], 'reference_match')

    def test_swapped_origins_fail_even_when_source_counts_are_unchanged(self):
        c, entities, ref = self.sample('frame:self_report')
        c['draft'] = '沈砚秋站在议事殿内并说“我在殿外”。'
        data = v.scope.model_input(c, entities)
        subject, task = next(iter(data['tasks'].items()))
        segments = list(v.scope.frame.extraction.segments(c, entities))
        items = [dict(boundary=boundary, position=dict(value=value, polarity='positive'),
                      core=segments[:], conditions=[], modifiers=[], nonPremiseUnits=[], unresolvedUnits=[],
                      origin=dict(speaker=speaker, scope=segments[:]))
                 for boundary, value, speaker in zip(task['boundaries'], ('inside', 'outside'), (None, subject))]
        c['frameAttributions'] = {subject: 'position'}
        ref.update(draftSha256=hashlib.sha256(c['draft'].encode()).hexdigest(),
            legacyAttributions=c['frameAttributions'],
            control={v.scope.VERSION: [dict(subject=subject, resolution='positions', items=items)]})
        response = copy.deepcopy(ref['control'])
        a, b = response[v.scope.VERSION][0]['items']
        a['origin']['speaker'], b['origin']['speaker'] = b['origin']['speaker'], a['origin']['speaker']
        result = v.assess(response, c, entities, ref)
        self.assertEqual(result['status'], 'reference_mismatch')
        self.assertEqual(result['dimensions']['positionBindings']['matched'], 2)
        self.assertEqual(result['dimensions']['originBindings']['matched'], 0)
        self.assertEqual(result['diagnostics']['reportedAsNarrator'], 1)

    def test_range_difference_is_reference_disagreement_not_truth_judgment(self):
        c, entities, ref = self.sample('frame:other_report')
        response = copy.deepcopy(ref['control'])
        # Absence basis may legally be narrower; there is only one frozen reference.
        response[v.scope.VERSION][0]['basis'] = ['P1-U1-S1']
        result = v.assess(response, c, entities, ref)
        self.assertNotEqual(result['structuralStatus'], 'invalid_response')
        self.assertEqual(result['dimensions']['fullBindings']['missing'], 0)
        self.assertEqual(result['dimensions']['decisions']['missing'], 1)
        self.assertEqual(result['status'], 'reference_mismatch')
        self.assertEqual(result['semanticStatus'], 'unverified')

    def test_reference_cannot_silently_bind_different_text_or_labels(self):
        c, entities, ref = self.sample('frame:self_report')
        for key, value in (('caseId', 'other'), ('draftSha256', 'bad'), ('legacyAttributions', {})):
            bad = dict(ref, **{key: value})
            with self.assertRaisesRegex(ValueError, '未绑定'):
                v.assess(ref['control'], c, entities, bad)
        bad = dict(ref, control={v.scope.VERSION: []})
        with self.assertRaisesRegex(ValueError, '参考结构无效'):
            v.assess(ref['control'], c, entities, bad)

    def test_replay_audit_preserves_history_detects_tamper_and_never_calls_gateway(self):
        saved = json.loads(replay.previous.AUDIT.read_text())
        with patch.object(replay.previous, 'audit', return_value=saved), \
                patch.object(replay.previous, 'load_inputs', return_value=(self.fixture, self.controls)), \
                patch.object(replay.f, '_gateway') as gateway:
            report = replay.build()
            self.assertEqual(len(report['originalFailures']), 5)
            self.assertEqual(report['originalSummary']['matched'], 37)
            self.assertEqual(report['summary']['legacySubtypeScored'], 0)
            self.assertEqual(report['newModelCalls'], 0)
            self.assertFalse(report['readyForModelEvaluation'])
            with TemporaryDirectory() as d:
                path = Path(d)/'report.json'
                replay.f.save_checkpoint(path, report, create=True)
                self.assertTrue(replay.audit(path)['allRowsReproduced'])
                for mode in ('score', 'fault', 'hash', 'boundary'):
                    bad = copy.deepcopy(report)
                    if mode == 'score': bad['cases'][0]['assessment']['status'] = 'reference_mismatch'
                    elif mode == 'fault': bad['probes'][0]['assessment']['diagnostics']['reportedAsNarrator'] = 0
                    elif mode == 'hash': bad['inputHashes'].pop('test_support/context_assertion_scoring.py')
                    else: bad['productionEnablement'] = True
                    path.write_text(json.dumps(bad))
                    with self.assertRaisesRegex(ValueError, '不可复现'): replay.audit(path)
                with self.assertRaises(FileExistsError): replay.f.save_checkpoint(path, report, create=True)
            gateway.assert_not_called()
