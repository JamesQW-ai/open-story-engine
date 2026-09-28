"""Required turn snapshots fail before model calls on official longform data."""
import copy
import unittest
from unittest.mock import Mock

from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.cocreation import LlmPlanner
from open_story_engine.llm import LlmError
from tests_py import test_context_prompt_integration as fixtures


class RequiredContextTests(unittest.TestCase):
    def setUp(self):
        _, _, self.context, self.selected, self.state, self.resolver = next(
            fixtures.ContextPromptIntegrationTests().cases())

    def assert_rejected(self, planner, context=None):
        context = self.context if context is None else context
        # Lazy package module caches have identity-based equality; verify the
        # authoritative inputs, not the resolver's internal cache objects.
        before = copy.deepcopy(({k: v for k, v in context.items() if k != 'package'}, self.selected, self.state))
        with self.assertRaises(LlmError) as caught:
            planner.plan(context, self.selected, self.state)
        self.assertEqual(caught.exception.code, 'context_projection_rejected')
        planner.gateway.complete_json.assert_not_called()
        planner.gateway.complete_text.assert_not_called()
        self.assertIsNone(planner.last_context_bundle)
        self.assertEqual(planner.last_prompt_context['contextBundle']['status'], 'rejected')
        self.assertEqual(before, ({k: v for k, v in context.items() if k != 'package'}, self.selected, self.state))
        self.assertEqual(caught.exception.audit['rawResponse'], '')
        return planner.last_prompt_context['contextBundle']

    def test_required_snapshots_build_for_every_official_entrance_in_both_modes(self):
        for case, character, context, selected, state, resolver in fixtures.ContextPromptIntegrationTests().cases():
            for mode in ('audit_fallback', 'formal_required'):
                with self.subTest(book=case['package_id'], character=character, mode=mode):
                    planner = PlayerNarrativePlanner(Mock(), context_resolver=resolver,
                        require_context_bundle=True, context_projection={'stateVisibilityMode': mode})
                    planner._preflight_context_projection(context, selected, state)
                    self.assertIsNotNone(planner.last_context_bundle)
                    self.assertEqual(planner.last_prompt_context['contextBundle']['status'], 'recorded')
                    self.assertEqual(planner.last_context_bundle.branch['parentBranchId'], context['parent']['id'])
                    planner.gateway.complete_json.assert_not_called()

    def test_formal_mode_rejects_missing_resolver_even_with_valid_visibility(self):
        planner = PlayerNarrativePlanner(Mock(model='fixture'),
                                        context_projection={'stateVisibilityMode': 'formal_required'})
        audit = self.assert_rejected(planner)
        self.assertEqual(audit['bundleRejectionAudit']['reason'], 'module_context_unavailable')

    def test_runtime_requirement_cannot_be_disabled_by_context_visibility_mode(self):
        planner = PlayerNarrativePlanner(Mock(model='fixture'), require_context_bundle=True)
        context = {**self.context, 'contextProjection': {'stateVisibilityMode': 'audit_fallback'}}
        self.assert_rejected(planner, context)

    def test_required_mode_rejects_resolver_exception_none_and_wrong_shape(self):
        for resolver in (Mock(resolve=Mock(side_effect=OSError('module unavailable'))),
                         Mock(resolve=Mock(return_value=None)), Mock(resolve=Mock(return_value=[]))):
            with self.subTest(resolver=resolver):
                planner = PlayerNarrativePlanner(Mock(model='fixture'), context_resolver=resolver,
                                                require_context_bundle=True)
                self.assert_rejected(planner)

    def test_formal_mode_rejects_generic_bundle_error(self):
        context = dict(self.context)
        context.pop('validatedStatePatchSource')
        planner = PlayerNarrativePlanner(Mock(model='fixture'), context_resolver=self.resolver,
                                        context_projection={'stateVisibilityMode': 'formal_required'})
        audit = self.assert_rejected(planner, context)
        self.assertIn('状态层验证标记', audit['error'])

    def test_protected_overflow_rejects_even_in_optional_audit_mode(self):
        planner = PlayerNarrativePlanner(Mock(model='fixture'), context_resolver=self.resolver,
                                        context_window_tokens=1)
        audit = self.assert_rejected(planner)
        self.assertEqual(audit['contextBudget']['code'], 'context_budget_exceeded')

    def test_reused_planner_cannot_keep_old_snapshot_after_missing_or_broken_resolver(self):
        planner = PlayerNarrativePlanner(Mock(model='fixture'), context_resolver=self.resolver)
        for broken in (None, Mock(resolve=Mock(side_effect=ValueError('broken')))):
            planner.context_resolver = self.resolver
            planner._preflight_context_projection(self.context, self.selected, self.state)
            self.assertIsNotNone(planner.last_context_bundle)
            planner.context_resolver = broken
            planner._preflight_context_projection(self.context, self.selected, self.state)
            self.assertIsNone(planner.last_context_bundle)
            self.assertIsNone(planner.writing_scope)
            self.assertNotIn('contextSha256', planner.last_prompt_context['contextBundle'])

    def test_writer_entry_cannot_bypass_required_snapshot_gate(self):
        for planner_class in (PlayerNarrativePlanner, LlmPlanner):
            with self.subTest(planner=planner_class):
                planner = planner_class(Mock(), require_context_bundle=True)
                with self.assertRaises(LlmError):
                    planner._prompt(self.context, self.selected, self.state, None)
                planner.gateway.complete_text.assert_not_called()

    def test_compatibility_remains_explicit_in_audit_when_bundle_is_optional(self):
        planner = PlayerNarrativePlanner(Mock())
        planner._prompt(self.context, self.selected, self.state, None)
        self.assertEqual(planner.last_prompt_context['contextBundle'],
                         {'status': 'skipped', 'reason': 'module_context_unavailable'})
        self.assertEqual(planner.last_prompt_context['chapterContinuity']['reason'], 'bundle_unavailable')
