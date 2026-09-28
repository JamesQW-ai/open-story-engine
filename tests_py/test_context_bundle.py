import unittest

from open_story_engine.context_budget import ContextBudgetError, apply_context_budget, estimate_text_tokens
from open_story_engine.context_bundle import (
    CONTEXT_PRIORITY,
    ContextBundle,
    ContextBundleBuilder,
    ContextBundleError,
    context_priority,
    validate_dynamic_memory_transition,
    validate_bundle,
)
from open_story_engine.cocreation import LlmPlanner


def make_bundle(evidence=None, turn_intent=None):
    return ContextBundle.create(
        context_id="ctx-test",
        package={"id": "pkg", "version": "0.1"},
        branch={"branchId": "branch-1", "parentBranchId": "root"},
        hard_constraints={"perspective": "second_person_limited", "stopPoint": "原地等待"},
        turn_intent=turn_intent or {"rawInput": "只说明打算", "atomicRequirements": ["说明", "等待"]},
        authoritative_state={"playerLocationId": "location-1", "itemState": {"item-1": "held"}},
        state_visibility={"/playerLocationId": "player_known", "/itemState/item-1": "player_known"},
        allowed_evidence=evidence or [
            {"sourceId": "state-1", "kind": "confirmed_event", "visibility": "player_known", "branchId": "branch-1", "location": "state", "content": "木牌在手中", "authority": "authoritative", "validity": "confirmed", "tier": "protected"},
            {"sourceId": "style-1", "kind": "style_sample", "visibility": "player_known", "branchId": "package", "location": "style", "content": "短句", "authority": "style_only", "validity": "confirmed"},
        ],
        continuity_window=({
            "sourceId": "history-1", "branchId": "branch-1", "status": "confirmed",
            "content": "上一段已确认正文",
        },),
        style_guide={"pacing": "compact"},
        output_contract={"mustStop": True},
        provenance={"sourceIds": ["state-1", "style-1"]},
    )


class ContextBundleTests(unittest.TestCase):
    def test_opening_excerpts_remain_reviewable_without_resetting_writer_snapshot(self):
        base = dict(kind='public_fact', visibility='player_known', branchId='package',
                    authority='confirmed_evidence', validity='confirmed')
        current = dict(base, sourceId='module:opening:visibleItems:0',
                       location='module.openingContext.visibleItems', content='引荐文书在你手中，尚未交出。')
        past = dict(base, sourceId='module:opening:evidence:0',
                    location='module.openingContext.evidence', content='陆照临摸了摸怀里的引荐文书。')
        known = dict(base, sourceId='module:opening:knownFacts:0',
                     location='module.openingContext.knownFacts', content='老人让你来问山门的灯。')
        bundle = make_bundle([current, past, known])
        frozen = bundle.as_dict()
        for stage in ('chapter', 'result_contract', 'repair'):
            with self.subTest(stage=stage):
                evidence = bundle.project(stage)['allowedEvidence']
                self.assertEqual(evidence, [current, known])
        for stage in ('grounding_review', 'fact_extract'):
            with self.subTest(stage=stage):
                self.assertIn(past, bundle.project(stage)['allowedEvidence'])
        self.assertEqual(bundle.as_dict(), frozen)
        self.assertEqual(ContextBundle.from_dict(frozen).context_sha256, bundle.context_sha256)

    def test_hash_is_stable_when_evidence_order_and_dict_order_change(self):
        first = make_bundle()
        second = make_bundle(list(reversed(list(first.allowed_evidence))))
        self.assertEqual(first.context_sha256, second.context_sha256)

    def test_rejects_cross_branch_or_rejected_evidence(self):
        with self.assertRaises(ContextBundleError):
            make_bundle([{
                "sourceId": "other", "kind": "confirmed_event", "visibility": "player_known",
                "branchId": "branch-2", "location": "state", "content": "x",
                "authority": "authoritative", "validity": "confirmed",
            }])
        with self.assertRaises(ContextBundleError):
            make_bundle([{
                "sourceId": "rejected", "kind": "recent_prose", "visibility": "player_known",
                "branchId": "branch-1", "location": "history", "content": "x",
                "authority": "confirmed_evidence", "validity": "rejected",
            }])

    def test_rejects_unknown_kind_duplicate_source_and_bad_package(self):
        with self.assertRaisesRegex(ContextBundleError, 'kind 无效'):
            make_bundle([{**make_bundle().allowed_evidence[0], 'kind': 'future_secret'}])
        duplicate = list(make_bundle().allowed_evidence)
        duplicate.append(dict(duplicate[0]))
        with self.assertRaisesRegex(ContextBundleError, 'sourceId 不能重复'):
            make_bundle(duplicate)
        with self.assertRaisesRegex(ContextBundleError, 'package.version'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg'}, branch={'branchId': 'b'},
                hard_constraints={}, turn_intent={}, authoritative_state={}, allowed_evidence=[])

    def test_rejects_malformed_selected_direction_projection(self):
        cases = [
            ({"title": []}, "title 必须是非空字符串"),
            ({"summary": " "}, "summary 必须是非空字符串"),
            ({"id": "x" * 201}, "id 超过 200 字符上限"),
            ({"title": "x" * 201}, "title 超过 200 字符上限"),
            ({"summary": "x" * 1001}, "summary 超过 1000 字符上限"),
        ]
        for selected, message in cases:
            with self.subTest(selected=selected), self.assertRaisesRegex(ContextBundleError, message):
                make_bundle(turn_intent={"rawInput": "只说明打算", "selectedDirection": selected})
        with self.assertRaisesRegex(ContextBundleError, "selectedDirection 必须是对象"):
            make_bundle(turn_intent={"rawInput": "只说明打算", "selectedDirection": []})

    def test_rejects_unbounded_or_unconfirmed_continuity_window(self):
        base = {"sourceId": "history-1", "branchId": "branch-1", "status": "confirmed", "content": "上一段"}
        with self.assertRaisesRegex(ContextBundleError, "最多保留 2 条"):
            ContextBundle.create(
                context_id="ctx", package={"id": "pkg", "version": "0.1"},
                branch={"branchId": "branch-1"}, hard_constraints={}, turn_intent={},
                authoritative_state={}, allowed_evidence=[],
                continuity_window=[base, {**base, "sourceId": "history-2"}, {**base, "sourceId": "history-3"}],
            )
        with self.assertRaisesRegex(ContextBundleError, "项缺少字段：branchId,status"):
            ContextBundle.create(
                context_id="ctx", package={"id": "pkg", "version": "0.1"},
                branch={"branchId": "branch-1"}, hard_constraints={}, turn_intent={},
                authoritative_state={}, allowed_evidence=[], continuity_window=[{"sourceId": "history-1", "content": "上一段"}],
            )
        with self.assertRaisesRegex(ContextBundleError, "只能包含 confirmed"):
            ContextBundle.create(
                context_id="ctx", package={"id": "pkg", "version": "0.1"},
                branch={"branchId": "branch-1"}, hard_constraints={}, turn_intent={},
                authoritative_state={}, allowed_evidence=[],
                continuity_window=[{**base, "status": "unknown"}],
            )
        with self.assertRaisesRegex(ContextBundleError, "跨越当前分支"):
            ContextBundle.create(
                context_id="ctx", package={"id": "pkg", "version": "0.1"},
                branch={"branchId": "branch-1"}, hard_constraints={}, turn_intent={},
                authoritative_state={}, allowed_evidence=[],
                continuity_window=[{**base, "branchId": "branch-2"}],
            )

    def test_validates_dynamic_memory_inheritance_and_conflict_links(self):
        source = make_bundle().allowed_evidence[0]
        memory = {
            "memoryId": "memory-1", "kind": "event", "sourceIds": ["state-1"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "authoritative",
            "validity": "confirmed", "status": "confirmed", "sequence": 1, "content": "已确认事件",
        }
        kwargs = dict(
            context_id="ctx", package={"id": "pkg", "version": "0.1"},
            branch={"branchId": "branch-1"}, hard_constraints={}, turn_intent={},
            authoritative_state={}, allowed_evidence=[source], provenance={"sourceIds": ["state-1"]},
        )
        inherited = {**memory, "memoryId": "memory-2", "parentMemoryId": "memory-1", "inheritanceReason": "分支明确允许继承"}
        bundle = ContextBundle.create(**kwargs, dynamic_memory=[inherited])
        self.assertEqual(bundle.dynamic_memory[0]["parentMemoryId"], "memory-1")
        with self.assertRaisesRegex(ContextBundleError, "必须同时提供 inheritanceReason"):
            ContextBundle.create(**kwargs, dynamic_memory=[{**inherited, "inheritanceReason": None}])
        with self.assertRaisesRegex(ContextBundleError, "conflictWith 必须是无重复"):
            ContextBundle.create(**kwargs, dynamic_memory=[{**memory, "conflictWith": ["memory-1"]}])
        with self.assertRaisesRegex(ContextBundleError, "只能与 parentMemoryId 一起"):
            ContextBundle.create(**kwargs, dynamic_memory=[{**memory, "inheritanceReason": "缺少父记忆"}])

    def test_dynamic_memory_transition_guard_enforces_append_only_rules(self):
        candidate = {
            "memoryId": "memory-1", "kind": "event", "sourceIds": ["state-1"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "confirmed_evidence",
            "validity": "unknown", "status": "candidate", "sequence": 1, "content": "待确认事件",
        }
        confirmed = {**candidate, "validity": "confirmed", "status": "confirmed", "sequence": 2}
        validate_dynamic_memory_transition(candidate, confirmed, branch_id="branch-1")
        rejected = {
            **confirmed, "validity": "rejected", "status": "rejected", "sequence": 3,
            "conflictWith": ["memory-contradiction"],
        }
        validate_dynamic_memory_transition(confirmed, rejected, branch_id="branch-1")
        inherited = {
            **confirmed, "memoryId": "memory-2", "branchId": "branch-2", "sequence": 4,
            "parentMemoryId": "memory-1", "inheritanceReason": "分支明确允许继承",
        }
        validate_dynamic_memory_transition(confirmed, inherited, branch_id="branch-2")
        with self.assertRaisesRegex(ContextBundleError, "不允许从 rejected 迁移到 candidate"):
            validate_dynamic_memory_transition(rejected, {**candidate, "sequence": 4}, branch_id="branch-1")
        with self.assertRaisesRegex(ContextBundleError, "必须增加 sequence"):
            validate_dynamic_memory_transition(candidate, {**confirmed, "sequence": 1}, branch_id="branch-1")
        with self.assertRaisesRegex(ContextBundleError, "必须记录 conflictWith"):
            validate_dynamic_memory_transition(
                confirmed,
                {**confirmed, "validity": "rejected", "status": "rejected", "sequence": 3},
                branch_id="branch-1",
            )

    def test_rejects_uncontrolled_visibility_and_invalid_dynamic_memory(self):
        with self.assertRaisesRegex(ContextBundleError, 'visibility 无效'):
            make_bundle([{**make_bundle().allowed_evidence[0], 'visibility': 'current-route'}])
        memory = {
            "memoryId": "memory-1", "kind": "event", "sourceIds": ["state-1"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "confirmed_evidence",
            "validity": "unknown", "status": "candidate", "sequence": 1, "content": "待确认事件",
        }
        with self.assertRaisesRegex(ContextBundleError, 'candidate 动态记忆不得跨分支'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={}, allowed_evidence=[],
                dynamic_memory=[{**memory, 'branchId': 'package'}])

        with self.assertRaisesRegex(ContextBundleError, 'status=.*validity=.*不匹配'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={},
                allowed_evidence=[make_bundle().allowed_evidence[0]],
                provenance={'sourceIds': ['state-1']},
                dynamic_memory=[{**memory, 'status': 'confirmed', 'validity': 'unknown'}])

        with self.assertRaisesRegex(ContextBundleError, 'sourceIds 不能重复'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={},
                allowed_evidence=[make_bundle().allowed_evidence[0]],
                provenance={'sourceIds': ['state-1']},
                dynamic_memory=[{**memory, 'sourceIds': ['state-1', 'state-1']}])

        with self.assertRaisesRegex(ContextBundleError, '无法追溯'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={}, allowed_evidence=[],
                dynamic_memory=[memory])

        with self.assertRaisesRegex(ContextBundleError, 'stateVisibility 必须指向现有叶子字段'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={'location': 'gate'},
                state_visibility={'/missing': 'player_known'}, allowed_evidence=[])

        with self.assertRaisesRegex(ContextBundleError, 'stateVisibility 可见性无效'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={'location': 'gate'},
                state_visibility={'/location': 'author_only'}, allowed_evidence=[])

    def test_priority_order_is_executable(self):
        self.assertEqual(
            CONTEXT_PRIORITY,
            ('hardSpec', 'confirmed_dynamic_memory', 'shortTermMemory.turnIntent',
             'shortTermMemory.continuityWindow', 'unconfirmed_evidence', 'styleGuide'),
        )
        self.assertLess(context_priority('hardSpec'), context_priority('styleGuide'))
        with self.assertRaisesRegex(ContextBundleError, '未知上下文优先级'):
            context_priority('unknown')

    def test_player_projections_exclude_author_truth_and_candidates(self):
        player_evidence = {
            "sourceId": "player-1", "kind": "confirmed_event", "visibility": "player_known",
            "branchId": "branch-1", "location": "state", "content": "公开事件",
            "authority": "confirmed_evidence", "validity": "confirmed",
        }
        author_evidence = {**player_evidence, "sourceId": "author-1", "visibility": "author_truth", "content": "作者真相"}
        confirmed_player = {
            "memoryId": "memory-player", "kind": "event", "sourceIds": ["player-1"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "confirmed_evidence",
            "validity": "confirmed", "status": "confirmed", "sequence": 1, "content": "公开记忆",
        }
        confirmed_author = {**confirmed_player, "memoryId": "memory-author", "sourceIds": ["author-1"], "visibility": "author_truth", "content": "作者记忆"}
        candidate_player = {**confirmed_player, "memoryId": "memory-candidate", "status": "candidate", "validity": "unknown", "sequence": 3, "content": "待确认记忆"}
        bundle = ContextBundle.create(
            context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
            hard_constraints={}, turn_intent={}, authoritative_state={},
            allowed_evidence=[player_evidence, author_evidence],
            dynamic_memory=[confirmed_player, confirmed_author, candidate_player],
            provenance={'sourceIds': ['player-1', 'author-1']},
        )
        chapter = bundle.project('chapter')
        self.assertEqual([item['memoryId'] for item in chapter['dynamicMemory']], ['memory-player'])
        self.assertEqual([item['sourceId'] for item in chapter['allowedEvidence']], ['player-1'])
        self.assertNotIn('provenance', chapter)
        self.assertNotIn('provenance', bundle.project('result_contract'))
        self.assertNotIn('provenance', bundle.project('repair'))

    def test_player_projection_whitelists_authoritative_state_leaves(self):
        bundle = ContextBundle.create(
            context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
            hard_constraints={}, turn_intent={},
            authoritative_state={
                'playerLocationId': 'location-1',
                'authorSecret': 'hidden',
                'nested': {'publicFact': 'visible', 'privateFact': 'hidden'},
            },
            state_visibility={
                '/playerLocationId': 'player_known',
                '/nested/publicFact': 'public_world_fact',
                '/authorSecret': 'author_truth',
                '/nested/privateFact': 'author_truth',
            },
            allowed_evidence=[],
        )
        chapter_state = bundle.project('chapter')['authoritativeState']
        self.assertEqual(
            chapter_state,
            {'playerLocationId': 'location-1', 'nested': {'publicFact': 'visible'}},
        )

    def test_dynamic_memory_source_permissions_must_match(self):
        evidence = {
            'sourceId': 'public-1', 'kind': 'confirmed_event', 'visibility': 'public_world_fact',
            'branchId': 'branch-1', 'location': 'state', 'content': '公开事实',
            'authority': 'confirmed_evidence', 'validity': 'confirmed',
        }
        memory = {
            'memoryId': 'memory-1', 'kind': 'event', 'sourceIds': ['public-1'],
            'branchId': 'branch-1', 'visibility': 'player_known', 'authority': 'confirmed_evidence',
            'validity': 'confirmed', 'status': 'confirmed', 'sequence': 1, 'content': '记忆',
        }
        with self.assertRaisesRegex(ContextBundleError, '可见性不一致'):
            ContextBundle.create(
                context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
                hard_constraints={}, turn_intent={}, authoritative_state={},
                allowed_evidence=[evidence], dynamic_memory=[memory])

    def test_dynamic_memory_order_and_nested_semantic_timestamp_are_hash_stable_only_when_equivalent(self):
        first = {
            "memoryId": "memory-1", "kind": "event", "sourceIds": ["source-1", "source-2"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "confirmed_evidence",
            "validity": "confirmed", "status": "confirmed", "sequence": 1, "content": "记忆一",
        }
        second = {**first, "memoryId": "memory-2", "sourceIds": ["source-2", "source-1"], "sequence": 2, "content": "记忆二"}
        kwargs = dict(
            context_id='ctx', package={'id': 'pkg', 'version': '0.1'}, branch={'branchId': 'branch-1'},
            hard_constraints={}, turn_intent={}, authoritative_state={}, allowed_evidence=[
                {"sourceId": "source-1", "kind": "confirmed_event", "visibility": "player_known", "branchId": "branch-1", "location": "state", "content": "记忆一", "authority": "confirmed_evidence", "validity": "confirmed"},
                {"sourceId": "source-2", "kind": "confirmed_event", "visibility": "player_known", "branchId": "branch-1", "location": "state", "content": "记忆二", "authority": "confirmed_evidence", "validity": "confirmed"},
            ],
            provenance={'sourceIds': ['source-1', 'source-2']},
        )
        ordered = ContextBundle.create(**kwargs, dynamic_memory=[first, second])
        reversed_order = ContextBundle.create(**kwargs, dynamic_memory=[second, first])
        self.assertEqual(ordered.context_sha256, reversed_order.context_sha256)
        changed = {**first, 'content': {'timestamp': 'semantic-value'}}
        changed_bundle = ContextBundle.create(**kwargs, dynamic_memory=[changed, second])
        self.assertNotEqual(ordered.context_sha256, changed_bundle.context_sha256)

    def test_bundle_is_deeply_immutable_and_audit_fields_do_not_change_hash(self):
        bundle = make_bundle()
        with self.assertRaises(TypeError):
            bundle.hard_constraints['perspective'] = 'first_person'
        with self.assertRaises(TypeError):
            bundle.provenance['sourceIds'].append('new-source')
        with self.assertRaises(TypeError):
            bundle.provenance['sourceIds'] += ['new-source']
        with self.assertRaises(TypeError):
            bundle.provenance |= {'requestId': 'req-1'}
        first = bundle.context_sha256
        payload = bundle.as_dict(include_hash=False)
        payload['recordedAt'] = '2026-09-22T10:00:00Z'
        payload['provenance']['requestId'] = 'req-1'
        self.assertEqual(first, ContextBundle.from_dict({**payload, 'contextSha256': first}).context_sha256)

    def test_round_trip_and_projection_keep_traceability(self):
        bundle = make_bundle()
        restored = ContextBundle.from_dict(bundle.as_dict())
        self.assertEqual(restored.context_sha256, bundle.context_sha256)
        projected = restored.project('grounding_review')
        self.assertEqual(projected['package'], bundle.package)
        self.assertEqual(projected['branch'], bundle.branch)
        self.assertEqual(projected['provenance'], bundle.provenance)
        tampered = bundle.as_dict()
        tampered['contextSha256'] = '0' * 64
        with self.assertRaisesRegex(ContextBundleError, '不匹配'):
            ContextBundle.from_dict(tampered)

    def test_projection_keeps_style_out_of_result_contract(self):
        result = make_bundle().project("result_contract")
        self.assertEqual([item["sourceId"] for item in result["allowedEvidence"]], ["state-1"])
        self.assertNotIn("styleGuide", result)

    def test_all_stage_projections_keep_distinct_read_only_boundaries(self):
        bundle = make_bundle()

        result_contract = bundle.project("result_contract")
        self.assertEqual(result_contract["authoritativeState"], {
            "playerLocationId": "location-1",
            "itemState": {"item-1": "held"},
        })
        self.assertEqual([item["sourceId"] for item in result_contract["allowedEvidence"]], ["state-1"])
        self.assertNotIn("styleGuide", result_contract)
        self.assertNotIn("continuityWindow", result_contract)
        self.assertNotIn("provenance", result_contract)

        chapter = bundle.project("chapter")
        self.assertEqual(chapter["authoritativeState"], result_contract["authoritativeState"])
        self.assertEqual([item["sourceId"] for item in chapter["allowedEvidence"]], ["state-1"])
        self.assertIn("continuityWindow", chapter)
        self.assertIn("styleGuide", chapter)
        self.assertNotIn("provenance", chapter)

        fact_extract = bundle.project("fact_extract")
        self.assertEqual(fact_extract["authoritativeState"], bundle.authoritative_state)
        self.assertEqual(fact_extract["allowedEvidence"], list(bundle.allowed_evidence))
        self.assertEqual(fact_extract["provenance"], bundle.provenance)
        self.assertNotIn("styleGuide", fact_extract)

        grounding_review = bundle.project("grounding_review")
        self.assertEqual(grounding_review["authoritativeState"], bundle.authoritative_state)
        self.assertEqual(grounding_review["allowedEvidence"], list(bundle.allowed_evidence))
        self.assertEqual(grounding_review["provenance"], bundle.provenance)
        self.assertEqual(grounding_review["dynamicMemory"], list(bundle.dynamic_memory))

        repair = bundle.project("repair")
        self.assertEqual(repair["authoritativeState"], result_contract["authoritativeState"])
        self.assertEqual([item["sourceId"] for item in repair["allowedEvidence"]], ["state-1"])
        self.assertIn("continuityWindow", repair)
        self.assertNotIn("styleGuide", repair)
        self.assertNotIn("provenance", repair)

    def test_budget_omits_soft_items_and_records_reason(self):
        bundle = make_bundle()
        result = apply_context_budget(bundle, context_window_tokens=220, reserved_output_tokens=0)
        self.assertIn("styleGuide", result.omitted_sources)
        self.assertIn("continuity:history-1", result.omitted_sources)
        self.assertEqual([item["sourceId"] for item in result.bundle.project("chapter")["allowedEvidence"]], ["state-1"])
        self.assertEqual(result.bundle.provenance["excludedSources"], [])
        self.assertEqual(result.bundle.style_guide, {})
        validate_bundle(result.bundle)

    def test_budget_keeps_dynamic_memory_when_soft_items_are_omitted(self):
        source = {
            "sourceId": "memory-source", "kind": "confirmed_event", "visibility": "player_known",
            "branchId": "branch-1", "location": "state", "content": "已确认事件",
            "authority": "confirmed_evidence", "validity": "confirmed",
        }
        memory = {
            "memoryId": "memory-1", "kind": "event", "sourceIds": ["memory-source"],
            "branchId": "branch-1", "visibility": "player_known", "authority": "confirmed_evidence",
            "validity": "confirmed", "status": "confirmed", "sequence": 1, "content": "已确认记忆",
        }
        base = make_bundle()
        bundle = ContextBundle.create(
            context_id="ctx-budget-memory", package=base.package, branch=base.branch,
            hard_constraints=base.hard_constraints, turn_intent=base.turn_intent,
            authoritative_state=base.authoritative_state, state_visibility=base.state_visibility,
            allowed_evidence=[*base.allowed_evidence, source],
            continuity_window=base.continuity_window, dynamic_memory=[memory],
            style_guide={"pacing": "x" * 80}, output_contract=base.output_contract,
            provenance=base.provenance,
        )
        result = apply_context_budget(bundle, context_window_tokens=340)
        self.assertTrue(result.omitted_sources)
        self.assertEqual([item["memoryId"] for item in result.bundle.dynamic_memory], ["memory-1"])
        self.assertIn("dynamicMemory", result.bundle.project("chapter"))

    def test_budget_can_omit_confirmed_recent_prose(self):
        evidence = [
            dict(make_bundle().allowed_evidence[0]),
            {
                "sourceId": "history-soft", "kind": "recent_prose", "visibility": "player_known",
                "branchId": "branch-1", "location": "history", "content": "历史承接" * 100,
                "authority": "confirmed_evidence", "validity": "confirmed",
            },
        ]
        bundle = make_bundle(evidence=evidence)
        result = apply_context_budget(bundle, context_window_tokens=260)
        self.assertIn("history-soft", result.omitted_sources)
        self.assertNotIn("history-soft", [item["sourceId"] for item in result.bundle.allowed_evidence])

    def test_chapter_budget_ignores_hidden_dynamic_memory(self):
        source = {
            "sourceId": "secret-source", "kind": "public_fact", "visibility": "author_truth",
            "branchId": "branch-1", "location": "secret", "content": "作者真相",
            "authority": "authoritative", "validity": "confirmed",
        }
        memory = {
            "memoryId": "secret-memory", "kind": "event", "sourceIds": ["secret-source"],
            "branchId": "branch-1", "visibility": "author_truth", "authority": "authoritative",
            "validity": "confirmed", "status": "confirmed", "sequence": 1,
            "content": "秘密" * 300,
        }
        bundle = ContextBundle.create(
            context_id="ctx-hidden-memory", package={"id": "pkg", "version": "0.1"},
            branch={"branchId": "branch-1"}, hard_constraints={},
            turn_intent={"rawInput": "等待"}, authoritative_state={}, state_visibility={},
            allowed_evidence=[source], dynamic_memory=[memory],
            provenance={"sourceIds": ["secret-source"]},
        )
        result = apply_context_budget(bundle, context_window_tokens=160)
        self.assertEqual(result.bundle.project("chapter")["dynamicMemory"], [])

    def test_negative_reserved_output_is_rejected(self):
        with self.assertRaises(ContextBudgetError):
            apply_context_budget(make_bundle(), context_window_tokens=1000, reserved_output_tokens=-1)

    def test_cjk_token_estimate_does_not_divide_each_character_by_four(self):
        self.assertEqual(estimate_text_tokens("中" * 40), 40)
        self.assertLess(estimate_text_tokens("a" * 40), estimate_text_tokens("中" * 40))

    def test_direct_create_validates_optional_branch_fields(self):
        with self.assertRaisesRegex(ContextBundleError, "branch.sessionId"):
            ContextBundle.create(
                context_id="ctx-invalid-branch", package={"id": "pkg", "version": "0.1"},
                branch={"branchId": "branch-1", "sessionId": 123}, hard_constraints={},
                turn_intent={"rawInput": "等待"}, authoritative_state={}, state_visibility={},
                allowed_evidence=[],
            )

    def test_projection_carries_parent_context_id(self):
        bundle = ContextBundle.create(
            context_id="ctx-child", parent_context_id="ctx-parent",
            package={"id": "pkg", "version": "0.1"}, branch={"branchId": "branch-1"},
            hard_constraints={}, turn_intent={"rawInput": "等待"}, authoritative_state={},
            state_visibility={}, allowed_evidence=[],
        )
        self.assertEqual(bundle.project("chapter")["parentContextId"], "ctx-parent")

    def test_protected_overflow_fails_without_model_call(self):
        with self.assertRaises(ContextBudgetError) as error:
            apply_context_budget(make_bundle(), context_window_tokens=5)
        self.assertEqual(error.exception.code, "context_budget_exceeded")

    def test_builder_keeps_module_context_bounded_and_state_visibility_explicit(self):
        context = {
            "package": {"id": "pkg", "version": "0.1"},
            "playerDirection": "留在原地听完回应",
            "lineage": [{"id": "node-1", "summary": "上一回合已确认木牌在手中"}],
        }
        selected = {"title": "原地等待", "summary": "留在原地听完回应", "statePatch": {"playerLocationId": "gate"}}
        state = {"playerLocationId": "gate", "authorSecret": "hidden", "branchLedger": {"entries": []}}
        module_context = {
            "world": {
                "globalConstraints": ["不得越过当前行动边界"],
                "immutableFacts": [{"id": "fact-1", "text": "木牌在门边"}],
                "futureBeat": {"id": "future-secret", "summary": "不应进入"},
            },
            "currentChapter": {"id": "chapter-1", "title": "门边"},
            "currentBeat": {"id": "beat-1", "summary": "回应正在门内传来"},
            "actionContract": {"instruction": "只听回应", "stopPoint": "原地等待"},
            "openingContext": {
                "identity": "初到山门的求访者",
                "knownFacts": ["父亲曾押送引路灯后失踪"],
                "unknownBoundaries": ["伤者身份未知"],
                "visibleItems": ["引荐文书在手中"],
            },
            "continuityContract": {"sourceCutoffLine": 61},
            "narrativeBrief": [{"evidenceParagraphId": "p-1", "text": "门内传来脚步声"}],
            "priorNarrativeBrief": [{"evidenceParagraphId": "p-0", "text": "木牌已在手中"}],
            "previousBeatSummaries": ["上一节点已确认门未打开"],
            "sourceDialogueContext": [{"dialogueParagraphId": "d-1", "paragraphs": [{"text": "等候。"}]}],
            "characters": [{"id": "character-1", "name": "门内人"}],
            "characterDetails": [{"name": "门内人", "detail": "门内人已在当前已确认剧情中出现。"}],
            "locations": [{"id": "gate", "name": "山门"}],
            "items": [{"id": "token-1", "name": "木牌"}],
            "modulePaths": ["beat-index.json", "beats/chapter-1.json"],
        }
        builder = ContextBundleBuilder()
        bundle = builder.build(
            context=context, selected=selected, state=state,
            branch={"branchId": "branch-1", "lineageHead": "node-1"},
            state_visibility={"/playerLocationId": "player_known", "/authorSecret": "author_truth"},
            module_context=module_context,
            validated_state_patch={"playerLocationId": "gate"},
        )
        second = builder.build(
            context=context, selected=selected, state=state,
            branch={"branchId": "branch-1", "lineageHead": "node-1"},
            state_visibility={"/playerLocationId": "player_known", "/authorSecret": "author_truth"},
            module_context=module_context,
            context_id="different-trace-id",
            validated_state_patch={"playerLocationId": "gate"},
        )
        self.assertEqual(bundle.context_sha256, second.context_sha256)
        self.assertEqual(bundle.turn_intent["rawInput"], "留在原地听完回应")
        self.assertNotIn("future-secret", str(bundle.allowed_evidence))
        self.assertNotIn("branchLedger", bundle.authoritative_state)
        self.assertEqual(bundle.project("chapter")["authoritativeState"], {"playerLocationId": "gate"})
        self.assertIn("伤者身份未知", bundle.hard_constraints["openingKnowledgeBoundaries"])
        self.assertEqual(bundle.hard_constraints["immutableFacts"], [
            {"id": "fact-1", "text": "木牌在门边", "authority": "hard_constraint"},
        ])
        self.assertIn("module:opening:knownFacts:0", bundle.provenance["sourceIds"])
        self.assertIn("module:currentBeat:beat-1", bundle.provenance["sourceIds"])
        self.assertEqual(bundle.provenance["stateVisibilitySource"], "explicit")
        self.assertEqual(bundle.hard_constraints["entityContext"]["characters"][0]["name"], "门内人")
        self.assertEqual(bundle.hard_constraints["entityContext"]["locations"][0]["id"], "gate")
        self.assertEqual(bundle.hard_constraints["entityContext"]["items"][0]["name"], "木牌")
        self.assertEqual(bundle.output_contract["selectedStatePatch"], {"playerLocationId": "gate"})
        self.assertEqual(
            next(item for item in bundle.allowed_evidence if item["sourceId"] == "module:currentBeat:beat-1")["branchId"],
            "package",
        )
        lineage_evidence = next(item for item in bundle.allowed_evidence if item["sourceId"] == "branch:lineage:node-1")
        self.assertEqual(lineage_evidence["branchId"], "branch-1")

    def test_builder_caps_combined_continuity_sources_to_latest_lineage_entries(self):
        window = ContextBundleBuilder._continuity_window(
            {
                "previousBeatSummaries": ["旧节点一", "旧节点二"],
                "previousBeatSummaryIds": ["old-1", "old-2"],
            },
            [
                {"id": "node-1", "summary": "当前路线一"},
                {"id": "node-2", "summary": "当前路线二"},
            ],
            "branch-1",
        )
        self.assertEqual([item["sourceId"] for item in window], [
            "branch:lineage:node-1", "branch:lineage:node-2",
        ])
        self.assertTrue(all(item["status"] == "confirmed" for item in window))
        self.assertTrue(all(item["branchId"] == "branch-1" for item in window))

    def test_builder_rejects_unvalidated_or_protected_state_patch(self):
        kwargs = dict(
            context={"package": {"id": "pkg", "version": "0.1"}, "playerDirection": "等待", "lineage": []},
            state={"playerLocationId": "gate"},
            branch={"branchId": "branch-1"},
            state_visibility={"/playerLocationId": "player_known"},
            module_context={"world": {}, "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"}},
        )
        with self.assertRaisesRegex(ContextBundleError, "未声明状态字段"):
            ContextBundleBuilder().build(
                selected={"title": "等待", "summary": "等待", "statePatch": {"secretFlag": True}},
                validated_state_patch={"secretFlag": True}, **kwargs,
            )
        with self.assertRaisesRegex(ContextBundleError, "受保护状态"):
            ContextBundleBuilder().build(
                selected={"title": "等待", "summary": "等待", "statePatch": {"freeTextProgress": 1}},
                validated_state_patch={"freeTextProgress": 1}, **kwargs,
            )
        with self.assertRaisesRegex(ContextBundleError, "validated_state_patch"):
            ContextBundleBuilder().build(
                selected={"title": "等待", "summary": "等待", "statePatch": {"playerLocationId": "gate"}}, **kwargs,
            )

    def test_default_state_visibility_is_conservative_and_projects_only_player_fields(self):
        state = {
            "playerCharacterId": "character-player",
            "playerLocationId": "location-player",
            "currentLocationId": "location-current",
            "characterLocationIds": {
                "character-player": "location-player",
                "character-other": "location-hidden",
            },
            "sourceProgress": "chapter-001",
            "itemOwnerCharacterIds": {"item-secret": "character-other"},
            "goalLedger": [{"title": "hidden"}],
        }
        visibility = ContextBundleBuilder.default_state_visibility(state)
        self.assertEqual(
            visibility,
            {
                "/playerCharacterId": "player_known",
                "/playerLocationId": "player_known",
                "/characterLocationIds/character-player": "player_known",
            },
        )
        bundle = ContextBundle.create(
            context_id="ctx-visibility",
            package={"id": "pkg", "version": "0.1"},
            branch={"branchId": "branch-1"},
            hard_constraints={},
            turn_intent={},
            authoritative_state=state,
            state_visibility=visibility,
            allowed_evidence=[],
        )
        self.assertEqual(
            bundle.project("chapter")["authoritativeState"],
            {
                "playerCharacterId": "character-player",
                "playerLocationId": "location-player",
                "characterLocationIds": {"character-player": "location-player"},
            },
        )

    def test_default_state_visibility_hides_source_current_location(self):
        state = {
            "playerCharacterId": "character-player",
            "playerLocationId": "location-player",
            "currentLocationId": "location-source",
            "characterLocationIds": {"character-player": "location-player"},
        }
        visibility = ContextBundleBuilder.default_state_visibility(state)
        self.assertNotIn("/currentLocationId", visibility)
        bundle = ContextBundle.create(
            context_id="ctx-source-location",
            package={"id": "pkg", "version": "0.1"},
            branch={"branchId": "branch-1"},
            hard_constraints={},
            turn_intent={},
            authoritative_state=state,
            state_visibility=visibility,
            allowed_evidence=[],
        )
        self.assertNotIn("currentLocationId", bundle.project("chapter")["authoritativeState"])

    def test_builder_skips_untraceable_module_evidence(self):
        bundle = ContextBundleBuilder().build(
            context={"package": {"id": "pkg", "version": "0.1"}, "playerDirection": "等待", "lineage": []},
            selected={"title": "等待", "summary": "等待"},
            state={"playerLocationId": "gate"},
            branch={"branchId": "branch-1"},
            state_visibility={"/playerLocationId": "player_known"},
            module_context={
                "world": {"immutableFacts": [{"text": "无 ID 事实"}]},
                "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前节点"},
                "narrativeBrief": [{"text": "无段落 ID 摘要"}],
                "characterIdentityEvidence": [{"name": "角色", "text": "无段落 ID 身份"}],
                "sourceDialogueContext": [{"paragraphs": [{"text": "无对白 ID"}]}],
            },
        )
        self.assertEqual([item["sourceId"] for item in bundle.allowed_evidence], ["module:currentBeat:beat-1"])

    def test_builder_rejects_duplicate_immutable_fact_ids(self):
        with self.assertRaisesRegex(ContextBundleError, "重复 id：fact-1"):
            ContextBundleBuilder().build(
                context={"package": {"id": "pkg", "version": "0.1"}, "playerDirection": "等待", "lineage": []},
                selected={"title": "等待", "summary": "等待"},
                state={"playerLocationId": "gate"},
                branch={"branchId": "branch-1"},
                state_visibility={"/playerLocationId": "player_known"},
                module_context={
                    "world": {"immutableFacts": [
                        {"id": "fact-1", "text": "门仍未开启"},
                        {"id": "fact-1", "text": "门仍未开启"},
                    ]},
                    "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"},
                },
            )

    def test_planner_records_audit_bundle_without_exposing_internal_patch(self):
        planner = LlmPlanner(object(), context_window_tokens=1000)
        context = {
            "package": {"id": "pkg", "version": "0.1"},
            "playerDirection": "继续等待",
            "lineage": [{"id": "node-1", "summary": "上一回合仍在门外等待"}],
            "parent": {"id": "node-1", "sessionId": "session-1"},
            "validatedStatePatch": {"count": 1},
            "validatedStatePatchSource": "apply_branch_patch",
        }
        selected = {
            "title": "继续等待", "summary": "继续等待",
            "statePatch": {"count": 1, "freeTextProgress": 2},
        }
        module_context = {
            "modulePaths": ["beats/chapter-1/beat-1.json"],
            "world": {"globalConstraints": [], "immutableFacts": [{"id": "fact-1", "text": "门仍未开启"}]},
            "currentChapter": {"id": "chapter-1"},
            "currentBeat": {"id": "beat-1", "summary": "门内仍无回应"},
            "actionContract": {"stopPoint": "原地等待"},
            "characters": [], "locations": [], "items": [], "characterDetails": [],
        }
        audit = planner._record_context_bundle_audit(
            context, selected, {"count": 0, "freeTextProgress": 1}, module_context,
        )
        self.assertEqual(audit["status"], "recorded")
        self.assertEqual(audit["excludedStatePatchFields"], ["freeTextProgress"])
        self.assertEqual(audit["stateVisibilityEntries"], 0)
        self.assertEqual(audit["bundle"]["contextSha256"], audit["contextSha256"])
        self.assertEqual(audit["contextBudget"]["status"], "recorded")
        self.assertTrue(audit["contextBudget"]["estimated"])
        self.assertEqual(audit["bundle"]["outputContract"]["selectedStatePatch"], {"count": 1})
        self.assertIsNotNone(planner.last_context_bundle)
        self.assertEqual(planner.last_context_bundle.output_contract["selectedStatePatch"], {"count": 1})
        planner.context_window_tokens = 1
        rejected = planner._record_context_bundle_audit(context, selected, {"count": 0, "freeTextProgress": 1}, module_context)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["contextBudget"]["code"], "context_budget_exceeded")

    def test_chapter_projection_compatibility_audit_records_field_diff_without_values(self):
        projection = {
            "contextSha256": "a" * 64,
            "hardConstraints": {"actionContract": {"instruction": "secret prompt text"}},
            "turnIntent": {
                "rawInput": "private player input",
                "selectedDirection": {"title": "private title", "summary": "private summary"},
            },
            "authoritativeState": {"playerLocationId": "private state"},
            "allowedEvidence": [{"content": "private evidence text"}],
            "styleGuide": {"sample": "private style text"},
        }
        report = LlmPlanner._chapter_projection_compatibility(
            projection,
            ["get", "profile_text", "state_json", "summary", "title"],
            LlmPlanner._legacy_prompt_projection_paths(False),
        )
        self.assertEqual(report["status"], "recorded")
        self.assertEqual(report["projectionStage"], "chapter")
        self.assertEqual(report["comparisonLevel"], "schema_path_presence_only")
        self.assertEqual(report["projectionContextSha256"], "a" * 64)
        self.assertEqual(report["legacyOnlyFields"], ["profile_text"])
        self.assertEqual(report["partialFields"], [])
        self.assertEqual(
            next(item for item in report["fieldMappings"] if item["legacyField"] == "get")["status"],
            "all_declared_paths_present",
        )
        self.assertEqual(
            next(item for item in report["fieldMappings"] if item["legacyField"] == "state_json")["status"],
            "all_declared_paths_present",
        )
        self.assertEqual(report["projectionOnlyFields"], ["allowedEvidence", "styleGuide"])
        self.assertNotIn("secret prompt text", str(report))
        self.assertNotIn("private player input", str(report))
        self.assertNotIn("private evidence text", str(report))
        self.assertNotIn("private style text", str(report))

    def test_chapter_projection_compatibility_uses_mode_specific_state_mapping(self):
        projection = {
            "contextSha256": "b" * 64,
            "authoritativeState": {"playerLocationId": "loc-1"},
            "allowedEvidence": [{"sourceId": "module:characterIdentityEvidence:e-1"}],
        }
        module_paths = LlmPlanner._legacy_prompt_projection_paths(True)
        package_paths = LlmPlanner._legacy_prompt_projection_paths(False)
        self.assertEqual(module_paths["summary"], ("turnIntent.selectedDirection.summary",))
        self.assertEqual(module_paths["title"], ("turnIntent.selectedDirection.title",))
        self.assertEqual(module_paths["state_json"], ("allowedEvidence",))
        self.assertEqual(package_paths["state_json"], ("authoritativeState",))
        report = LlmPlanner._chapter_projection_compatibility(
            projection,
            ["state_json"],
            module_paths,
        )
        mapping = report["fieldMappings"][0]
        self.assertEqual(mapping["availableProjectionPaths"], ["allowedEvidence"])
        self.assertEqual(mapping["status"], "all_declared_paths_present")

    def test_planner_records_bundle_rejection_without_changing_generation_path(self):
        planner = LlmPlanner(object())
        audit = planner._record_context_bundle_audit(
            {
                "package": {"id": "pkg", "version": "0.1"},
                "playerDirection": "等待",
                "parent": {"id": "node-1"},
                "validatedStatePatch": {"unknown": True},
                "validatedStatePatchSource": "apply_branch_patch",
            },
            {"title": "等待", "summary": "等待", "statePatch": {"unknown": True}},
            {"count": 0},
            {"world": {}, "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"}},
        )
        self.assertEqual(audit["status"], "rejected")
        self.assertIn("未声明状态字段", audit["error"])
        self.assertIsNone(planner.last_context_bundle)

    def test_planner_rejects_bundle_without_state_layer_validation_marker(self):
        planner = LlmPlanner(object())
        audit = planner._record_context_bundle_audit(
            {
                "package": {"id": "pkg", "version": "0.1"},
                "playerDirection": "等待",
                "parent": {"id": "node-1"},
            },
            {"title": "等待", "summary": "等待", "statePatch": {"count": 1}},
            {"count": 0},
            {"world": {}, "currentChapter": {}, "currentBeat": {"id": "beat-1", "summary": "当前"}},
        )
        self.assertEqual(audit["status"], "rejected")
        self.assertIn("状态层验证标记", audit["error"])

    def test_planner_uses_conservative_default_state_visibility(self):
        planner = LlmPlanner(object())
        context = {
            "package": {"id": "pkg", "version": "0.1"},
            "playerDirection": "继续等待",
            "lineage": [],
            "parent": {"id": "node-1"},
            "validatedStatePatch": {"playerLocationId": "gate"},
            "validatedStatePatchSource": "apply_branch_patch",
        }
        selected = {
            "title": "继续等待", "summary": "继续等待",
            "statePatch": {"playerLocationId": "gate"},
        }
        state = {
            "playerCharacterId": "character-player",
            "playerLocationId": "gate",
            "characterLocationIds": {
                "character-player": "gate",
                "character-other": "secret",
            },
            "sourceProgress": "chapter-001",
        }
        module_context = {
            "modulePaths": ["beats/chapter-1/beat-1.json"],
            "world": {}, "currentChapter": {},
            "currentBeat": {"id": "beat-1", "summary": "当前"},
        }
        audit = planner._record_context_bundle_audit(context, selected, state, module_context)
        self.assertEqual(audit["status"], "recorded")
        self.assertEqual(audit["stateVisibilityEntries"], 3)
        visibility = audit["bundle"]["stateVisibility"]
        self.assertIn("/playerCharacterId", visibility)
        self.assertIn("/playerLocationId", visibility)
        self.assertIn("/characterLocationIds/character-player", visibility)
        self.assertNotIn("/sourceProgress", visibility)
        self.assertNotIn("/characterLocationIds/character-other", visibility)
        self.assertEqual(audit["bundle"]["provenance"]["stateVisibilitySource"], "runtime_default_migration")


if __name__ == "__main__":
    unittest.main()
