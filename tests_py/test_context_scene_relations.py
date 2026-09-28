"""Reviewed relation and prose mapping contracts on official longform evidence."""
import copy
import unittest

from test_support import context_scene_relations as s


class SceneRelationTests(unittest.TestCase):
    def setUp(self):
        self.data, self.review, self.evidence, self.originals, self.scope = copy.deepcopy(s.load_contract())

    def compare(self, mid='original_C10'):
        return s.compare(mid, self.data, self.review, self.evidence, self.originals, self.scope)

    def reseal_for_test(self):
        # Simulates an author-side review of a changed fixture, never model input.
        self.review['artifactDigest'] = s.digest(self.data)

    def test_original_failure_and_four_position_contrasts(self):
        expected = {'original_C10':'contradicted', 'position_out':'supported', 'position_in':'contradicted',
                    'position_not_out':'contradicted', 'position_not_in':'supported'}
        for mid, verdict in expected.items():
            result = self.compare(mid)
            self.assertEqual(result['verdict'], verdict)
            self.assertFalse(result['proseAcceptance'])
            self.assertEqual(result['sources'][0]['span']['quote'], '陌生伤者与空灯被挡在封山线外')

    def test_each_scope_change_requires_new_review_including_child_branch(self):
        for key in self.scope:
            with self.subTest(key=key):
                active = dict(self.scope, **{key:self.scope[key] + '-changed'})
                result = s.compare('original_C10', self.data, self.review, self.evidence, self.originals, active)
                self.assertEqual(result['verdict'], 'needs_review')

    def test_model_review_flags_do_not_grant_mapping_approval(self):
        self.review['mappingIds'].remove('original_C10')
        mapping = self.data['mappings'][-1]
        mapping['reviewed'] = True
        mapping['authority'] = 'authoritative'
        self.reseal_for_test()
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_fact_and_entities_need_separate_review(self):
        self.review['factIds'] = []
        self.assertEqual(self.compare()['verdict'], 'needs_review')
        self.review['factIds'] = ['wounded-side']
        self.review['entityIds'].remove('scene:wounded_stranger')
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_changed_relation_cannot_reuse_old_review_receipt(self):
        self.data['facts'][0]['normal']['value'] = 'inside'
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_mapping_binds_full_original_statement_and_span(self):
        for key, bad in [('statement','伤者在封山线外。'), ('draftSha256','0'*64),
                         ('span',dict(start=65,end=86,quote='伪造引文'))]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data['mappings'][-1][key] = bad
                review = dict(self.review, artifactDigest=s.digest(data))
                self.assertEqual(s.compare('original_C10', data, review, self.evidence, self.originals, self.scope)['verdict'], 'needs_review')

    def test_visibility_or_source_change_invalidates_relations(self):
        key = self.data['facts'][0]['source']['id']
        self.evidence.pop(key)
        self.assertEqual(self.compare()['verdict'], 'needs_review')
        self.evidence[key] = '你携引荐文书来到山门，陌生伤者与空灯被挡在封山线内。'
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_source_change_outside_quoted_span_also_invalidates_review(self):
        key = self.data['facts'][0]['source']['id']
        self.evidence[key] += '之后伤者被移到了线内。'
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_malformed_input_cannot_crash_or_grant_support(self):
        for value in (None, [], '', 1):
            self.assertEqual(s.compare('original_C10', value, self.review, self.evidence, self.originals, self.scope)['verdict'], 'needs_review')
        self.assertEqual(self.compare([])['verdict'], 'needs_review')

    def test_time_condition_modality_and_unknown_entities_are_not_discarded(self):
        for changes in (dict(time='past'), dict(condition='before_lock'), dict(modality='possible'),
                        dict(subject='unknown'), dict(object='玄霄宗'), dict(value='unknown')):
            with self.subTest(changes=changes):
                data = copy.deepcopy(self.data)
                data['mappings'][-1]['normal'].update(changes)
                review = dict(self.review, artifactDigest=s.digest(data))
                result = s.compare('original_C10', data, review, self.evidence, self.originals, self.scope)
                self.assertEqual(result['verdict'], 'needs_review')

    def test_conflicting_reviewed_facts_block_without_selecting_one(self):
        other = copy.deepcopy(self.data['facts'][0])
        other['id'] = 'conflicting-side'
        other['normal']['value'] = 'inside'
        self.data['facts'].append(other)
        self.review['factIds'].append(other['id'])
        self.reseal_for_test()
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_negative_source_cannot_infer_unique_opposite_position(self):
        self.data['facts'][0]['normal'].update(value='inside', polarity='negative')
        self.reseal_for_test()
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_boundary_is_third_value_not_automatic_outside(self):
        # Reviewed hypothetical data exercises the three-value domain; this does
        # not alter the frozen official fact or serve as semantic evidence.
        self.data['facts'][0]['normal']['value'] = 'on_boundary'
        self.reseal_for_test()
        self.assertEqual(self.compare('position_out')['verdict'], 'contradicted')
        self.assertEqual(self.compare('position_not_in')['verdict'], 'supported')

    def test_unmapped_claim_and_whole_prose_cannot_be_authorized(self):
        self.assertEqual(self.compare('alive')['verdict'], 'needs_review')
        report = s.audit()
        self.assertEqual(report['summary'], dict(reviewedMappings=5, matched=5, unmappedContrasts=12))
        self.assertFalse(report['automaticExtractionVerified'])
        self.assertFalse(report['acceptance'])
        self.assertFalse(report['productionEnablement'])
        self.assertEqual(report['modelCalls'], 0)

    def test_review_scope_cannot_be_changed_to_production(self):
        self.review['scope'] = 'production'
        self.assertEqual(self.compare()['verdict'], 'needs_review')

    def test_duplicate_or_missing_review_targets_block(self):
        for ids in (['wounded-side', 'wounded-side'], ['missing'], [True]):
            self.review['factIds'] = ids
            self.assertEqual(self.compare()['verdict'], 'needs_review')
