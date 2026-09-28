import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_support import context_qualifier_coverage as v
from test_support import context_qualifier_core_contract_eval as previous
from tests_py.test_context_qualifier_mention_contrast_eval import control as contrast_control
from tests_py.test_context_qualifier_unit_premises import proposal as regression_control


class CoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = previous.load_cases()
        cls.prior = json.loads(previous.OUTPUT.read_text())

    def case(self, draft=None, cid='in_core:mention'):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        if draft is not None:
            c['draft'] = draft
        return c, self.fixture['scenes'][c['sceneKey']]

    def control(self, case, entities):
        build = contrast_control if case['cohort'] == 'contrast' else regression_control
        items = build(case, entities)[v.extraction.VERSION]
        slots = v.model_input(case, entities)['coverageIndex']['slots']
        return {v.VERSION: [dict(slotId=sid, disposition='position',
                                items=[copy.deepcopy(x) for x in items if x['subject'] == slot['subjectRef']])
                            for sid, slot in slots.items()]}

    def test_all_frozen_positions_have_slots_with_source_once_and_no_gold(self):
        total = 0
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            data = v.model_input(c, entities)
            original = v.extraction.model_input(c, entities)
            self.assertEqual({k: data[k] for k in ('source', 'entityRefs')}, original)
            self.assertEqual(''.join(x if isinstance(x, str) else ''.join(x['segments'].values())
                                    for x in data['source']), c['draft'])
            self.assertNotIn('scene:', json.dumps(data))
            self.assertEqual(data, v.model_input(dict(c, targets=[], selectionExpectations=[], id='secret'), entities))
            response = self.control(c, entities)
            decoded, coverage = v.decode(response, c, entities)
            self.assertEqual(v.extraction.assess(decoded, c, entities)['status'], 'matched', c['id'])
            self.assertTrue(coverage['decisionsComplete'])
            self.assertEqual(coverage['semanticCoverage'], 'unverified')
            total += len(decoded[v.extraction.VERSION])
        self.assertEqual(total, 33)

    def test_old_empty_output_and_new_missing_decision_rejected(self):
        c, entities = self.case()
        row = next(r for r in self.prior['cases'] if r['caseId'] == c['id'])
        old = json.loads(row['calls'][0]['content'])
        self.assertEqual(old, {v.extraction.VERSION: []})
        self.assertEqual(v.inspect(old, c, entities)['status'], 'invalid_response')
        result = v.inspect({v.VERSION: []}, c, entities)
        self.assertEqual(result['status'], 'invalid_response')
        self.assertIn('缺少覆盖任务', result['error'])
        self.assertEqual(v.extraction.assess(old, c, entities)['status'], 'mismatched')

    def test_non_position_is_only_a_claim_not_semantic_success(self):
        c, entities = self.case()
        response = self.control(c, entities)
        response[v.VERSION][0].update(disposition='non_position', items=[])
        result = v.inspect(response, c, entities)
        self.assertEqual(result['status'], 'structurally_valid')
        self.assertEqual(result['coverage']['semanticCoverage'], 'unverified')
        self.assertEqual(len(result['coverage']['nonPositionSlots']), 1)
        self.assertFalse(result['productionEnablement'])
        self.assertEqual(v.extraction.assess(result['decodedProposal'], c, entities)['status'], 'mismatched')

    def test_pure_action_cooccurrence_does_not_create_position(self):
        c, entities = self.case('沈砚秋提到了议事殿。')
        slots = v.model_input(c, entities)['coverageIndex']['slots']
        self.assertEqual(len(slots), 1)
        response = {v.VERSION: [dict(slotId=next(iter(slots)), disposition='non_position', items=[])]}
        decoded, coverage = v.decode(response, c, entities)
        self.assertEqual(decoded, {v.extraction.VERSION: []})
        self.assertTrue(coverage['reviewRequired'])

    def test_pronouns_missing_entities_and_cross_unit_are_pending(self):
        for draft in ('她在议事殿内。', '沈砚秋在里面。', '沈砚秋停步。她在议事殿内。', '雨声渐歇。'):
            c, entities = self.case(draft)
            index = v.model_input(c, entities)['coverageIndex']
            self.assertEqual(index['slots'], {})
            self.assertTrue(index['pendingUnits'])
            _, coverage = v.decode({v.VERSION: []}, c, entities)
            self.assertEqual(coverage['pendingUnits'], index['pendingUnits'])
            self.assertEqual(coverage['semanticCoverage'], 'unverified')

    def test_multiple_subjects_are_separate_without_pair_cartesian_product(self):
        c, entities = self.case(cid='hall:two_people')
        index = v.model_input(c, entities)['coverageIndex']
        self.assertEqual(len(index['slots']), 2)
        self.assertEqual([len(x['boundaryRefs']) for x in index['slots'].values()], [2, 2])
        response = self.control(c, entities)
        response[v.VERSION].pop()
        self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response')

    def test_ambiguous_alias_kept_pending(self):
        c, entities = self.case('沈砚秋在议事殿内。')
        entities = copy.deepcopy(entities)
        entities['scene:other_shen'] = dict(kind='scene_person', mentions=['沈砚秋'])
        index = v.model_input(c, entities)['coverageIndex']
        self.assertEqual(index['slots'], {})
        self.assertIn('ambiguous_entity_reference', index['pendingUnits'][0]['reasons'])

    def test_all_budgets_fail_without_truncation(self):
        c, entities = self.case()
        for limit in ('MAX_UNITS', 'MAX_SLOTS', 'MAX_BOUNDARIES_PER_UNIT', 'MAX_INDEX_CHARS'):
            with self.subTest(limit=limit), patch.object(v, limit, 0):
                with self.assertRaisesRegex(ValueError, '预算超限'):
                    v.model_input(c, entities)
        response = self.control(c, entities)
        with patch.object(v, 'MAX_ITEMS', 0):
            self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response')

    def test_unknown_duplicate_rebound_identity_and_mixed_disposition_rejected(self):
        c, entities = self.case()
        base = self.control(c, entities)
        for kind in ('unknown', 'duplicate', 'rebound', 'identity', 'empty_position', 'mixed', 'malformed'):
            response = copy.deepcopy(base)
            decision = response[v.VERSION][0]
            other = c
            if kind == 'unknown': decision['slotId'] = 'missing'
            elif kind == 'duplicate': response[v.VERSION].append(copy.deepcopy(decision))
            elif kind == 'rebound': other = dict(c, draft=c['draft']+'雨停了。')
            elif kind == 'identity': decision['items'][0]['subject'] = decision['items'][0]['boundary']
            elif kind == 'empty_position': decision['items'] = []
            elif kind == 'mixed': decision['disposition'] = 'non_position'
            else: decision['disposition'] = []
            with self.subTest(kind=kind):
                self.assertEqual(v.inspect(response, other, entities)['status'], 'invalid_response')

    def test_condition_survives_and_cross_unit_core_is_not_borrowed(self):
        c, entities = self.case(cid='gate:both_paraphrase')
        response = self.control(c, entities)
        decoded, coverage = v.decode(response, c, entities)
        self.assertEqual(v.extraction.assess(decoded, c, entities)['status'], 'matched')
        self.assertTrue(coverage['pendingUnits'])  # The condition unit is not a position slot.
        response[v.VERSION][0]['items'][0]['core'].insert(0, 'P1-U1-S2')
        self.assertEqual(v.inspect(response, c, entities)['status'], 'invalid_response')

    def test_unresolved_is_visible_and_response_is_not_mutated(self):
        c, entities = self.case()
        response = self.control(c, entities)
        response[v.VERSION][0].update(disposition='unresolved', items=[])
        original = copy.deepcopy(response)
        result = v.inspect(response, c, entities)
        self.assertEqual(response, original)
        self.assertEqual(result['proposedResponse'], original)
        self.assertEqual(len(result['coverage']['unresolvedSlots']), 1)
        self.assertFalse(result['productionEnablement'])

    def test_real_slot_limit_allows_boundary_and_rejects_overflow(self):
        c, entities = self.case('沈砚秋在议事殿内。' * v.MAX_SLOTS)
        self.assertEqual(len(v.model_input(c, entities)['coverageIndex']['slots']), v.MAX_SLOTS)
        c['draft'] += '沈砚秋在议事殿内。'
        with self.assertRaisesRegex(ValueError, '覆盖任务预算超限'):
            v.model_input(c, entities)

    def test_offline_replay_preserves_failures_and_detects_tampering_without_gateway(self):
        from test_support import context_qualifier_coverage_replay as replay
        with patch.object(previous.f, '_gateway') as gateway:
            report = replay.build()
            self.assertEqual(report['priorSummary'], self.prior['summary'])
            self.assertEqual(report['summary']['slots'], 33)
            self.assertEqual(report['summary']['slotsWithoutSubmittedItems'], 1)
            failure = next(r for r in report['cases'] if r['caseId'] == 'in_core:mention')
            self.assertTrue(failure['slotsWithoutSubmittedItems'])
            modality = next(r for r in report['cases'] if r['caseId'] == 'hall:word_mention')
            self.assertEqual(modality['priorStatus'], 'mismatched')
            self.assertEqual(modality['slotsWithoutSubmittedItems'], [])
            with TemporaryDirectory() as d:
                path = Path(d)/'replay.json'
                previous.f.save_checkpoint(path, report, create=True)
                self.assertTrue(replay.audit(path)['allRowsReproduced'])
                report['semanticCoverage'] = 'verified'
                path.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, '不可复现'):
                    replay.audit(path)
            gateway.assert_not_called()

    def test_projected_tasks_preserve_decisions_with_local_pending_ledger(self):
        from test_support import context_qualifier_coverage_projection as projection
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            full = v.model_input(c, entities)
            data = projection.model_input(c, entities)
            self.assertEqual(set(data), {'source', 'entityRefs', 'coverageTasks'})
            self.assertEqual(data['source'], full['source'])
            self.assertEqual(data['entityRefs'], full['entityRefs'])
            self.assertEqual(set(data['coverageTasks']), set(full['coverageIndex']['slots']))
            for sid, task in data['coverageTasks'].items():
                self.assertEqual(task, {k: full['coverageIndex']['slots'][sid][k]
                                        for k in ('subjectRef', 'boundaryRefs')})
            _, ledger = v.decode(self.control(c, entities), c, entities)
            self.assertEqual(ledger['pendingUnits'], full['coverageIndex']['pendingUnits'])
            self.assertLess(len(json.dumps(data)), len(json.dumps(full)))
        c, entities = self.case('她在议事殿内。')
        self.assertEqual(projection.model_input(c, entities)['coverageTasks'], {})
        _, ledger = v.decode({v.VERSION: []}, c, entities)
        self.assertTrue(ledger['pendingUnits'])

    def test_projection_cannot_bypass_budget_or_leak_gold(self):
        from test_support import context_qualifier_coverage_projection as projection
        c, entities = self.case()
        self.assertEqual(projection.model_input(c, entities), projection.model_input(
            dict(c, id='secret', targets=[], selectionExpectations=[]), entities))
        with patch.object(v, 'MAX_SLOTS', 0):
            with self.assertRaisesRegex(ValueError, '预算超限'):
                projection.model_input(c, entities)
