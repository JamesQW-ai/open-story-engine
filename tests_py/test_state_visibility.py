import unittest
from pathlib import Path

from open_story_engine.content import (
    StoryPackageError,
    expand_state_visibility,
    load_runtime_story_package,
    validate_state_visibility_declaration,
)
from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.cocreation import LlmPlanner
from open_story_engine.llm import LlmError


class StateVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.package = {
            "stateModel": {
                "runtimeStateFields": ["characterLocationIds"],
                "locationReferenceFields": ["playerLocationId"],
                "monotonicEnums": {"sourceProgress": ["chapter-001"]},
            }
        }

    def test_static_and_template_declarations_expand_to_normalized_leaf_paths(self):
        declaration = {
            "schemaVersion": "state-visibility/0.2",
            "paths": {"/playerLocationId": "player_known"},
            "pathTemplates": {"/characterLocationIds/{playerCharacterId}": "character_known"},
        }
        validate_state_visibility_declaration(self.package, declaration)
        state = {
            "playerCharacterId": "character/a",
            "playerLocationId": "location-1",
            "characterLocationIds": {"character/a": "location-1"},
        }
        self.assertEqual(
            expand_state_visibility(declaration, state, self.package),
            {
                "/playerLocationId": "player_known",
                "/characterLocationIds/character~1a": "character_known",
            },
        )

    def test_version_01_is_static_only(self):
        declaration = {
            "schemaVersion": "state-visibility/0.1",
            "paths": {"/playerLocationId": "player_known"},
        }
        validate_state_visibility_declaration(self.package, declaration)
        with self.assertRaisesRegex(StoryPackageError, "不支持 pathTemplates"):
            validate_state_visibility_declaration(self.package, {**declaration, "pathTemplates": {}})
        with self.assertRaisesRegex(StoryPackageError, "未声明状态字段"):
            validate_state_visibility_declaration(
                self.package,
                {
                    "schemaVersion": "state-visibility/0.2",
                    "paths": {},
                    "pathTemplates": {"/characterLocationIds/{unknownId}": "player_known"},
                },
            )
        with self.assertRaisesRegex(StoryPackageError, "未声明状态字段"):
            expand_state_visibility(
                {
                    "schemaVersion": "state-visibility/0.2",
                    "paths": {},
                    "pathTemplates": {"/characterLocationIds/{unknownId}": "player_known"},
                },
                {"unknownId": "character/secret", "characterLocationIds": {"character/secret": "gate"}},
                self.package,
            )

    def test_rejects_unresolved_paths_and_conflicting_collisions(self):
        with self.assertRaisesRegex(StoryPackageError, "无法解析状态字段"):
            expand_state_visibility(
                {"schemaVersion": "state-visibility/0.2", "paths": {}, "pathTemplates": {"/x/{missing}": "player_known"}},
                {"x": "value"},
                {**self.package, "stateModel": {**self.package["stateModel"], "runtimeStateFields": ["characterLocationIds", "missing"]}},
            )
        declaration = {
            "schemaVersion": "state-visibility/0.2",
            "paths": {"/location-1": "player_known"},
            "pathTemplates": {"/{playerLocationId}": "author_truth"},
        }
        with self.assertRaisesRegex(StoryPackageError, "冲突可见性"):
            expand_state_visibility(declaration, {"playerLocationId": "location-1", "location-1": "value"}, self.package)

    def test_current_official_package_uses_formal_visibility_declaration(self):
        package_path = Path("content/packages/taixu-relics-part1/0.1.3/package.json")
        package = load_runtime_story_package(package_path, lazy=True)
        self.assertEqual(package["stateVisibility"]["schemaVersion"], "state-visibility/0.2")
        self.assertEqual(package["stateVisibility"]["paths"], {
            "/playerCharacterId": "player_known",
            "/playerLocationId": "player_known",
        })
        self.assertEqual(package["stateVisibility"]["pathTemplates"], {
            "/characterLocationIds/{playerCharacterId}": "player_known",
        })

    def test_planner_audit_uses_package_declaration_before_fallback(self):
        planner = LlmPlanner(object())
        context = {
            "package": {
                "id": "pkg", "version": "0.1",
                "stateModel": {
                    "runtimeStateFields": ["characterLocationIds"],
                    "locationReferenceFields": ["playerLocationId"],
                    "monotonicEnums": {},
                },
                "stateVisibility": {
                    "schemaVersion": "state-visibility/0.2",
                    "paths": {"/playerLocationId": "player_known"},
                    "pathTemplates": {"/characterLocationIds/{playerCharacterId}": "player_known"},
                },
            },
            "playerDirection": "继续等待",
            "lineage": [],
            "parent": {"id": "node-1"},
            "validatedStatePatch": {"playerLocationId": "gate"},
            "validatedStatePatchSource": "apply_branch_patch",
        }
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {"playerLocationId": "gate"}}
        state = {
            "playerCharacterId": "character/player",
            "playerLocationId": "gate",
            "characterLocationIds": {"character/player": "gate"},
        }
        module_context = {
            "modulePaths": [], "world": {}, "currentChapter": {},
            "currentBeat": {"id": "beat-1", "summary": "当前"},
        }
        audit = planner._record_context_bundle_audit(context, selected, state, module_context)
        self.assertEqual(audit["status"], "recorded")
        self.assertEqual(audit["stateVisibilityEntries"], 2)
        self.assertEqual(audit["bundle"]["provenance"]["stateVisibilitySource"], "explicit")
        self.assertEqual(set(audit["bundle"]["stateVisibility"]), {
            "/playerLocationId", "/characterLocationIds/character~1player",
        })
        self.assertEqual(audit["stateVisibilityMode"], "audit_fallback")
        self.assertEqual(audit["bundle"]["provenance"]["stateVisibilityMode"], "audit_fallback")

    def _formal_context(self, declaration=None):
        package = {
            "id": "pkg", "version": "0.1",
            "stateModel": {
                "runtimeStateFields": ["playerLocationId"],
                "locationReferenceFields": ["playerLocationId"],
                "monotonicEnums": {},
            },
        }
        if declaration is not None:
            package["stateVisibility"] = declaration
        return {
            "package": package,
            "contextProjection": {"stateVisibilityMode": "formal_required"},
            "playerDirection": "继续等待",
            "lineage": [],
            "parent": {"id": "node-1", "sessionId": "session-1"},
            "requestId": "request-1",
            "validatedStatePatch": {"playerLocationId": "gate"},
            "validatedStatePatchSource": "apply_branch_patch",
        }

    def test_formal_mode_emits_independent_rejection_audit_for_missing_declaration(self):
        planner = LlmPlanner(object())
        context = self._formal_context()
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {"playerLocationId": "gate"}}
        module_context = {"modulePaths": [], "world": {}, "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"}}
        audit = planner._record_context_bundle_audit(context, selected, {"playerLocationId": "gate"}, module_context)
        self.assertEqual(audit["status"], "rejected")
        self.assertEqual(audit["stateVisibilityMode"], "formal_required")
        self.assertEqual(audit["projectionRejectionAudit"]["auditType"], "state_visibility_projection_rejected")
        self.assertEqual(audit["projectionRejectionAudit"]["reason"], "missing_declaration")
        self.assertEqual(audit["projectionRejectionAudit"]["requestId"], "request-1")
        self.assertIsNone(planner.last_context_bundle)

    def test_formal_mode_classifies_version_and_path_failures(self):
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {"playerLocationId": "gate"}}
        module_context = {"modulePaths": [], "world": {}, "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"}}
        for declaration, reason in (
            ({"schemaVersion": "state-visibility/9.9", "paths": {}}, "version_mismatch"),
            ({"schemaVersion": "state-visibility/0.2", "paths": {"/missing": "player_known"}}, "unresolved_path"),
        ):
            planner = LlmPlanner(object())
            audit = planner._record_context_bundle_audit(
                self._formal_context(declaration), selected, {"playerLocationId": "gate"}, module_context,
            )
            self.assertEqual(audit["projectionRejectionAudit"]["reason"], reason)

    def test_formal_mode_rejects_before_bundle_when_module_projection_is_unavailable(self):
        planner = LlmPlanner(object())
        context = self._formal_context()
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {"playerLocationId": "gate"}}
        audit = planner._record_context_bundle_audit(context, selected, {"playerLocationId": "gate"}, None)
        self.assertEqual(audit["projectionRejectionAudit"]["reason"], "missing_declaration")
        self.assertEqual(planner._context_projection_rejection["status"], "rejected")

    def test_planner_level_projection_config_applies_without_context_field(self):
        planner = LlmPlanner(object(), context_projection={"stateVisibilityMode": "formal_required"})
        context = self._formal_context()
        context.pop("contextProjection")
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {"playerLocationId": "gate"}}
        audit = planner._record_context_bundle_audit(context, selected, {"playerLocationId": "gate"}, None)
        self.assertEqual(audit["projectionRejectionAudit"]["reason"], "missing_declaration")

    def test_invalid_projection_mode_type_is_controlled(self):
        planner = LlmPlanner(object())
        context = self._formal_context()
        context["contextProjection"] = {"stateVisibilityMode": []}
        with self.assertRaisesRegex(LlmError, "stateVisibilityMode 无效"):
            planner._preflight_context_projection(context, {}, {})

    def test_player_formal_gate_runs_before_any_model_call(self):
        class CountingGateway:
            model = "test"

            def __init__(self):
                self.calls = 0

            def complete_json(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("formal projection rejection must precede model calls")

            def complete_text(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("formal projection rejection must precede model calls")

        gateway = CountingGateway()
        planner = PlayerNarrativePlanner(gateway, context_projection={"stateVisibilityMode": "formal_required"})
        context = {"package": {}, "contract": {"persona": {"name": "测试角色"}}}
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {}}
        with self.assertRaisesRegex(LlmError, "要求业务包声明 stateVisibility"):
            planner.plan(context, selected, {})
        self.assertEqual(gateway.calls, 0)

    def test_player_formal_gate_rejects_module_resolution_before_any_model_call(self):
        class CountingGateway:
            model = "test"

            def __init__(self):
                self.calls = 0

            def complete_json(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("module projection rejection must precede model calls")

            def complete_text(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("module projection rejection must precede model calls")

        class BrokenResolver:
            def resolve(self, *args, **kwargs):
                raise ValueError("当前剧情节点模块无效")

        gateway = CountingGateway()
        planner = PlayerNarrativePlanner(
            gateway,
            context_resolver=BrokenResolver(),
            context_projection={"stateVisibilityMode": "formal_required"},
        )
        declaration = {
            "schemaVersion": "state-visibility/0.2",
            "paths": {"/playerLocationId": "player_known"},
        }
        context = self._formal_context(declaration)
        context.pop("contextProjection")
        context["contract"] = {"persona": {"name": "测试角色"}}
        selected = {"title": "继续等待", "summary": "继续等待", "statePatch": {}}
        with self.assertRaisesRegex(LlmError, "模块上下文解析失败") as caught:
            planner.plan(context, selected, {"playerLocationId": "gate"})
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(caught.exception.code, "context_projection_rejected")
        self.assertEqual(planner.last_prompt_context["contextBundle"]["projectionRejectionAudit"]["reason"], "unresolved_path")


if __name__ == "__main__":
    unittest.main()
