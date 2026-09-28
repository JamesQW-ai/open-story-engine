"""History selection boundaries on official longform context snapshots."""
import copy
import json
import unittest

from open_story_engine.action_review_context import action_review_history
from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.context_bundle import ContextBundleBuilder, ContextBundleError
from open_story_engine.reader_scene_review import grounding_input_evidence, public_scene_evidence
from tests_py import test_context_prompt_integration as fixtures


class ActionReviewContextTests(unittest.TestCase):
    def setUp(self):
        _, _, self.context, self.selected, self.state, self.resolver = next(
            fixtures.ContextPromptIntegrationTests().cases())

    def bundle(self, context=None, **extra):
        context = context or self.context
        planner = PlayerNarrativePlanner(object(), context_resolver=self.resolver)
        planner._prompt(self.context, self.selected, self.state, None)
        return ContextBundleBuilder().build(
            context=context, selected=self.selected, state=self.state,
            branch={'parentBranchId': context['parent']['id']}, module_context=extra.pop('module_context', {}),
            state_visibility=planner.last_context_bundle.as_dict()['stateVisibility'],
            validated_state_patch=self.selected['statePatch'], **extra)

    def select(self, context=None, action='照他说的做', bundle=None):
        context = context or self.context
        bundle = bundle or self.bundle(context)
        evidence = grounding_input_evidence({'contextProjection': bundle.project('grounding_review')})
        return action_review_history(context, action, bundle, evidence), evidence

    def test_all_official_entrances_share_writer_snapshot_and_public_sources(self):
        for case, character, context, selected, state, resolver in fixtures.ContextPromptIntegrationTests().cases():
            with self.subTest(book=case['package_id'], character=character):
                planner = PlayerNarrativePlanner(object(), context_resolver=resolver)
                planner._prompt(context, selected, state, None)
                bundle = planner.last_context_bundle
                before = bundle.as_dict()
                evidence = planner._public_review_evidence(context)
                history, audit = action_review_history(context, context['playerDirection'], bundle, evidence)
                self.assertEqual(history['contextSha256'], bundle.context_sha256)
                self.assertEqual(audit['contextSha256'], bundle.context_sha256)
                self.assertTrue(audit['selectedSourceIds'])
                self.assertTrue(set(audit['selectedSourceIds']) <= evidence.keys())
                self.assertEqual(before, bundle.as_dict())
                self.assertTrue(all('content' not in entry for entry in history['sourceReferences']))

    def test_long_irrelevant_ancestry_does_not_expand_history(self):
        expected, _ = self.select()
        context = copy.deepcopy(self.context)
        context['lineage'] = [dict(id=f'old-{i}', summary='旧日雨声', narrativeText='不可注入的旧正文')
                              for i in range(1000)] + context['lineage']
        (history, audit), _ = self.select(context)
        self.assertEqual(history['sourceReferences'], expected[0]['sourceReferences'])
        self.assertNotIn('不可注入', json.dumps(history, ensure_ascii=False))
        self.assertIn('branch:lineage:old-999', audit['omittedContinuitySourceIds'])

    def test_compound_action_selects_both_topics_and_pronoun_keeps_parent(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] = '册页已经核对完毕。'
        context['lineage'] = [dict(id='prior', summary='铜牌暂放桌上。'), context['parent']]
        (history, audit), _ = self.select(context, action='核对册页，然后查看铜牌')
        self.assertEqual(len(history['sourceReferences']), 2)
        (_, pronoun_audit), _ = self.select(context)
        self.assertIn('branch:lineage:' + context['parent']['id'], pronoun_audit['selectedSourceIds'])
        self.assertIn('branch:lineage:prior', pronoun_audit['omittedContinuitySourceIds'])

    def test_parent_topic_preserves_immediate_reference_chain(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] = '铜牌由他保管。'
        context['lineage'] = [dict(id='prior', summary='铜牌还未交出。'), context['parent']]
        (_, audit), _ = self.select(context)
        self.assertEqual(len(audit['selectedSourceIds']), 2)

    def test_explicit_plan_source_survives_without_lexical_overlap(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] = '册页已经核对完毕。'
        context['lineage'] = [dict(id='prior', summary='铜牌暂放桌上。'), context['parent']]
        context['resultContract'] = {'scenePlan': {'knowledge': [
            {'sources': [{'id': 'branch:lineage:prior', 'quote': '铜牌暂放桌上'}]},
        ]}}
        (history, audit), _ = self.select(context, action='沉默')
        self.assertIn({'sourceId': 'branch:lineage:prior', 'reason': 'scene_plan_source'}, history['sourceReferences'])
        self.assertEqual(audit['omittedContinuitySourceIds'], [])

    def test_deictic_words_do_not_hide_named_objects_or_explicit_sources(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] = '你停下脚步。'
        context['lineage'] = [dict(id='prior', summary='空灯在他们手边。'), context['parent']]
        for action in ('看空灯', '看看空灯', '查看他们手边的空灯'):
            with self.subTest(action=action):
                (_, audit), _ = self.select(context, action=action)
                self.assertIn('branch:lineage:prior', audit['selectedSourceIds'])
        (_, audit), _ = self.select(context, action='看看他们手边的文书')
        self.assertNotIn('branch:lineage:prior', audit['selectedSourceIds'])
        context['resultContract'] = {'scenePlan': {'knowledge': [
            {'sources': [{'id': 'branch:lineage:prior', 'quote': '空灯在他们手边'}]},
        ]}}
        (_, audit), _ = self.select(context, action='照他们说的做')
        self.assertIn('branch:lineage:prior', audit['selectedSourceIds'])

    def test_empty_window_stays_empty_without_legacy_fallback(self):
        bundle_context = {**self.context, 'lineage': []}
        bundle = self.bundle(bundle_context)
        (history, audit), _ = self.select(bundle=bundle)
        self.assertEqual(history['sourceReferences'], [])
        self.assertEqual(audit['source'], 'context_bundle')
        self.assertEqual(history['missingContext'], ['parent_summary'])

    def test_explicit_older_summary_uses_frozen_evidence_without_expanding_window(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] = '你停下脚步。'
        context['lineage'] = [dict(id='recent', summary='雨势未变。'), context['parent']]
        source = 'module:previousBeat:older'
        quote = context['contract']['openingContext']['visibleItems'][0]
        modules = dict(previousBeatSummaries=[quote], previousBeatSummaryIds=['older'])
        context['resultContract'] = {'scenePlan': {'knowledge': [
            {'sources': [{'id': source, 'quote': quote}, {'id': source, 'quote': quote}]},
        ]}}
        bundle = self.bundle(context, module_context=modules)
        before = bundle.as_dict()
        (history, audit), _ = self.select(context, action='继续', bundle=bundle)
        self.assertEqual(audit['selectedSourceIds'].count(source), 1)
        self.assertIn({'sourceId': source, 'reason': 'scene_plan_source'}, history['sourceReferences'])
        self.assertEqual(before, bundle.as_dict())
        self.assertEqual(len(before['continuityWindow']), 2)
        self.assertNotIn(source, [item['sourceId'] for item in before['continuityWindow']])
        self.assertEqual(audit['duplicatedEvidenceCharacters'], 0)

    def test_explicit_reference_rejects_missing_hidden_changed_and_forged_sources(self):
        bundle = self.bundle()
        evidence = grounding_input_evidence({'contextProjection': bundle.project('grounding_review')})
        source = next(iter(evidence))
        quote = evidence[source]
        variants = [
            (source, quote, {}),
            (source, quote, {**evidence, source: quote + '篡改'}),
            (source, '不属于当前来源的引文', evidence),
            ('invented', quote, {**evidence, 'invented': quote}),
            (source, '', evidence),
        ]
        # Hidden facts may exist in the bundle but are never public support,
        # even if a caller injects them into its evidence mapping.
        hidden = dict(sourceId='test:hidden', kind='recent_prose', branchId=self.context['parent']['id'],
                      visibility='author_truth', authority='confirmed_evidence', validity='confirmed',
                      location='test', content='此来源只对作者可见。')
        hidden_bundle = self.bundle(memory_evidence=[hidden])
        hidden_context = copy.deepcopy(self.context)
        hidden_context['resultContract'] = {'scenePlan': {'knowledge': [
            {'sources': [{'id': hidden['sourceId'], 'quote': hidden['content']}]},
        ]}}
        with self.assertRaisesRegex(ContextBundleError, '计划引用'):
            action_review_history(hidden_context, '继续', hidden_bundle,
                                  {**evidence, hidden['sourceId']: hidden['content']})
        for ref_id, ref_quote, supplied in variants:
            context = copy.deepcopy(self.context)
            context['resultContract'] = {'scenePlan': {'knowledge': [
                {'sources': [{'id': ref_id, 'quote': ref_quote}]},
            ]}}
            with self.subTest(source=ref_id, quote=ref_quote), self.assertRaisesRegex(ContextBundleError, '计划引用'):
                action_review_history(context, '继续', bundle, supplied)

    def test_missing_or_altered_evidence_is_rejected(self):
        bundle = self.bundle()
        (_, audit), evidence = self.select(bundle=bundle)
        source = audit['selectedSourceIds'][0]
        for broken in ({}, {**evidence, source: evidence[source] + '改写'}):
            with self.subTest(evidence=broken), self.assertRaises(ContextBundleError):
                action_review_history(self.context, '继续', bundle, broken)

    def test_wrong_branch_is_rejected(self):
        other = {**self.context, 'parent': {**self.context['parent'], 'id': 'sibling'}}
        with self.assertRaisesRegex(ContextBundleError, '父分支'):
            self.select(other, bundle=self.bundle())

    def test_selected_memory_is_not_lexically_dropped_or_duplicated(self):
        parent_id = self.context['parent']['id']
        source = dict(sourceId='memory:goal:test', kind='confirmed_event', branchId=parent_id,
                      visibility='player_known', authority='confirmed_evidence', validity='confirmed',
                      location='test', content=self.context['parent']['summary'])
        memory = dict(memoryId='test-goal', kind='event', sourceIds=[source['sourceId']],
                      branchId=parent_id, visibility='player_known', authority='confirmed_evidence',
                      validity='confirmed', status='confirmed', sequence=1, content=source['content'])
        for status, validity, visible in [('confirmed', 'confirmed', True), ('rejected', 'rejected', False),
                                          ('candidate', 'unknown', False)]:
            with self.subTest(status=status):
                item = {**memory, 'status': status, 'validity': validity}
                bundle = self.bundle(dynamic_memory=[item], memory_evidence=[source])
                (history, audit), evidence = self.select(action='沉默', bundle=bundle)
                self.assertEqual(source['sourceId'] in audit['selectedSourceIds'], visible)
                if visible:
                    self.assertEqual(audit['selectedSourceIds'].count(source['sourceId']), 1)
                    evidence.pop(source['sourceId'])
                    with self.assertRaisesRegex(ContextBundleError, '动态记忆'):
                        action_review_history(self.context, '沉默', bundle, evidence)

    def test_large_parent_source_is_referenced_without_truncation_or_copy(self):
        context = copy.deepcopy(self.context)
        context['parent']['summary'] += '。' * 6000
        (history, audit), evidence = self.select(context)
        self.assertGreater(len(evidence['branch:lineage:' + context['parent']['id']]), 6000)
        self.assertLess(audit['serializedCharacters'], 1200)
        self.assertEqual(audit['duplicatedEvidenceCharacters'], 0)

    def test_legacy_is_explicit_parent_only_and_missing_is_not_filled(self):
        context = copy.deepcopy(self.context)
        context['lineage'].insert(0, dict(id='old', summary='旧日雨声'))
        evidence = public_scene_evidence(context)
        history, audit = action_review_history(context, '继续', None, evidence)
        self.assertEqual(audit['selectedSourceIds'], [f"history-{context['parent']['id']}-P1"])
        self.assertEqual(history['coverage'], 'legacy_parent_only')
        history, _ = action_review_history(context, '继续', None, {})
        self.assertEqual(history['sourceReferences'], [])
        self.assertEqual(history['missingContext'], ['parent_summary'])
