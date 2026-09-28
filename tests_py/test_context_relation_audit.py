"""Public state comparison must never authorize an inferred text relation."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_support import context_relation_audit as r


class RelationAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.snapshot, cls.evidence = r.load_inputs()

    def claim(self, **changes):
        fact = self.snapshot.locations[0]
        return dict(dict(snapshotId=self.snapshot.snapshot_id, branchId=self.snapshot.branch_id,
                         subject=fact.subject, relation='exact_location_id', object=fact.value,
                         polarity='positive', time='snapshot', condition=None), **changes)

    def test_current_public_field_and_negation_compare_with_provenance(self):
        yes = r.compare_location(self.claim(), self.snapshot)
        no = r.compare_location(self.claim(polarity='negative'), self.snapshot)
        self.assertEqual(yes['verdict'], 'supported')
        self.assertEqual(no['verdict'], 'contradicted')
        self.assertEqual(len(yes['sources']), 2)
        self.assertTrue(all(s['snapshotId'] == self.snapshot.snapshot_id for s in yes['sources']))
        self.assertFalse(yes['proseAcceptance'])

    def test_exact_identifier_relation_does_not_infer_geographic_containment(self):
        # exact_location_id asserts identifier equality, not "inside this place".
        for value in ('location_9c23a624e6b0', '玄霄宗山门', 'unresolved-location'):
            for polarity in ('positive', 'negative'):
                self.assertEqual(r.compare_location(self.claim(object=value, polarity=polarity), self.snapshot)['verdict'], 'needs_review')
        self.assertEqual(r.compare_location(self.claim(relation='inside', object='玄霄宗'), self.snapshot)['verdict'], 'needs_review')

    def test_missing_subject_is_unknown_not_false(self):
        for subject in ('scene:wounded_stranger', 'scene:gatekeeper', '陆照临'):
            for polarity in ('positive', 'negative'):
                result = r.compare_location(self.claim(subject=subject, polarity=polarity), self.snapshot)
                self.assertEqual(result['verdict'], 'needs_review')
                self.assertEqual(result['sources'], [])

    def test_time_condition_branch_and_snapshot_cannot_be_silently_dropped(self):
        for changes in (dict(time='three_years_ago'), dict(time='after_lock'), dict(condition='before_lock'),
                        dict(branchId='other'), dict(snapshotId='stale')):
            with self.subTest(changes=changes):
                self.assertEqual(r.compare_location(self.claim(**changes), self.snapshot)['verdict'], 'needs_review')

    def test_model_supplied_support_cannot_be_promoted_to_authority(self):
        data = self.claim()
        data['evidence'] = {'subject': data['subject'], 'value': data['object'], 'authority': 'authoritative'}
        self.assertEqual(r.compare_location(data, self.snapshot)['verdict'], 'needs_review')
        empty = replace(self.snapshot, locations=())
        self.assertEqual(r.compare_location(self.claim(), empty)['verdict'], 'needs_review')

    def test_invalid_claims_fail_closed(self):
        for data in (None, {}, [], self.claim(object=True), self.claim(object=1), self.claim(polarity='yes')):
            self.assertEqual(r.compare_location(data, self.snapshot)['verdict'], 'needs_review')

    def test_visibility_does_not_expose_reviewer_only_state(self):
        fact = self.snapshot.locations[0]
        state = dict(playerCharacterId=fact.subject, playerLocationId=fact.value,
                     characterLocationIds={fact.subject:fact.value, 'secret-character':'hidden-location'},
                     itemOwnerCharacterIds={'item_open_letter':fact.subject})
        visibility = {'/playerCharacterId':'player_known', '/playerLocationId':'player_known'}
        result = r.public_locations(state, visibility)
        self.assertEqual(result[0].paths, ('/playerLocationId',))
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('item_open_letter', repr(result))
        for value in ('author_truth', 'character_known', 'unknown'):
            self.assertEqual(r.public_locations(state, dict(visibility, **{'/playerLocationId':value})), ())
        self.assertEqual(r.public_locations(state, {'/playerLocationId':'player_known'}), ())

    def test_disagreeing_public_paths_block_instead_of_picking_one(self):
        fact = self.snapshot.locations[0]
        state = dict(playerCharacterId=fact.subject, playerLocationId=fact.value,
                     characterLocationIds={fact.subject:'location_9c23a624e6b0'})
        visibility = dict.fromkeys(['/playerCharacterId', *fact.paths], 'player_known')
        with self.assertRaisesRegex(ValueError, '冲突'):
            r.public_locations(state, visibility)

    def test_gold_answers_and_selected_reference_are_not_model_inputs(self):
        case = self.fixture['cases'][0]
        payload = r.text_request(case, self.evidence)
        other = dict(case, expectedVerdict='contradicted', rationale='change', reference={})
        self.assertEqual(payload, r.text_request(other, self.evidence))
        self.assertEqual(set(payload), {'statement', 'publicEvidence'})

    def test_contrasts_preserve_open_world_time_and_actor_uncertainty(self):
        cases = {c['id']:c for c in self.fixture['cases']}
        self.assertEqual(len(cases), 16)
        for name in ('time_two', 'actor_guard', 'completed', 'order_before'):
            self.assertEqual(cases[name]['expectedVerdict'], 'unsupported')
        self.assertEqual(cases['position_in']['expectedVerdict'], 'contradicted')

    def test_binding_and_reference_corruption_stop_audit(self):
        for kind in ('hash', 'missing_binding', 'quote', 'duplicate'):
            fixture = copy.deepcopy(self.fixture)
            if kind == 'hash': fixture['bindings'][next(iter(fixture['bindings']))] = '0'*64
            elif kind == 'missing_binding': fixture['bindings'].pop(next(iter(fixture['bindings'])))
            elif kind == 'quote': fixture['cases'][0]['reference']['quote'] = '不存在的公开关系'
            else: fixture['cases'].append(fixture['cases'][0])
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                path = Path(directory)/'fixture.json'
                path.write_text(json.dumps(fixture))
                with self.assertRaises(ValueError): r.load_inputs(path)

    def test_audit_reports_coverage_separately_from_semantic_success(self):
        report = r.audit()
        self.assertEqual(report['summary'], dict(textCases=16, textNeedsReview=16, structuredControls=8, structuredMatches=8))
        self.assertEqual(report['priorFailure']['rawVerdict'], 'supported')
        self.assertEqual(report['priorFailure']['localVerdict'], 'needs_review')
        self.assertFalse(report['acceptance'])
        self.assertFalse(report['productionEnablement'])
        self.assertEqual(report['modelCalls'], 0)
        self.assertTrue(all(not c['result']['proseAcceptance'] for c in report['structuredControls']))
