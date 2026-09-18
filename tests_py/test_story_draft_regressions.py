"""Regressions for the rejected 0.1.16 manual playthrough."""

import copy
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from open_story_engine.cocreation import (
    CoCreationService, LlmPlanner, MockPlanner, apply_branch_patch, entry_initial_state,
    guard_narrative, guard_offstage_reports, guard_repeated_paragraphs, trim_repeated_tail, initial_branch_state, state_character_locations,
)
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import Completion, LlmError, OpenAICompatibleGateway, parse_json_content
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore


def review_fixture(narrative, quote=None, reason=None):
    checks = []
    for index, paragraph in enumerate(re.split(r"\n\s*\n", narrative), start=1):
        conflict = quote is not None and quote.strip('“”') in paragraph
        checks.append({"paragraphId": index, "status": "conflict" if conflict else "non_factual",
                       "evidenceIds": [], "quote": quote if conflict else "",
                       "reason": reason if conflict else "普通场景动作，无新增持久事实"})
        if conflict:
            checks[-1]["quotes"] = [quote]
    return json.dumps({"checks": checks}, ensure_ascii=False)


class RecordedGateway:
    model = "regression-fixture"

    def __init__(self, responses, streamed=False, support_responses=None):
        self.responses = iter(responses)
        self.messages = []
        self.streamed = streamed
        # Separate journals keep existing tests focused on generation/repair;
        # tests of the independent reviewer supply its explicit responses.
        self.support_messages = []
        self.support_responses = iter(support_responses) if support_responses is not None else None

    def complete_text(self, messages, on_delta=None, on_reset=None):
        self.messages.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        visible = self.streamed and on_delta is not None
        if visible:
            on_delta(response)
        return Completion(response, response, [{"outcome": "completed"}], body_was_streamed=visible)

    def complete_json(self, messages):
        data = json.loads(messages[1]["content"])
        if data.get("task") == "verify_fact_support":
            self.support_messages.append(messages)
            response = (next(self.support_responses) if self.support_responses is not None else
                        json.dumps({"checks": [{"paragraphId": p["id"], "status": "allowed"} for p in data["paragraphs"]]}))
            if isinstance(response, Exception):
                raise response
            return Completion(response, response, [{"outcome": "completed"}])
        return self.complete_text(messages)


class StoryDraftRegressions(unittest.TestCase):
    def test_over_budget_repair_text_stops_before_model_calls(self):
        narrative = "错误事实。" * 80 + "\n\n" + "雨落在玻璃上。" * 10
        conflict = {"paragraphId": 1, "quotes": ["错误事实"], "reason": "无依据的事实"}
        for suggestions in (None, [{"paragraphId": 1, "text": "许川没有回答。"}]):
            with self.subTest(local_suggestions=suggestions is not None):
                gateway = RecordedGateway([])
                planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
                with self.assertRaisesRegex(LlmError, "事实修订超出局部修改范围"):
                    planner.plan(self.context, self.selected, self.resolved,
                                 repair="剧情正文事实核对未通过", repair_narrative=narrative,
                                 repair_conflicts=[conflict], repair_replacements=suggestions)
                self.assertEqual(gateway.messages, [])
                self.assertEqual(gateway.support_messages, [])

    def test_repair_text_budget_preserves_exact_boundary_and_short_draft_floor(self):
        for original_size, unchanged_size in ((200, 10), (300, 298)):
            with self.subTest(original_size=original_size):
                narrative = "甲" * original_size + "\n\n" + "乙" * unchanged_size
                replacement = json.dumps({"replacements": [{"paragraphId": 1, "text": "修订。"}]})
                LlmPlanner._check_fact_repair_size(narrative, [1])
                self.assertEqual(LlmPlanner._apply_fact_replacements(narrative, replacement),
                                 "修订。\n\n" + "乙" * unchanged_size)
                for check in (lambda: LlmPlanner._check_fact_repair_size("甲" + narrative, [1]),
                              lambda: LlmPlanner._apply_fact_replacements("甲" + narrative, replacement)):
                    with self.assertRaisesRegex(LlmError, "事实修订超出局部修改范围"):
                        check()

    def test_over_budget_conflict_count_stops_before_requesting_an_editor(self):
        paragraphs = [f"许川想起第 {index} 次巡查，记录证明全部设备正常。" for index in range(9)]
        narrative = "\n\n".join(paragraphs)
        review = {"checks": [{"paragraphId": index + 1, "status": "conflict", "evidenceIds": [],
                              "quotes": [text], "reason": "未登记的巡查往事"}
                             for index, text in enumerate(paragraphs)]}
        gateway = RecordedGateway([narrative, json.dumps(review)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "超过 8 处局部修改上限；未请求"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(len(gateway.messages), 2)
        self.assertEqual(shown, [])

    def test_explicit_source_attribution_cannot_pass_on_quote_content_alone(self):
        paragraph = '“西边消防通道的锁坏了。”许川说，“你刚才说的。”'
        narrative = '“你去哪？”陈砚问。\n\n' + paragraph + '\n\n陈砚没有拦他。'
        evidence = [{"id": "e1", "kind": "sourceDialogueContext", "value": "姜序说：“西边消防通道的锁坏了。”"}]
        records = [{"paragraphId": p["id"], "status": "non_factual", "evidenceIds": [], "reason": "普通描写"}
                   for p in LlmPlanner._draft_paragraphs(narrative)]
        check = records[1]
        check.update(status="supported", evidenceIds=["e1"], supports=[
            {"claim": "西边消防通道的锁坏了", "evidenceId": "e1", "evidenceQuote": "西边消防通道的锁坏了"}])
        def validate():
            LlmPlanner._check_fact_review(json.dumps({"checks": records}), narrative, {"e1"}, evidence)
        self.assertEqual(LlmPlanner._attribution_claims(paragraph), ["你刚才说的"])
        with self.assertRaisesRegex(LlmError, "缺少完整 sourceAttributions"):
            validate()
        check["sourceAttributions"] = [{"claim": "你刚才说的", "claimedSource": "陈砚", "sourceSpeaker": "姜序", "evidenceIds": ["e1"]}]
        with self.assertRaisesRegex(LlmError, "来源不一致"):
            validate()
        check["sourceAttributions"][0]["claimedSource"] = "姜序"
        with self.assertRaisesRegex(LlmError, "姓名或其引用无法定位"):
            validate()
        narrative = narrative.replace("陈砚问", "姜序问")
        validate()
        check["sourceAttributions"][0]["evidenceIds"] = ["e404"]
        with self.assertRaises(LlmError):
            validate()
        for ordinary in ('“西边消防通道的锁坏了。”许川说。', '“你刚才说的是什么？”许川问。'):
            self.assertEqual(LlmPlanner._attribution_claims(ordinary), [])

    def test_dialogue_context_keeps_adjacent_source_and_phase_without_guessing_speaker(self):
        cues = [
            {"text": "陈砚朝这边走来。姜序向后退了半步。", "evidenceParagraphId": "paragraph-045", "lineRange": {"start": 93, "end": 93}},
            {"text": "“西边消防通道的锁坏了。”他说。", "evidenceParagraphId": "paragraph-046", "lineRange": {"start": 95, "end": 95}},
            {"text": "许川知道姜序愿意帮忙。", "evidenceParagraphId": "paragraph-047", "lineRange": {"start": 97, "end": 97}},
        ]
        snapshot = copy.deepcopy(cues)
        passage = ModuleContextResolver._dialogue_context(cues, 93)[0]
        self.assertEqual([p["text"] for p in passage["paragraphs"]], [p["text"] for p in cues])
        self.assertEqual([p["phase"] for p in passage["paragraphs"]], ["prior", "current", "current"])
        self.assertNotIn("speaker", str(passage))
        self.assertEqual(cues, snapshot)
        self.assertEqual(ModuleContextResolver._dialogue_context([cues[1]], 93), [])
        gap = {**cues[0], "evidenceParagraphId": "paragraph-043"}
        self.assertEqual(ModuleContextResolver._dialogue_context([gap, cues[1]], 93), [])
        names = ["陈砚", "姜序", "许川"]
        resolved = ModuleContextResolver._dialogue_context(cues, 93, names)[0]
        self.assertEqual(resolved["speakerName"], "姜序")
        self.assertEqual(resolved["speakerBasis"]["text"], "姜序向后退了半步。")
        for text in ("陈砚看着姜序。", "姜序的通行证落在地上。", "姜序手里的通行证湿了。", "“姜序在这里。”陈砚说。"):
            ambiguous = [{**cues[0], "text": text}, *cues[1:]]
            self.assertNotIn("speakerName", ModuleContextResolver._dialogue_context(ambiguous, 93, names)[0])
        switched = [cues[0], {**cues[1], "text": "陈砚走过来。他说：“通道锁坏了。”"}, cues[2]]
        self.assertNotIn("speakerName", ModuleContextResolver._dialogue_context(switched, 93, names)[0])

    def test_branch_excerpt_preserves_complete_paragraphs_and_deduplicates_macro(self):
        old = "许川问：“" + "旧事" * 700 + "”"
        recent = "姜序后退半步。\n\n“别从正门进去。”他说。"
        self.assertEqual(ModuleContextResolver._branch_excerpt(old + "\n\n" + recent), recent)
        self.assertEqual(ModuleContextResolver._branch_excerpt(old), "")
        context = {**self.context, "lineage": [{"narrativeText": old + "\n\n" + recent}] * 2}
        scope = ModuleContextResolver.for_package(self.path, self.package).resolve(context, self.selected, self.resolved)
        self.assertEqual(scope["continuityText"].count(recent), 1)
        self.assertNotIn("旧事", scope["continuityText"])

    def test_compact_review_derives_evidence_ids_without_trusting_unresolved_refs(self):
        text = "电话没有接通。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": text}]
        support = {"claimRef": LlmPlanner._referenced_fact_spans(text, "p1")[0]["ref"],
                   "evidenceRef": LlmPlanner._referenced_fact_spans(text, "e1")[0]["ref"]}
        payload = {"checks": [{"paragraphId": 1, "status": "supported", "supports": [support, support]}]}
        normalized = LlmPlanner._normalize_fact_review(json.dumps(payload))
        self.assertEqual(json.loads(normalized)["checks"][0]["evidenceIds"], ["e1"])
        LlmPlanner._check_fact_review(normalized, text, {"e1"}, evidence)
        payload["checks"][0]["supports"][0] = {**support, "evidenceRef": "e1:made-up"}
        with self.assertRaisesRegex(LlmError, "ref 不存在"):
            LlmPlanner._check_fact_review(LlmPlanner._normalize_fact_review(json.dumps(payload)), text, {"e1"}, evidence)
        conflict = {"checks": [{"paragraphId": 1, "status": "conflict", "quotes": [text]}]}
        with self.assertRaises(LlmError):
            LlmPlanner._check_fact_review(LlmPlanner._normalize_fact_review(json.dumps(conflict)), text, {"e1"}, evidence)

    def test_truncated_review_retries_records_once_and_keeps_prose(self):
        loop = '{"checks":[{"paragraphId":1,"evidenceIds":[' + '"e57",' * 200
        compact = json.dumps({"checks": [{"paragraphId": 1, "status": "non_factual"}]})
        gateway = RecordedGateway([self.prefix, loop, compact])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        result, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [self.prefix])
        self.assertEqual(result["narrativeText"], self.prefix)
        self.assertIn("fact_review_format_repair", [c.get("generationStage") for c in audit["callObservations"]])
        self.assertNotIn(loop, str(gateway.messages[2]))
        gateway = RecordedGateway([self.prefix, loop, loop])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        with self.assertRaisesRegex(LlmError, "最终 JSON"):
            planner.plan(self.context, self.selected, self.resolved)
        self.assertEqual(len(gateway.messages), 3)

    def test_identity_reveal_keeps_source_context_without_future_or_extra_reads(self):
        reveal = {"text": "通行证上写着：姜序，夜班维修。", "lineRange": {"start": 39, "end": 39}}
        characters = [{"name": "姜序", "sourceDescriptionEvidence": [reveal]}]
        cues = [{"text": "一个穿旧雨衣的男人抬起头。他攥着通行证。" + reveal["text"], "lineRange": {"start": 39, "end": 39}}]
        snapshot = copy.deepcopy((characters, cues))
        self.assertEqual(ModuleContextResolver._identity_evidence(characters, cues, 38), [])
        evidence = ModuleContextResolver._identity_evidence(characters, cues, 39)
        self.assertEqual(evidence, [{"name": "姜序", **cues[0]}])
        self.assertEqual((characters, cues), snapshot)
        truncated = [{**cues[0], "text": "一个穿旧雨衣的男人抬起头。他攥着通行证。"}]
        self.assertEqual(ModuleContextResolver._identity_evidence(characters, truncated, 39),
                         [{"name": "姜序", **reveal, "context": truncated[0]["text"]}])
        adjacent = [{**truncated[0], "lineRange": {"start": 38, "end": 38}}]
        self.assertEqual(ModuleContextResolver._identity_evidence(characters, adjacent, 39), [{"name": "姜序", **reveal}])
        future = [{**cues[0], "lineRange": {"start": 39, "end": 100}}]
        self.assertEqual(ModuleContextResolver._identity_evidence(characters, future, 39), [{"name": "姜序", **reveal}])

    def test_past_action_requires_its_own_evidence_even_inside_a_question(self):
        text = '“你让我签的时候，说泵坏了只是小事。”'
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "陈砚说泵坏了只是小事。"}]
        self.assertEqual(LlmPlanner._past_claims(text), ["你让我签的时候"])
        self.assertEqual(LlmPlanner._past_claims("你让我签的时候，唐栖在哪？"), ["你让我签的时候"])
        for hypothetical in ("如果你让我签的时候出了问题呢？", "你让我签字吗？"):
            self.assertEqual(LlmPlanner._past_claims(hypothetical), [])
        def review():
            return LlmPlanner._normalize_fact_review(json.dumps({"checks": [{"paragraphId": 1, "status": "supported", "supports": [
                {"claimRef": LlmPlanner._referenced_fact_spans(text, "p1")[0]["ref"],
                 "evidenceRef": LlmPlanner._referenced_fact_spans(evidence[0]["value"], "e1")[0]["ref"]}]}]}))
        with self.assertRaisesRegex(LlmError, "往事断言缺少"):
            LlmPlanner._check_fact_review(review(), text, {"e1"}, evidence)
        with self.assertRaisesRegex(LlmError, "不能标为 non_factual"):
            LlmPlanner._check_fact_review(review_fixture(text), text, {"e1"}, evidence)
        evidence[0]["value"] = text
        LlmPlanner._check_fact_review(review(), text, {"e1"}, evidence)

    def test_opaque_support_refs_preserve_exact_sources_and_reject_wrong_scope(self):
        text = '许川拨打电话没有接通。屏幕上亮着红色的“未接通”。'
        evidence = [{"id": "e7", "kind": "sourceSceneCues", "value": {"许川": "电话没有接通。"}}]
        claims = LlmPlanner._referenced_fact_spans(text, "p1")
        source = LlmPlanner._referenced_fact_spans(evidence[0]["value"], "e7")[0]
        support = {"claimRef": claims[0]["ref"], "evidenceRef": source["ref"]}
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e7"], "reason": "未接通结果的表现",
                 "supports": [support]}
        LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, {"e7"}, evidence)
        for field, value in (("claimRef", True), ("claimRef", []), ("claimRef", "p1:3"),
                             ("claimRef", LlmPlanner._referenced_fact_spans(text, "p2")[0]["ref"]),
                             ("evidenceRef", source["ref"].replace("e7:", "e8:")),
                             ("evidenceRef", LlmPlanner._referenced_fact_spans({"姜序": "电话没有接通。"}, "e7")[0]["ref"]),
                             ("evidenceRef", LlmPlanner._referenced_fact_spans({"许川": "电话已经接通。"}, "e7")[0]["ref"]),
                             ("claimId", 1)):
            broken = copy.deepcopy(check)
            broken["supports"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(LlmError):
                LlmPlanner._check_fact_review(json.dumps({"checks": [broken]}), text, {"e7"}, evidence)
        repeated = LlmPlanner._referenced_fact_spans("雨声不停。雨声不停。", "p1")
        self.assertNotEqual(repeated[0]["ref"], repeated[1]["ref"])
        self.assertEqual(source["path"], ["许川"])

    def test_explicit_support_verdict_distinguishes_allowed_prose_from_conflicts(self):
        text = '红色的“未接通”三个字亮在屏幕上。\n\n“我也试过。”陈砚说。'
        initial = review_fixture(text)
        allowed = {"paragraphId": 1, "status": "allowed"}
        conflict = {"paragraphId": 2, "status": "conflict", "quotes": ["我也试过"],
                    "reason": "没有陈砚曾拨号的依据", "correctedText": "陈砚没有回答。"}
        with self.assertRaises(ValueError) as caught:
            LlmPlanner._check_fact_support_review(json.dumps({"checks": [allowed, conflict]}), initial, text, [])
        self.assertEqual([c["paragraphId"] for c in caught.exception.fact_conflicts], [2])
        for checks in ([allowed], [allowed, allowed], [{**allowed, "paragraphId": True}, conflict],
                       [allowed, {**conflict, "status": "allowed"}], [allowed, {**conflict, "status": "unknown"}]):
            with self.subTest(checks=checks), self.assertRaises(LlmError):
                LlmPlanner._check_fact_support_review(json.dumps({"checks": checks}), initial, text, [])

    def test_conditional_equipment_restriction_cannot_hide_behind_punctuation(self):
        text = '“信号恢复之前，谁都不能进设备间。”他说。'
        self.assertEqual(LlmPlanner._rule_claims(text), ["信号恢复之前，谁都不能进设备间"])
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "设备间进水，里面危险。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "规则",
                 "supports": [{"claimRef": LlmPlanner._referenced_fact_spans(text, "p1")[0]["ref"],
                               "evidenceRef": LlmPlanner._referenced_fact_spans(evidence[0]["value"], "e1")[0]["ref"]}]}
        with self.assertRaisesRegex(LlmError, "缺少对应原句依据"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, {"e1"}, evidence)
        check["status"] = "non_factual"
        with self.assertRaisesRegex(LlmError, "不能标为 non_factual"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, {"e1"}, evidence)
        for hypothetical in ('“信号恢复之前，谁都不能进设备间吗？”', '也许信号恢复之前，谁都不能进设备间。'):
            self.assertEqual(LlmPlanner._rule_claims(hypothetical), [])

    def test_opaque_refs_do_not_weaken_rule_or_declared_evidence_checks(self):
        text = "应急线路只允许占用到零点。"
        evidence = [{"id": "e7", "kind": "sourceSceneCues", "value": "零点前必须恢复放行。"}]
        def review():
            return json.dumps({"checks": [{"paragraphId": 1, "status": "supported", "evidenceIds": ["e7"],
                "reason": "规则", "supports": [{"claimRef": LlmPlanner._referenced_fact_spans(text, "p1")[0]["ref"],
                "evidenceRef": LlmPlanner._referenced_fact_spans(evidence[0]["value"], "e7")[0]["ref"]}]}]})
        with self.assertRaisesRegex(LlmError, "缺少对应原句依据"):
            LlmPlanner._check_fact_review(review(), text, {"e7"}, evidence)
        evidence[0]["value"] = text
        LlmPlanner._check_fact_review(review(), text, {"e7"}, evidence)
        broken = json.loads(review())
        broken["checks"][0]["evidenceIds"] = ["e8"]
        with self.assertRaisesRegex(LlmError, "指定证据 ID"):
            LlmPlanner._check_fact_review(json.dumps(broken), text, {"e7", "e8"}, evidence)

    def test_independent_support_review_catches_unsupported_second_clause_and_repairs_once(self):
        original = '陈砚说：“她的电话打不通，我也试过。”\n\n许川看着地面。\n\n雨声不停。'
        corrected = original.replace('，我也试过', '')
        conflict = {"checkedParagraphIds": [1, 2, 3], "conflicts": [
            {"paragraphId": 1, "quotes": ["我也试过"], "reason": "没有陈砚曾拨号的依据",
             "correctedText": '陈砚说：“她的电话打不通。”'}]}
        clean = {"checkedParagraphIds": [1, 2, 3], "conflicts": []}
        gateway = RecordedGateway([original, review_fixture(original), review_fixture(corrected)],
                                  support_responses=[json.dumps(conflict), json.dumps(clean)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        result, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(result["narrativeText"], corrected)
        self.assertEqual(shown, [corrected])
        self.assertEqual(len(gateway.support_messages), 2)
        stages = [x.get("generationStage") for x in audit["callObservations"]]
        self.assertIn("fact_support_review", stages)
        self.assertIn("semantic_repair_fact_support_review", stages)
        second_input = json.loads(gateway.support_messages[1][1]["content"])
        self.assertNotIn("我也试过", json.dumps(second_input, ensure_ascii=False))
        for paragraph in second_input["paragraphs"]:
            self.assertNotIn("status", paragraph)
            self.assertNotIn("reason", paragraph)

    def test_support_review_requires_complete_coverage_and_locatable_conflicts(self):
        text = "电话打不通。\n\n雨声不停。"
        initial = review_fixture(text)
        for response in ({"checkedParagraphIds": [1], "conflicts": []},
                         {"checkedParagraphIds": [1, 1], "conflicts": []},
                         {"checkedParagraphIds": [True, 2], "conflicts": []},
                         {"checkedParagraphIds": [1, 2], "conflicts": {}},
                         {"checkedParagraphIds": [1, 2], "conflicts": [{"paragraphId": 3}]},
                         {"checkedParagraphIds": [1, 2], "conflicts": [{"paragraphId": 1, "quotes": ["我也试过"], "reason": "不存在的引文"}]}):
            with self.subTest(response=response), self.assertRaises(LlmError):
                LlmPlanner._check_fact_support_review(json.dumps(response), initial, text, [])
        LlmPlanner._check_fact_support_review(json.dumps({"checkedParagraphIds": [2, 1], "conflicts": []}), initial, text, [])

    def test_support_review_cannot_start_a_second_semantic_repair(self):
        original = '陈砚说：“我也试过。”\n\n许川看着地面。\n\n雨声不停。'
        corrected = original.replace('我也试过', '雨停之前，信号不会恢复')
        reviews = [
            {"checkedParagraphIds": [1, 2, 3], "conflicts": [{"paragraphId": 1, "quotes": ["我也试过"],
                "reason": "没有此前拨号的依据", "correctedText": corrected.split('\n\n')[0]}]},
            {"checkedParagraphIds": [1, 2, 3], "conflicts": [{"paragraphId": 1, "quotes": ["雨停之前，信号不会恢复"],
                "reason": "新增通信恢复条件", "correctedText": '陈砚说：“信号还没恢复。”'}]},
        ]
        gateway = RecordedGateway([original, review_fixture(original), review_fixture(corrected)],
                                  support_responses=[json.dumps(r) for r in reviews])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "新增通信恢复条件"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.support_messages), 2)

    def test_support_review_timeout_never_exposes_a_confirmable_draft(self):
        gateway = RecordedGateway([self.prefix, review_fixture(self.prefix)],
                                  support_responses=[LlmError("support review timeout", "transport_error")])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "support review timeout"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.support_messages), 1)

    def test_quoted_known_dialogue_does_not_make_an_object_a_new_speaker(self):
        text = '陈砚面对一个陌生人提起唐栖，第一反应不该是“她情绪不稳定”这么简单。'
        scope = {"narrativeBrief": [], "world": {"immutableFacts": []}}
        guard_offstage_reports(text, scope)
        for bad in ('一个陌生人站在柱子边。他低声说：“线路已经恢复。”',
                    '许川看向柱子。那个陌生人抬起头：“线路已经恢复。”',
                    '许川面对一个陌生人，陌生人告诉他线路已经恢复。'):
            with self.subTest(text=bad), self.assertRaisesRegex(ValueError, "未登记的信息来源"):
                guard_offstage_reports(bad, scope)

    def test_numbered_support_uses_script_sentences_and_rejects_invalid_references(self):
        narrative = '“零点前必须恢复放行。”他说，声音低了一些，“线路不能一直占着。”'
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "零点前必须恢复放行。线路不能一直占着。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "已知放行要求",
                 "supports": [{"claimId": 1, "evidenceId": "e1", "evidenceSpanId": 1},
                              {"claimId": 2, "evidenceId": "e1", "evidenceSpanId": 2}]}
        LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        for field, value in (("claimId", True), ("claimId", 0), ("claimId", 99), ("claimId", "1"),
                             ("evidenceSpanId", False), ("evidenceSpanId", -1), ("evidenceSpanId", 99),
                             ("evidenceId", "e2"), ("claim", "另一段的句子")):
            invalid = copy.deepcopy(check)
            invalid["supports"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(LlmError):
                LlmPlanner._check_fact_review(json.dumps({"checks": [invalid]}), narrative, {"e1"}, evidence)

    def test_numbered_support_cannot_bypass_equipment_rule_evidence(self):
        narrative = "应急线路只允许占用到零点。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "零点前必须恢复放行。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "规则",
                 "supports": [{"claimId": 1, "evidenceId": "e1", "evidenceSpanId": 1}]}
        with self.assertRaisesRegex(LlmError, "缺少对应原句依据"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        evidence[0]["value"] = narrative
        LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)

    def test_numbered_support_retry_names_the_invalid_reference_and_available_ids(self):
        text = "“零点前必须恢复放行。”他说。"
        evidence = [{"id": "e7", "kind": "sourceSceneCues", "value": "零点前必须恢复放行。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e7"], "reason": "已知要求",
                 "supports": [{"claimId": 4, "evidenceId": "e7", "evidenceSpanId": 3}]}
        with self.assertRaises(LlmError) as caught:
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, {"e7"}, evidence)
        self.assertEqual(caught.exception.invalid_paragraph_ids, [1])
        for diagnostic in ("claimId=4", "当前段可选 id 为 [1, 2]", "evidenceId=e7", "evidenceSpanId=3", "该证据可选 id 为 [1]"):
            self.assertIn(diagnostic, str(caught.exception))

    def test_evidence_spans_preserve_structured_field_ownership_and_negation(self):
        evidence = {"许川": {"location": "候车厅。", "knows": ["没有恢复通信。", "零点前必须放行。"]},
                    "姜序": {"location": "站务室。"}}
        spans = LlmPlanner._fact_spans(evidence)
        self.assertEqual([s["id"] for s in spans], [1, 2, 3, 4])
        self.assertEqual(spans[1], {"id": 2, "path": ["许川", "knows", 0], "text": "没有恢复通信。"})
        self.assertEqual(spans[-1]["path"], ["姜序", "location"])

    def test_supported_review_requires_locatable_claim_and_source(self):
        narrative = "陈砚要求零点前放行。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "零点前必须恢复放行。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "已知要求"}
        with self.assertRaisesRegex(LlmError, "supports"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        support = {"claim": narrative, "evidenceId": "e1", "evidenceQuote": "零点前必须恢复放行"}
        check["supports"] = [support]
        LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        punctuated = copy.deepcopy(check)
        punctuated["supports"][0].update(claim="“陈砚要求零点前放行”", evidenceQuote="“零点前必须恢复放行。”")
        LlmPlanner._check_fact_review(json.dumps({"checks": [punctuated]}), narrative, {"e1"}, evidence)
        for field, value in (("claim", "线路共用"), ("evidenceQuote", "应急线路只允许占用到零点"),
                             ("claim", "陈砚不要求零点前放行"), ("evidenceQuote", "零点前不必恢复放行"),
                             ("evidenceId", "e2"), ("evidenceQuote", "")):
            broken = copy.deepcopy(check)
            broken["supports"][0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(LlmError, "无法定位"):
                LlmPlanner._check_fact_review(json.dumps({"checks": [broken]}), narrative, {"e1"}, evidence)

    def test_expansion_echo_is_trimmed_without_rewriting_original(self):
        ending = "电子钟的红色数字又暗了一下，又亮回来。"
        original = "他没有回答。\n \n雨声没有停。" + ending
        added = "湿冷的空气贴着他的脸颊，他仍觉得喉咙发紧。"
        observations = []
        response = json.dumps({"expansion": {"paragraphId": 2, "text": added + ending}})
        result = LlmPlanner._apply_scene_expansion(original, response, observations)
        self.assertEqual(result, "他没有回答。\n \n雨声没有停。" + added + ending)
        self.assertEqual(observations[0]["normalization"], "removed_expansion_ending_echo")
        for text in (ending, "雨声没有停。" + ending):
            with self.assertRaisesRegex(LlmError, "没有新增内容"):
                LlmPlanner._apply_scene_expansion(original, json.dumps({"expansion": {"paragraphId": 2, "text": text}}))

    def test_support_can_end_at_a_literal_speech_attribution(self):
        text = '“十七号柜的牌。”许川说，“姜序说它在站务室里。”'
        for quote in ('“十七号柜的牌。”许川说。', '十七号柜的牌，姜序说它在站务室里'):
            self.assertTrue(LlmPlanner._fact_quote_is_contained(quote, text))
        for quote in ('“十七号柜的牌。”陈砚说。', '十七号柜的牌不在站务室里', ''):
            self.assertFalse(LlmPlanner._fact_quote_is_contained(quote, text))

    def test_quote_array_alias_preserves_rejection_and_location_checks(self):
        text = '应急线路只允许占用到零点。'
        check = {"paragraphId": 1, "status": "conflict", "evidenceIds": [],
                 "quote": [text], "reason": "无依据的设备规则", "correctedText": "他没有回答。"}
        with self.assertRaisesRegex(ValueError, "事实核对未通过") as caught:
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, set())
        self.assertEqual(caught.exception.fact_conflicts[0]["claims"], [text[:-1]])
        check["quote"] = ["正文不存在的句子"]
        with self.assertRaisesRegex(LlmError, "可定位"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), text, set())

    def test_rule_claims_cannot_hide_behind_supported_neighbor(self):
        narrative = "雨声不停。站里所有对外通信都走同一条应急线路。应急线路只允许占用到零点。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "雨声不停。零点前必须恢复放行。线路不能一直占着。"}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "已知环境",
                 "supports": [{"claim": "雨声不停。", "evidenceId": "e1", "evidenceQuote": "雨声不停。"}]}
        with self.assertRaisesRegex(LlmError, "遗漏设备或通信规则"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        check["status"] = "non_factual"
        with self.assertRaisesRegex(LlmError, "不能标为 non_factual"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        self.assertEqual(len(LlmPlanner._rule_claims(narrative)), 2)
        for text in ("应急线路只能用到零点吗？", "如果通信都走同一条线路，他该怎么办。", "他只能等着。"):
            self.assertEqual(LlmPlanner._rule_claims(text), [])

    def test_real_but_unrelated_source_does_not_establish_equipment_rule(self):
        narrative = "应急线路只允许占用到零点。"
        source = "零点前必须恢复放行。线路不能一直占着。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": source}]
        check = {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "reason": "通信规则",
                 "supports": [{"claim": narrative, "evidenceId": "e1", "evidenceQuote": source}]}
        with self.assertRaisesRegex(LlmError, "缺少对应原句依据"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        evidence[0]["value"] = narrative
        check["supports"][0]["evidenceQuote"] = narrative
        LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)
        evidence[0]["value"] = "应急线路并非只允许占用到零点。"
        check["supports"][0]["evidenceQuote"] = evidence[0]["value"]
        with self.assertRaisesRegex(LlmError, "缺少对应原句依据"):
            LlmPlanner._check_fact_review(json.dumps({"checks": [check]}), narrative, {"e1"}, evidence)

    def test_json_examples_cannot_replace_the_final_response(self):
        example = '{"replacements":[{"paragraphId":2,"text":"..."}]}'
        final = '{"replacements":[{"paragraphId":2,"text":"他没有回答。"}]}'
        self.assertEqual(parse_json_content("示例：" + example + "\n最终结果：" + final), json.loads(final))
        self.assertEqual(parse_json_content("<think>示例：" + example + "</think>```json\n" + final + "\n```"), json.loads(final))
        for text in ("示例：" + example + '\n最终结果：{"replacements":',
                     "<think>示例：" + example, "<think>" + example + "</think>"):
            with self.subTest(text=text), self.assertRaises(LlmError):
                parse_json_content(text)

    def setUp(self):
        self.path = Path(__file__).resolve().parents[1] / "tests_py/fixtures/content/packages/rainy-waiting-room-source/0.1.16/package.json"
        self.package = load_runtime_story_package(self.path, lazy=True)
        self.state = initial_branch_state(self.package["story"]["narrativeGraph"]["beats"][0])
        self.selected = self.package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
        self.resolved = {**self.state, **self.selected["statePatch"]}
        self.context = {
            "package": self.package, "parent": {"id": "branch_fixture", "branchState": self.state, "summary": "等待联络"},
            "characterDetails": [],
        }
        self.prefix = "许川站在候车厅中央，听着雨声。"
        self.suffix = "陈砚按住对讲机，要求零点前恢复放行。" + "雨水打在玻璃上，灯光依旧亮着。" * 8

    def test_message_quotes_reject_replacement_and_insertions_but_allow_excerpt(self):
        invalid = [
            "唐栖的语音响起：\n\n“末班车进站了，但车门没开。候车厅里只有我一个人。”",
            "唐栖的声音在语音里响起：“临潮站，末班车，别让陈砚拿到储物柜里的录音。隧道里还有人。”",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "通信状态"):
                guard_narrative(text, self.state, [], package=self.package)
        valid = [
            "许川又听了一遍唐栖的语音：“别让陈砚拿到储物柜里的录音。隧道里还有人。”",
            "许川想起唐栖最后一条语音：“隧道里还有人。”",
            "许川关掉唐栖的语音。\n\n陈砚走过来，问：“找人？”",
            "许川想起唐栖最后一条语音的时间。他看了一眼手机，电话一直无法接通。\n\n“你听到的声音，是男是女？”许川问。",
        ]
        for text in valid:
            with self.subTest(text=text):
                guard_narrative(text, self.state, [], package=self.package)

    def test_position_tracks_unambiguous_pronoun_and_allows_return(self):
        bad = "许川收起手机。他沿着站台走了一段，停在列车旁。"
        with self.assertRaisesRegex(ValueError, "最终位置应为 候车厅"):
            guard_narrative(bad, self.state, [], package=self.package)
        guard_narrative(bad + "许川回到候车厅。", self.state, [], package=self.package)
        guard_narrative("许川看向站台，仍站在候车厅里。", self.state, [], package=self.package)

    def test_entry_through_named_doorway_updates_last_explicit_position(self):
        state = copy.deepcopy(self.state)
        state["playerLocationId"] = next(item["id"] for item in self.package["locations"] if item["name"] == "消防通道")
        prefix = "许川站在候车厅。许川站在消防通道门口。"
        guard_narrative(prefix + "他转过身，侧身挤进门缝。他回头看了一眼候车厅的灯光。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "最终位置应为 消防通道"):
            guard_narrative(prefix + "他没有挤进门缝。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "最终位置应为 消防通道"):
            guard_narrative("许川站在候车厅。许川站在消防通道的门口。他没有进去。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "最终位置应为 消防通道"):
            guard_narrative(prefix + "姜序挤进门缝。许川仍站在候车厅。", state, [], package=self.package)

    def test_fact_review_repairs_invented_history_before_showing_draft(self):
        invalid = "姜序说，下午三点我填了巡查正常。许川仍站在候车厅。"
        valid = "姜序没有回答。许川仍站在候车厅。"
        gateway = RecordedGateway([
            invalid, review_fixture(invalid, "“下午三点我填了巡查正常”", "素材没有这次巡查或记录内容。"),
            json.dumps({"replacements": [{"paragraphId": 1, "text": valid}]}, ensure_ascii=False), review_fixture(valid),
        ], streamed=True)
        shown = []
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        result, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(result["narrativeText"], valid)
        self.assertEqual(shown, [valid])
        self.assertEqual(len(gateway.messages), 4)
        stages = [item.get("generationStage") for item in audit["callObservations"]]
        self.assertIn("fact_review", stages)
        self.assertIn("semantic_repair_fact_review", stages)
        self.assertNotIn("下午三点我填了巡查正常", gateway.messages[2][-1]["content"])
        self.assertIn("REPAIR_GAP_1", gateway.messages[2][-1]["content"])

    def test_review_correction_is_applied_then_independently_checked(self):
        invalid = "姜序说，下午三点我填了巡查正常。许川仍站在候车厅。"
        valid = "姜序没有回答。许川仍站在候车厅。"
        review = json.loads(review_fixture(invalid, "下午三点我填了巡查正常", "未登记的巡查记录"))
        review["checks"][0]["correctedText"] = valid
        gateway = RecordedGateway([invalid, json.dumps(review), review_fixture(valid)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        _, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [valid])
        self.assertEqual(len(gateway.messages), 3)
        self.assertEqual("".join(s["text"] for s in json.loads(gateway.messages[2][1]["content"])["paragraphs"][0]["spans"]), valid)
        self.assertTrue(any(item.get("generationStage") == "semantic_repair_fact_review" for item in audit["callObservations"]))

    def test_review_correction_cannot_bypass_local_state_guard(self):
        invalid = "姜序说，下午三点我填了巡查正常。许川仍站在候车厅。"
        bad_fix = "闸门已经开了。许川仍站在候车厅。"
        review = json.loads(review_fixture(invalid, "下午三点我填了巡查正常", "未登记的巡查记录"))
        review["checks"][0]["correctedText"] = bad_fix
        gateway = RecordedGateway([invalid, json.dumps(review), review_fixture(bad_fix)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "要求写成执行结果"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.messages), 3)

    def test_flawed_short_draft_is_reviewed_and_corrected_before_expansion(self):
        invalid = "闸门已经开了。\n\n许川站在候车厅。"
        valid = "闸门仍然关闭。\n\n许川站在候车厅。"
        addition = "雨水打在玻璃上，许川收紧衣领，看着水珠沿玻璃往下滚落。" * 3
        reviewed = json.loads(review_fixture(invalid, "闸门已经开了", "要求不等于执行结果"))
        reviewed["checks"][0]["correctedText"] = "闸门仍然关闭。"
        expanded = "闸门仍然关闭。\n\n" + addition + "许川站在候车厅。"
        gateway = RecordedGateway([invalid, json.dumps(reviewed), json.dumps({"expansion":
            {"paragraphId": 2, "text": addition}}), review_fixture(expanded)])
        planner = LlmPlanner(gateway, minimum_narrative_characters=60,
                             context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        result, _ = planner.plan(self.context, self.selected, self.resolved)
        self.assertEqual(result["narrativeText"], expanded)
        self.assertIn("localIssue", json.loads(gateway.messages[1][1]["content"]))
        expansion_input = json.loads(gateway.messages[2][1]["content"])
        self.assertEqual(expansion_input["paragraphs"][0]["text"], "闸门仍然关闭。")
        self.assertNotIn("evidence", expansion_input)
        final_review_input = json.loads(gateway.messages[3][1]["content"])
        self.assertEqual(final_review_input["expandedParagraphId"], 2)
        self.assertEqual(final_review_input["expansionAddedText"], addition)
        self.assertEqual(json.loads(gateway.support_messages[0][1]["content"])["expansionAddedText"], addition)
        self.assertNotIn("beforeExpansionParagraphs", final_review_input)

    def test_fact_replacements_preserve_other_prose_and_reject_ambiguous_edits(self):
        narrative = "开头保持。\n\n错误事实。\n \n结尾保持。"
        self.assertEqual(LlmPlanner._apply_fact_replacements(narrative, json.dumps({
            "replacements": [{"paragraphId": 2, "text": "他没有回答。"}]})), "开头保持。\n\n他没有回答。\n \n结尾保持。")
        for replacements in ([{"paragraphId": 4, "text": "替换"}],
                             [{"paragraphId": True, "text": "替换"}],
                             [{"paragraphId": 2, "text": "替换"}, {"paragraphId": 2, "text": "另一替换"}],
                             [{"paragraphId": 2, "text": "他说，⟦REPAIR_GAP_1⟧。"}],
                             [{"paragraphId": 2, "text": "错误事实。"}]):
            with self.subTest(replacements=replacements), self.assertRaises(LlmError):
                LlmPlanner._apply_fact_replacements(narrative, json.dumps({"replacements": replacements}))

    def test_fact_review_requires_full_coverage_and_real_evidence(self):
        narrative = "许川站在候车厅。\n\n雨水落在玻璃上。"
        valid = {"checks": [
            {"paragraphId": 1, "status": "supported", "evidenceIds": ["e1"], "quote": "", "reason": "位置与已确认状态一致"},
            {"paragraphId": 2, "status": "non_factual", "evidenceIds": [], "quote": "", "reason": "普通天气描写"},
        ]}
        LlmPlanner._check_fact_review(json.dumps(valid), narrative, {"e1"})
        invalid = [
            {"issues": []}, {"checks": valid["checks"][:1]},
            {"checks": [valid["checks"][0], valid["checks"][0]]},
        ]
        for changes in ({"evidenceIds": []}, {"evidenceIds": ["e404"]},
                        {"status": "conflict", "quote": "正文里没有"},
                        {"status": "conflict", "quote": ""}, {"paragraphId": True}):
            invalid.append({"checks": [{**valid["checks"][0], **changes}, valid["checks"][1]]})
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(LlmError):
                LlmPlanner._check_fact_review(json.dumps(payload), narrative, {"e1"})

    def test_missing_review_records_are_filled_without_overwriting_existing_checks(self):
        narrative = "许川没有回答。\n\n雨声不停。\n\n他站在原地。"
        complete = json.loads(review_fixture(narrative))
        incomplete = {"checks": [complete["checks"][1]]}
        missing = {"checks": [complete["checks"][0], complete["checks"][2]]}
        with self.assertRaisesRegex(LlmError, "缺少该段") as caught:
            LlmPlanner._check_fact_review(json.dumps(incomplete), narrative, set())
        self.assertEqual(caught.exception.invalid_paragraph_ids, [1, 3])
        merged = LlmPlanner._merge_fact_review_records(json.dumps(incomplete), json.dumps(missing), [1, 3])
        self.assertEqual(json.loads(merged), complete)
        gateway = RecordedGateway([narrative, json.dumps(incomplete), json.dumps(missing)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        result, _ = planner.plan(self.context, self.selected, self.resolved)
        self.assertEqual(result["narrativeText"], narrative)
        retry = json.loads(gateway.messages[2][1]["content"])
        self.assertEqual(retry["onlyParagraphIds"], [1, 3])
        self.assertEqual([p["id"] for p in retry["paragraphs"]], [1, 3])

    def test_repair_strips_only_exact_long_neighbor_echo(self):
        neighbor = "陈砚转过身，目光落在电子钟上。那一眼很短，许川注意到他的喉结动了一下，像是咽下了什么话。雨水沿着窗户往下滚落，在白色的灯光下显得格外清楚。"
        original = neighbor + "\n\n错误事实。\n\n他没有回答。"
        corrected = LlmPlanner._apply_fact_replacements(original, json.dumps({"replacements": [
            {"paragraphId": 2, "text": neighbor + "他低头看着对讲机。"}]}))
        self.assertEqual(corrected, neighbor + "\n\n他低头看着对讲机。\n\n他没有回答。")
        with self.assertRaisesRegex(LlmError, "只重复了相邻"):
            LlmPlanner._apply_fact_replacements(original, json.dumps({"replacements": [{"paragraphId": 2, "text": neighbor}]}))

    def test_repair_prompt_contains_only_evidence_problem_and_numbered_prose(self):
        planner = LlmPlanner(object(), context_resolver=ModuleContextResolver.for_package(self.path, self.package))
        planner._prompt(self.context, self.selected, self.resolved, None)
        messages = planner._fact_repair_messages(self.context, self.selected, self.resolved, self.prefix, "具体错误")
        data = json.loads(messages[1]["content"])
        self.assertEqual(data["problems"], "具体错误")
        self.assertEqual(data["paragraphs"], [{"id": 1, "text": self.prefix}])
        self.assertTrue(data["evidence"])
        self.assertNotIn("叙事安排", messages[1]["content"])
        self.assertNotIn("只输出小说正文", messages[1]["content"])

    def test_fact_review_uses_same_bounded_confirmed_context_as_generation(self):
        context = copy.copy(self.context)
        context["lineage"] = [
            {"canonicalRelation": "diverged", "narrativeText": "遥远历史不应装载"},
            {"canonicalRelation": "diverged", "narrativeText": "截断范围以外" + "雨" * 1300 + "\n\n姜序说验收单日期不对。"},
            {"canonicalRelation": "diverged", "narrativeText": "陈砚要求零点前恢复放行。"},
        ]
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        planner = LlmPlanner(object(), context_resolver=resolver)
        prompt = planner._prompt(context, self.selected, self.resolved, None)
        loaded = resolver.loaded_paths[:]
        review_input = json.loads(planner._fact_review_messages(context, self.selected, self.resolved, self.prefix)[1]["content"])
        spans = next(item["spans"] for item in review_input["evidence"] if item["kind"] == "confirmedBranchContext")
        evidence = "".join(s["text"] for s in spans)
        for span in spans:
            self.assertIn(span["text"], prompt)
        self.assertIn("姜序说验收单日期不对", evidence)
        self.assertIn("陈砚要求零点前恢复放行", evidence)
        self.assertNotIn("遥远历史不应装载", evidence)
        self.assertNotIn("截断范围以外", evidence)
        self.assertEqual(resolver.loaded_paths, loaded)
        self.assertFalse(any(path.startswith("reader/") for path in loaded))

    def test_descriptive_corridor_reference_does_not_create_new_location(self):
        guard_narrative("许川望向消防通道。门里是一条窄窄的通道。许川仍站在候车厅。", self.state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "未登记"):
            guard_narrative("许川发现了秘密通道，推门走进秘密通道。", self.state, [], package=self.package)

    def test_repair_input_masks_only_rejected_claims_and_keeps_neighbor_context(self):
        paragraph = "“设备间进水了。”他说，“里面全是水，走不了人。”"
        masked = LlmPlanner._mask_fact_claims(paragraph, ["里面全是水走不了人", "走不了人"])
        self.assertEqual(masked, "“设备间进水了。”⟦REPAIR_GAP_1⟧")
        self.assertEqual(LlmPlanner._mask_fact_claims("错误。错误。", ["错误"]), "⟦REPAIR_GAP_1⟧")
        self.assertEqual(LlmPlanner._mask_fact_claims("“信号恢复之前，谁都不能进站台。”陈砚转过身。", ["谁都不能进站台"]),
                         "⟦REPAIR_GAP_1⟧陈砚转过身。")
        with self.assertRaisesRegex(LlmError, "无法定位"):
            LlmPlanner._mask_fact_claims(paragraph, ["不存在的断言"])
        planner = LlmPlanner(object(), context_resolver=ModuleContextResolver.for_package(self.path, self.package))
        planner._prompt(self.context, self.selected, self.resolved, None)
        messages = planner._fact_repair_messages(self.context, self.selected, self.resolved,
            "开头。\n\n" + paragraph + "\n\n结尾。\n\n无关段落。", "不能推出无法通行",
            [{"paragraphId": 2, "claims": ["里面全是水走不了人"]}])
        data = json.loads(messages[1]["content"])
        self.assertEqual(data["paragraphs"], [{"id": 2, "text": masked}])
        self.assertEqual([p["id"] for p in data["contextParagraphs"]], [1, 3])

    def test_repair_of_repeated_action_keeps_original_valid_occurrence(self):
        narrative = "许川捡起铜牌。\n\n雨声不停。\n\n许川捡起铜牌。\n\n" + self.prefix * 8
        review = json.loads(review_fixture(narrative))
        review["checks"][2].update(status="conflict", quotes=["许川捡起铜牌"],
            reason="物品已经取得，不能再次发现并取得", correctedText="许川握紧口袋里的铜牌。")
        with self.assertRaises(ValueError) as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, set())
        fixed = LlmPlanner._apply_fact_replacements(narrative, json.dumps({"replacements": caught.exception.corrected_paragraphs}))
        LlmPlanner._validate_fact_repair(fixed, caught.exception.fact_conflicts)
        self.assertTrue(fixed.startswith("许川捡起铜牌。"))
        with self.assertRaisesRegex(LlmError, "仍保留"):
            LlmPlanner._validate_fact_repair(fixed + "许川捡起铜牌。", caught.exception.fact_conflicts)

    def test_fact_review_failure_does_not_show_or_silently_accept_body(self):
        for response in (LlmError("review timeout", "transport_error"), '{"issues":[{"quote":"不存在的句子","reason":"错误"}]}'):
            with self.subTest(response=str(response)):
                gateway = RecordedGateway([self.prefix, response, response], streamed=True)
                planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
                shown = []
                with self.assertRaises(LlmError):
                    planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
                self.assertEqual(shown, [])
                self.assertEqual(len(gateway.messages), 2 if isinstance(response, Exception) else 3)

    def test_review_format_repair_preserves_the_valid_draft(self):
        narrative = "“你确定？”许川问。"
        invalid_review = json.loads(review_fixture(narrative))
        invalid_review["checks"][0].update(status="supported", quote=narrative)
        gateway = RecordedGateway([narrative, json.dumps(invalid_review), review_fixture(narrative)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        _, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [narrative])
        self.assertEqual(len(gateway.messages), 3)
        stages = [item.get("generationStage") for item in audit["callObservations"]]
        self.assertIn("fact_review_format_repair", stages)
        self.assertNotIn("semantic_repair", stages)

    def test_local_fact_repair_is_rechecked_and_never_retried_twice(self):
        invalid = "姜序说，下午三点我填了巡查正常。许川仍站在候车厅。"
        gateway = RecordedGateway([
            invalid, review_fixture(invalid, "下午三点我填了巡查正常", "未登记的巡查记录"),
            json.dumps({"replacements": [{"paragraphId": 1, "text": invalid.replace("三点", "四点")}]}),
            review_fixture(invalid.replace("三点", "四点"), "下午四点我填了巡查正常", "仍是未登记的巡查记录"),
        ])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "事实核对未通过"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.messages), 4)

    def test_format_repair_only_updates_invalid_records_and_keeps_conflicts(self):
        narrative = "“你确定？”许川问。\n\n陈砚说：“我检查过两遍。”\n\n" + self.prefix * 8
        fixed = narrative.replace("陈砚说：“我检查过两遍。”", "陈砚没有回答。")
        review = json.loads(review_fixture(narrative, "我检查过两遍", "没有检查往事的依据"))
        review["checks"][0]["status"] = "supported"
        review["checks"][1]["correctedText"] = "陈砚没有回答。"
        patch_review = {"checks": [json.loads(review_fixture(narrative))["checks"][0]]}
        gateway = RecordedGateway([narrative, json.dumps(review), json.dumps(patch_review), review_fixture(fixed)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        result, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(result["narrativeText"], fixed)
        self.assertEqual(shown, [fixed])
        self.assertEqual(len(gateway.messages), 4)
        repair_input = json.loads(gateway.messages[2][1]["content"])
        self.assertEqual(repair_input["onlyParagraphIds"], [1])
        self.assertEqual(repair_input["paragraphs"], [{"id": 1, "spans": LlmPlanner._referenced_fact_spans("“你确定？”许川问。", "p1")}])
        self.assertEqual(repair_input["checksToRepair"], [review["checks"][0]])
        self.assertIn("没有检查往事的依据", str(gateway.messages[3]))
        repair_review_context = json.loads(gateway.messages[3][-1]["content"])
        self.assertEqual(repair_review_context["changedParagraphIds"], [2])
        self.assertNotIn("replacedParagraphs", repair_review_context)
        self.assertIn("相邻段落", repair_review_context["checkRepairContinuity"])
        self.assertNotIn("陈砚说：“我检查过两遍。”", gateway.messages[3][1]["content"])
        self.assertIn("semantic_repair_fact_review", [item.get("generationStage") for item in audit["callObservations"]])

    def test_format_repair_cannot_overwrite_valid_conflict_records(self):
        original = review_fixture("开头。\n\n无依据的检查。", "无依据的检查", "没有来源")
        valid = json.loads(original)["checks"][0]
        for checks in ([], [valid, valid], [{**valid, "paragraphId": 2}], [{**valid, "paragraphId": True}]):
            with self.subTest(checks=checks), self.assertRaises(LlmError):
                LlmPlanner._merge_fact_review_records(original, json.dumps({"checks": checks}), [1])
        merged = json.loads(LlmPlanner._merge_fact_review_records(original, json.dumps({"checks": [valid]}), [1]))
        self.assertEqual(merged["checks"][1], json.loads(original)["checks"][1])
        with self.assertRaisesRegex(LlmError, "不能撤销"):
            LlmPlanner._merge_fact_review_records(original, json.dumps({"checks": [{**valid, "paragraphId": 2}]}), [2])
        conflict = json.loads(original)["checks"][1]
        merged = json.loads(LlmPlanner._merge_fact_review_records(original, json.dumps({"checks": [
            {**conflict, "reason": "模型试图省略原问题"}]}), [2]))
        self.assertEqual(merged["checks"][1]["reason"], conflict["reason"])

    def test_fact_quote_localization_preserves_words_negation_order_and_paragraph(self):
        paragraph = "“设备间进水了。”他说，“里面全是水，走不了人。”"
        quote = "设备间进水了。里面全是水，走不了人。"
        self.assertEqual(LlmPlanner._locate_fact_claims([quote], paragraph), ["设备间进水了", "里面全是水走不了人"])
        for invalid in ("设备间没有进水。里面全是水，走不了人。", "里面全是水。设备间进水了。",
                        "设备间进水了……走不了人", "设备间进水了。钥匙不见了。"):
            with self.subTest(quote=invalid), self.assertRaises(LlmError):
                LlmPlanner._locate_fact_claims([invalid], paragraph)
        review = json.loads(review_fixture(paragraph + "\n\n钥匙不见了。"))
        review["checks"][0].update(status="conflict", quote="钥匙不见了。", reason="没有依据")
        with self.assertRaises(LlmError):
            LlmPlanner._check_fact_review(json.dumps(review), paragraph + "\n\n钥匙不见了。", set())

    def test_review_collects_all_invalid_ids_without_accepting_question_premises(self):
        narrative = "“你检查的时候，唐栖在哪儿？”\n\n“你确定？”"
        review = json.loads(review_fixture(narrative))
        for check in review["checks"]:
            check["status"] = "supported"
        with self.assertRaises(LlmError) as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, set())
        self.assertEqual(caught.exception.invalid_paragraph_ids, [1, 2])

    def test_partial_correction_cannot_keep_another_rejected_assertion(self):
        bad_paragraph = "“隧道里没有人。”陈砚说，“我今晚已经检查过两遍。”"
        invalid = bad_paragraph + "\n\n" + self.prefix * 12
        partial = "“隧道里没有人。”陈砚说。"
        review = json.loads(review_fixture(invalid, bad_paragraph, "无隧道无人或检查往事的依据"))
        review["checks"][0]["quotes"] = ["隧道里没有人", "我今晚已经检查过两遍"]
        review["checks"][0]["correctedText"] = partial
        with self.assertRaises(ValueError) as caught:
            LlmPlanner._check_fact_review(json.dumps(review), invalid, set())
        self.assertFalse(hasattr(caught.exception, "corrected_paragraphs"))
        self.assertIn("隧道里没有人", caught.exception.fact_conflicts[0]["claims"])
        # Even a separate editing request must pass the retained-claim guard.
        gateway = RecordedGateway([invalid, json.dumps(review), json.dumps({"replacements": [
            {"paragraphId": 1, "text": partial}]})])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "仍保留已拒绝断言"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.messages), 3)

    def test_atomic_conflict_quotes_preserve_supported_text_in_same_paragraph(self):
        narrative = "“设备间进水了。”他说，“里面全是水，走不了人。”\n\n" + self.prefix * 8
        review = json.loads(review_fixture(narrative))
        review["checks"][0].update(status="conflict", quotes=["里面全是水，走不了人"],
            reason="进水不能推出无法通行", correctedText="“设备间进水了。”他说，“里面危险。”")
        with self.assertRaises(ValueError) as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, set())
        self.assertIn("设备间进水了", caught.exception.corrected_paragraphs[0]["text"])

    def test_partial_valid_suggestions_are_kept_while_only_unresolved_paragraph_is_edited(self):
        invalid = "姜序说：“我已经检查过两遍。”\n\n陈砚说：“轨道里全是水。”\n\n" + self.prefix * 12
        fixed = "姜序没有回答。\n\n陈砚看着玻璃门。\n\n" + self.prefix * 12
        review = json.loads(review_fixture(invalid))
        review["checks"][0].update(status="conflict", quotes=["我已经检查过两遍"],
            reason="没有检查往事依据", correctedText="姜序没有回答。")
        review["checks"][1].update(status="conflict", quotes=["轨道里全是水"],
            reason="没有轨道积水依据", correctedText="“轨道里全是水。”陈砚说。")
        gateway = RecordedGateway([invalid, json.dumps(review), json.dumps({"replacements": [
            {"paragraphId": 2, "text": "陈砚看着玻璃门。"}]}), review_fixture(fixed)])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        result, _ = planner.plan(self.context, self.selected, self.resolved)
        self.assertEqual(result["narrativeText"], fixed)
        editing = json.loads(gateway.messages[2][1]["content"])
        self.assertEqual([p["id"] for p in editing["paragraphs"]], [2])
        self.assertEqual(editing["contextParagraphs"][0]["text"], "姜序没有回答。")
        self.assertNotIn("我已经检查过两遍", gateway.messages[2][1]["content"])
        self.assertNotIn("轨道里全是水", gateway.messages[2][1]["content"])

    def test_legacy_broad_quote_requires_precise_claims_before_editing(self):
        narrative = "“设备间进水了。”他说，“里面全是水，走不了人。”"
        review = {"checks": [{"paragraphId": 1, "status": "conflict", "evidenceIds": [],
            "quote": "设备间进水了。里面全是水，走不了人。", "reason": "不能由进水推断无法通行",
            "correctedText": "“设备间进水了。”他说，“里面危险。”"}]}
        with self.assertRaisesRegex(LlmError, "最小错误断言") as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, set())
        self.assertEqual(caught.exception.invalid_paragraph_ids, [1])
        self.assertFalse(hasattr(caught.exception, "corrected_paragraphs"))

    def test_mixed_quote_with_supported_clause_requires_precise_review(self):
        narrative = "站里只有一条线路还在用。通往旧信号室的维修隧道。"
        review = json.loads(review_fixture(narrative, narrative, "没有只剩一条线路在用的依据"))
        review["checks"][0]["correctedText"] = "临潮站有一段通往旧信号室的维修隧道。"
        evidence = [{"id": "e1", "kind": "sourceSceneCues", "value": "临潮站只有一段通往旧信号室的维修隧道，平日不对旅客开放。"}]
        with self.assertRaisesRegex(LlmError, "夹带素材已有") as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, {"e1"}, evidence)
        self.assertEqual(caught.exception.invalid_paragraph_ids, [1])
        review["checks"][0]["quotes"] = ["站里只有一条线路还在用"]
        with self.assertRaises(ValueError) as caught:
            LlmPlanner._check_fact_review(json.dumps(review), narrative, {"e1"}, evidence)
        self.assertEqual(caught.exception.corrected_paragraphs[0]["text"], "临潮站有一段通往旧信号室的维修隧道。")

    def test_fact_quote_can_span_literal_speech_attribution_without_losing_negation(self):
        paragraph = "姜序低下头。“那把钥匙，”他低声说，“是站务室后门的。以前只有一把，挂在档案柜里。”"
        claims = LlmPlanner._locate_fact_claims(["那把钥匙是站务室后门的。以前只有一把，挂在档案柜里。"], paragraph)
        self.assertEqual(claims, ["那把钥匙是站务室后门的", "以前只有一把挂在档案柜里"])
        self.assertEqual(LlmPlanner._mask_fact_claims(paragraph, claims), "姜序低下头。⟦REPAIR_GAP_1⟧")
        with self.assertRaisesRegex(LlmError, "仍保留"):
            LlmPlanner._validate_fact_repair(paragraph, [{"paragraphId": 1, "claims": claims}])
        with self.assertRaisesRegex(LlmError, "无法定位|可定位"):
            LlmPlanner._locate_fact_claims(["那把钥匙不是站务室后门的"], paragraph)
        self.assertIn("他刚刚接到了电话", LlmPlanner._fact_claim_text("“那把钥匙。”他刚刚接到了电话：“是站务室后门的。”"))

    def test_expansion_cannot_reintroduce_previously_rejected_claim(self):
        narrative = "陈砚说：“我今晚已经检查过两遍。”\n\n" + self.prefix * 8
        review = json.loads(review_fixture(narrative, "我今晚已经检查过两遍", "没有检查记录"))
        review["checks"][0]["correctedText"] = "陈砚沉默了。"
        gateway = RecordedGateway([narrative, json.dumps(review), json.dumps({"expansion":
            {"paragraphId": 2, "text": "陈砚说：“我今晚已经检查过两遍。”许川握紧衣角，雨声不停。"}})])
        planner = LlmPlanner(gateway, minimum_narrative_characters=len(re.sub(r"\s", "", narrative)),
                             context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        with self.assertRaisesRegex(LlmError, "仍保留已拒绝断言"):
            planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [])
        self.assertEqual(len(gateway.messages), 3)

    def test_local_and_model_fact_issues_are_repaired_together(self):
        invalid = "闸门已经开了。姜序说，下午三点我填了巡查正常。许川仍站在候车厅。"
        valid = "闸门仍然关闭。姜序没有回答。许川仍站在候车厅。"
        gateway = RecordedGateway([
            invalid, review_fixture(invalid, "下午三点我填了巡查正常", "未登记的巡查记录"),
            json.dumps({"replacements": [{"paragraphId": 1, "text": valid}]}),
            review_fixture(valid),
        ])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        shown = []
        result, _ = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(shown, [result["narrativeText"]])
        self.assertEqual(len(gateway.messages), 4)
        self.assertIn("要求写成执行结果", gateway.messages[2][-1]["content"])
        self.assertIn("未登记的巡查记录", gateway.messages[2][-1]["content"])

    def test_live_budget_counts_prose_and_fact_review_before_extra_request(self):
        gateway = OpenAICompatibleGateway("https://example.invalid/v1", "fixture", "fixture", False, allow_transport_fallback=False)
        gateway.remaining_calls = 2
        with patch.object(gateway, "_json", return_value=("fixture", "fixture")) as request:
            gateway.complete_text([])
            gateway.complete_json([])
            with self.assertRaisesRegex(LlmError, "调用上限"):
                gateway.complete_text([])
            self.assertEqual(request.call_count, 2)

    def test_runaway_repetition_is_rejected_before_fact_review(self):
        paragraph = "许川没有接话。他看向玻璃门外，末班列车的轮廓在雨幕里模糊成一片深色，车窗里一排排空座位浮在湿漉漉的夜色中，车门紧闭，像一条没有开口的鱼。"
        with self.assertRaisesRegex(ValueError, "循环重复"):
            guard_repeated_paragraphs("\n\n".join([paragraph] * 20))
        guard_repeated_paragraphs(paragraph + "\n\n“谁的命令？”\n\n“谁的命令？”")
        gateway = RecordedGateway(["\n\n".join([paragraph] * 20) + "\n\n许川转过身，向陈砚提出另一个问题。"])
        planner = LlmPlanner(gateway, context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        with self.assertRaisesRegex(LlmError, "循环重复"):
            planner.plan(self.context, self.selected, self.resolved, repair="前次已修复")
        self.assertEqual(len(gateway.messages), 1)

    def test_only_provable_tail_loop_is_trimmed_then_completed_and_reviewed(self):
        paragraph = "许川没有接话。他看向玻璃门外，末班列车的轮廓在雨幕里模糊成一片深色，车窗里一排排空座位浮在湿漉漉的夜色中，车门紧闭，像一条没有开口的鱼。"
        loop = "\n\n".join([paragraph, "“谁的命令？”"] * 12) + "\n\n" + paragraph[:30]
        self.assertEqual(trim_repeated_tail(loop), paragraph + "\n\n“谁的命令？”")
        with self.assertRaisesRegex(ValueError, "循环重复"):
            trim_repeated_tail(loop + "\n\n许川回到候车厅，向陈砚提出另一个问题。")
        continuation = "陈砚并没有立即回答，他握着对讲机，站在原地。许川看着他，等到周围安静下来，才把已经问过的问题再说了一遍。这一次，他的声音更低，也更清楚。"
        expanded = paragraph + "\n\n" + continuation + "“谁的命令？”"
        gateway = RecordedGateway([loop, json.dumps({"expansion": {"paragraphId": 2, "text": continuation}}), review_fixture(expanded)])
        shown = []
        planner = LlmPlanner(gateway, minimum_narrative_characters=140,
                             context_resolver=ModuleContextResolver.for_package(self.path, self.package), verify_source_facts=True)
        result, audit = planner.plan(self.context, self.selected, self.resolved, stream=shown.append)
        self.assertEqual(result["narrativeText"].count(paragraph), 1)
        self.assertIn(continuation, result["narrativeText"])
        self.assertEqual(result["narrativeText"], expanded)
        self.assertEqual(shown, [result["narrativeText"]])
        self.assertEqual(len(gateway.messages), 3)
        self.assertTrue(any(item.get("normalization") == "removed_repeated_tail" for item in audit["callObservations"]))

    def test_short_scene_expansion_preserves_sentences_prefix_and_ending(self):
        original = "“唐栖和你吵什么？”\n \n许川抬起头。他看着消防通道。"
        text = "雨声仍从身后传来。"
        expanded = LlmPlanner._apply_scene_expansion(original, json.dumps({"expansion": {"paragraphId": 2, "text": text}}))
        self.assertEqual(expanded, "“唐栖和你吵什么？”\n \n许川抬起头。" + text + "他看着消防通道。")
        invalid = [{"paragraphId": 3, "text": text}, {"paragraphId": True, "text": text},
                   {"paragraphId": 1, "text": text}, {"paragraphId": 2, "text": ""},
                   {"paragraphId": 2, "text": "许川抬起头。他看着消防通道。"},
                   {"paragraphId": 2, "text": text + "\n\n雨声不停。"}]
        for expansion in invalid:
            with self.subTest(expansion=expansion), self.assertRaises(LlmError):
                LlmPlanner._apply_scene_expansion(original, json.dumps({"expansion": expansion}))
        recall = "他想起那句“唐栖和你吵什么”，胸口仍然发闷。"
        self.assertIn(recall, LlmPlanner._apply_scene_expansion(original, json.dumps({"expansion": {"paragraphId": 2, "text": recall}})))
        long_sentence = "陈砚看了一眼墙上的电子钟，红色的数字仍然停在二十三点十分，一秒暗下去，下一秒亮回来。"
        with self.assertRaisesRegex(LlmError, "重复了原有长句"):
            LlmPlanner._apply_scene_expansion(long_sentence + "\n\n结尾。", json.dumps({"expansion": {"paragraphId": 2, "text": long_sentence}}))

    def test_clothing_detail_is_not_a_new_evidence_trace(self):
        guard_narrative("口袋里的铜牌贴着大腿，边缘的刻痕隔着布料也能辨认出数字的轮廓。", self.state, [], package=self.package)
        guard_narrative("四个人的影子被通道尽头的光拉长。", self.state, [], package=self.package)
        guard_narrative("许川没有再走回隧道，仍留在候车厅。", self.state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, '可进入地点'):
            guard_narrative("许川发现了新的地下室入口。", self.state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "可取证痕迹.*布料"):
            guard_narrative("许川发现铁门边挂着一块布料，这块布料成了追踪的线索。", self.state, [], package=self.package)

    def test_communication_guard_does_not_bind_unrelated_later_subjects(self):
        valid = [
            "唐栖的号码已经拨出去第七次，每一次都是忙音。候车厅里的电子钟一动不动。他记得自己进站时看过手表。",
            "唐栖的名字还亮在屏幕上。陈砚背对着众人。姜序的伞尖轻轻点着地砖，发出细碎的声响。",
            "许川提起唐栖，陈砚说他刚刚进站。",
            "许川收好唐栖的录音笔。姜序在长椅旁，唐栖问姜序：“你改过记录没有？”",
            "许川看着唐栖手里的录音笔，对她说：“今晚的事，我不替你解释。”",
            "唐栖的号码还亮在那里，像一扇没有回应的门。",
            "唐栖刚才发了一条语音。许川又听了一遍电话里的留言。",
            "唐栖给你留了语音，不代表她说的每一句都是真的。",
            "许川想起唐栖的语音里最后那个“人”字，像她来不及说完就被什么打断了。",
            "许川想起唐栖语音里最后那声金属门合上的巨响。他问：“她和你说了什么？”",
            "许川收好唐栖的录音笔。许川站在长椅旁，朝门口望了一眼，说：“司机还在车上，我去叫他。”",
        ]
        for text in valid:
            with self.subTest(text=text):
                guard_narrative(text, self.state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "去向或结果"):
            guard_narrative("许川追问唐栖，得到的回答是她走了。", self.state, [], package=self.package)
        for text in ("唐栖又发来一条语音。", "唐栖发来新的消息。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "通信状态"):
                guard_narrative(text, self.state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "冲突片段.*唐栖在语音里说"):
            guard_narrative("唐栖在语音里说她已经上了末班车。", self.state, [], package=self.package)

    def test_vehicle_hypothesis_and_negative_are_not_departure(self):
        for text in ("如果零点前恢复放行，那列车会开走？", "末班列车没有离开，仍停在站台。",
                     "“所以你要恢复放行。”许川说，“让末班列车开走。”", "他打算让末班列车开走。"):
            guard_narrative(text, self.state, [], package=self.package)
        for text in ("末班列车已经开走了。", "陈砚让列车开走。", "“让列车开走了。”",
                     "“让列车开走。”他要求。列车已经驶离站台。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "交通状态"):
                guard_narrative(text, self.state, [], package=self.package)

    def test_message_content_is_still_checked_with_explicit_attribution(self):
        for text in ("唐栖的语音里，她问：“你带了车票吗？”",
                     "唐栖的语音响起：\n\n“去后门等我。”"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "通信状态"):
                guard_narrative(text, self.state, [], package=self.package)
        from open_story_engine.cocreation import _communication_fact_conflict
        assertion = {'communicationTimestamp': {'characterName': '唐栖', 'knownMessageTexts': ['别让陈砚拿到储物柜里的录音。']}}
        self.assertIsNone(_communication_fact_conflict('唐栖按下录音笔。你的声音先出现：“工程做没做完，不影响列车按时跑。”',
            assertion, '陈砚', ['陈砚', '唐栖']))
        self.assertIsNotNone(_communication_fact_conflict('唐栖按下语音。唐栖的声音先出现：“临时新编的求助。”',
            assertion, '陈砚', ['陈砚', '唐栖']))

    def test_json_and_sse_show_complete_chapter_once(self):
        for streamed in (False, True):
            with self.subTest(streamed=streamed):
                gateway = RecordedGateway([self.prefix, self.suffix], streamed)
                shown = []
                result, _ = LlmPlanner(gateway, minimum_narrative_characters=80).plan(
                    self.context, self.selected, self.resolved, stream=shown.append,
                )
                self.assertEqual("".join(shown), result["narrativeText"])
                self.assertEqual("".join(shown).count(self.prefix), 1)

    def test_invalid_short_draft_is_repaired_before_any_continuation(self):
        invalid = "唐栖的语音响起：“末班车进站了，候车厅里只有我一个人。”"
        gateway = RecordedGateway([invalid, self.suffix])
        shown = []
        result, audit = LlmPlanner(gateway, minimum_narrative_characters=80).plan(
            self.context, self.selected, self.resolved, stream=shown.append,
        )
        self.assertEqual(len(gateway.messages), 2)
        self.assertEqual(result["narrativeText"], self.suffix)
        self.assertNotIn(invalid, "".join(shown))
        self.assertEqual(gateway.messages[1][2], {"role": "assistant", "content": invalid})
        self.assertIn("不能原样复述", gateway.messages[1][-1]["content"])
        self.assertIn("semantic_repair", [item.get("generationStage") for item in audit["callObservations"]])

    def test_repair_continuation_timeout_has_stage_and_never_persists(self):
        error = LlmError("fixture timeout", "transport_error")
        error.observations = [{"outcome": "failed", "transport": {"responseMode": "json", "durationMs": 37502}}]
        gateway = RecordedGateway(["许川的手机亮起，一条新消息的发件人未知。", self.prefix, error])
        store = SessionStore(":memory:")
        try:
            session = store.create_session(self.package)
            service = CoCreationService(self.package, store, MockPlanner())
            _, root = service.start(session["id"])
            macro = service.continue_direction(session["id"], root["id"], root["nextDirections"][0]["id"], "选择大方向")
            service.planner = LlmPlanner(gateway, minimum_narrative_characters=80)
            before = copy.deepcopy(store.branches(session["id"]))
            editors = []
            with self.assertRaisesRegex(LlmError, "fixture timeout") as raised:
                service.continue_direction(
                    session["id"], macro["id"], macro["nextDirections"][0]["id"], "选择小方向",
                    draft_editor=lambda draft: editors.append(draft),
                )
            self.assertEqual(editors, [])
            self.assertEqual(store.branches(session["id"]), before)
            failures = [item for item in raised.exception.audit["callObservations"] if item.get("transport")]
            self.assertEqual(failures[-1]["generationStage"], "semantic_repair_continuation")
        finally:
            store.close()

    def test_prompt_includes_attributed_message_without_loading_reader(self):
        self.assertEqual(self.selected["title"], "陈砚要求放行")
        self.assertEqual(self.selected["summary"].count("“"), self.selected["summary"].count("”"))
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        prompt = LlmPlanner(object(), context_resolver=resolver)._prompt(
            self.context, self.selected, self.resolved, None,
        )
        self.assertIn("唐栖的已登记语音原句：别让陈砚拿到储物柜里的录音。隧道里还有人。", prompt)
        self.assertNotIn("可补充合理过程、细节、新角色或新地点", prompt)
        self.assertFalse(any(path.startswith("reader/") for path in resolver.loaded_paths))

    def test_compiled_cross_chapter_locations_follow_scene_evidence(self):
        names = {item["id"]: item["name"] for item in self.package["locations"]}
        beats = self.package["story"]["narrativeGraph"]["beats"]
        self.assertEqual([names[beats[i]["branchState"]["playerLocationId"]] for i in (1, 2, 3, 4)],
                         ["候车厅", "消防通道", "站务室", "站务室"])
        for i in (1, 2, 3):
            self.assertEqual(beats[i]["nextDirections"][0]["statePatch"]["playerLocationId"],
                             beats[i + 1]["branchState"]["playerLocationId"])

    def test_scene_cues_are_bounded_and_do_not_include_future_chapter(self):
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        scope = resolver.resolve(self.context, self.selected, self.resolved)
        self.assertTrue(scope["narrativeBrief"])
        self.assertLessEqual(sum(len(cue["text"]) for cue in scope["narrativeBrief"]), 1400)
        self.assertTrue(all(cue["lineRange"]["end"] <= 83 for cue in scope["narrativeBrief"]))
        self.assertNotIn("应急卫星终端", str(scope["narrativeBrief"]))
        self.assertFalse(any(path.startswith("reader/") for path in resolver.loaded_paths))

    def test_previously_registered_information_source_remains_available(self):
        scope = {"narrativeBrief": [], "priorNarrativeBrief": [{"text": "调度员报告列车仍然停靠。"}]}
        guard_offstage_reports("调度员说列车仍然停靠。", scope)
        with self.assertRaisesRegex(ValueError, "未登记"):
            guard_offstage_reports("陌生人报告信号恢复了。", scope)

    def test_chapter_boundary_retains_cited_communication_and_access_context(self):
        beats = self.package["story"]["narrativeGraph"]["beats"]
        context = {**self.context, "parent": {"id": "previous", "branchState": beats[1]["branchState"]}}
        selected = beats[1]["nextDirections"][0]
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        scope = resolver.resolve(context, selected, beats[2]["branchState"])
        cues = scope["narrativeBrief"] + scope["priorNarrativeBrief"]
        text = "\n".join(cue["text"] for cue in cues)
        self.assertIn("十九秒", text)
        self.assertIn("西边消防通道的锁坏了", text)
        self.assertIn("金属门猛然合上", text)
        new_text = "\n".join(cue["text"] for cue in scope["narrativeBrief"])
        previous_text = "\n".join(cue["text"] for cue in scope["priorNarrativeBrief"])
        self.assertNotIn("十九秒", new_text)
        self.assertIn("十九秒", previous_text)
        self.assertIn("西边消防通道的锁坏了", new_text)
        dialogue = next(item for item in scope["sourceDialogueContext"] if item["dialogueParagraphId"] == "paragraph-046")
        self.assertEqual([p["evidenceParagraphId"] for p in dialogue["paragraphs"]],
                         ["paragraph-045", "paragraph-046", "paragraph-047"])
        self.assertIn("姜序忽然向后退了半步", dialogue["paragraphs"][0]["text"])
        self.assertEqual(dialogue["speakerName"], "姜序")
        self.assertTrue(all(p["phase"] == "current" for p in dialogue["paragraphs"]))
        self.assertTrue(all(cue["lineRange"]["end"] <= scope["previousSourceLine"] for cue in scope["priorNarrativeBrief"]))
        self.assertTrue(all(cue["lineRange"]["end"] > scope["previousSourceLine"] for cue in scope["narrativeBrief"]))
        planner = LlmPlanner(object(), context_resolver=resolver)
        planner._prompt(context, selected, beats[2]["branchState"], None)
        evidence = planner._fact_evidence(context, selected, beats[2]["branchState"])
        self.assertTrue(any(item["kind"] == "sourceDialogueContext" and item["value"] == dialogue for item in evidence))
        self.assertTrue(any(item["kind"] == "priorSourceSceneCues" and "十九秒" in str(item["value"]) for item in evidence))
        self.assertLessEqual(sum(len(cue["text"]) for cue in cues), 1400)
        current_beat = next(module["beat"] for module in resolver._cache.values()
                            if module.get("beat", {}).get("id") == scope["currentBeat"]["id"])
        self.assertTrue(all(cue["lineRange"]["end"] <= current_beat["sourceEvidence"]["lineRange"]["end"] for cue in cues))
        self.assertFalse(any(path.startswith("reader/") for path in resolver.loaded_paths))

    def test_fact_activation_and_character_details_use_exact_source_evidence(self):
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        scope = resolver.resolve(self.context, self.selected, self.resolved)
        self.assertTrue(all(fact.get("lineRange", {}).get("end", 0) <= 81 for fact in scope["world"]["immutableFacts"]))
        late_fact = next(fact for fact in self.package["world"]["immutableFacts"] if fact.get("lineRange", {}).get("start") == 107)
        self.assertEqual(late_fact["sourceProgress"], "chapter_003")
        self.assertNotIn(late_fact, scope["world"]["immutableFacts"])
        character = next(item for item in scope["characters"] if item["name"] == "姜序")
        identity = next(item for item in character["sourceDescriptionEvidence"] if "夜班维修" in item["text"])
        self.assertEqual(identity["lineRange"], {"start": 39, "end": 39})
        self.assertNotEqual(resolver._character_detail(character, 81), "该角色在当前已确认剧情中尚未提供更多可用细节。")

    def test_unregistered_background_role_cannot_report_new_causal_facts(self):
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        scope = resolver.resolve(self.context, self.selected, self.resolved)
        for text in ("司机报告前方区间有异常。", "陈砚说，调度那边说时间也停了。", "站长的手机关机了。", "一个站务员走了过来，在陈砚面前停住。“钥匙在她那里。”他说。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "未登记的信息来源"):
                guard_offstage_reports(text, scope)
        guard_offstage_reports("陈砚要求零点前恢复放行。许川问，司机在哪里？", scope)

    def test_request_does_not_complete_gate_or_dispatch_transition(self):
        scope = ModuleContextResolver.for_package(self.path, self.package).resolve(self.context, self.selected, self.resolved)
        self.assertEqual(scope["actionContract"]["kind"], "request")
        for text in ("陈砚说：线路占用解除。", "闸门已经开了。", "指示灯由红转绿。"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "要求写成执行结果"):
                guard_offstage_reports(text, scope)
        guard_offstage_reports("陈砚要求零点前放行。许川说，不能让线路占用解除。", scope)
        guard_offstage_reports("线路占用没有解除。闸门已经开了吗？", scope)

    def test_location_violation_gets_one_bounded_repair(self):
        invalid = "许川走到站台。"
        gateway = RecordedGateway([invalid, "许川仍站在候车厅里。"])
        result, audit = LlmPlanner(gateway).plan(self.context, self.selected, self.resolved)
        self.assertEqual(result["narrativeText"], "许川仍站在候车厅里。")
        self.assertEqual(len(gateway.messages), 2)
        self.assertIn("semantic_repair", [item.get("generationStage") for item in audit["callObservations"]])

    def test_edited_draft_cannot_complete_unconfirmed_request(self):
        store = SessionStore(":memory:")
        try:
            session = store.create_session(self.package)
            service = CoCreationService(self.package, store, MockPlanner())
            _, root = service.start(session["id"])
            macro = service.continue_direction(session["id"], root["id"], root["nextDirections"][0]["id"], "选择大方向")
            service.planner = LlmPlanner(
                RecordedGateway([self.prefix]),
                context_resolver=ModuleContextResolver.for_package(self.path, self.package),
            )
            before = copy.deepcopy(store.branches(session["id"]))
            with self.assertRaisesRegex(ValueError, "要求写成执行结果"):
                service.continue_direction(
                    session["id"], macro["id"], macro["nextDirections"][0]["id"], "选择小方向",
                    draft_editor=lambda draft: draft["narrativeText"] + "闸门已经开了。",
                )
            self.assertEqual(store.branches(session["id"]), before)
        finally:
            store.close()

    def test_entry_player_locations_and_ledger_follow_chapter_switch(self):
        model = self.package["story"]["entryModel"]
        selections = [
            {"kind": "source_character", "sourceCharacterId": model["sourceCharacterIds"][0],
             "entryPointId": model["defaultEntryPointId"]},
            {"kind": "new_character", "entryPointId": model["defaultEntryPointId"], "profile": {
                "name": "林舟", "gender": "男", "age": 29, "occupation": "设备检修员",
                "sourceRelationship": "不认识原著角色，是临时滞留的旅客", "background": "因暴雨留在候车厅。",
            }},
        ]
        for selection in selections:
            with self.subTest(kind=selection["kind"]):
                store = SessionStore(":memory:")
                try:
                    initial = entry_initial_state(self.package, selection)
                    session = store.create_session(self.package, initial_state=initial)
                    service = CoCreationService(self.package, store, MockPlanner())
                    contract, root = service.start(session["id"], selection)
                    self.assertEqual(root["branchState"], initial)
                    current = root
                    while current["branchState"]["sourceProgress"] != "chapter_003":
                        current = service.continue_direction(session["id"], current["id"], current["nextDirections"][0]["id"], "选择方向")
                    state = current["branchState"]
                    position = state_character_locations(self.package, state)[contract["persona"]["name"]]
                    self.assertEqual(position["locationName"], "消防通道")
                    self.assertEqual(position["locationId"], state["playerLocationId"])
                    changes = [item for item in state["branchLedger"]["entries"]
                               if item["entityId"] == state["playerCharacterId"] and item["operation"] == "changed"]
                    self.assertEqual(changes[-1]["after"]["locationId"], state["playerLocationId"])
                    with self.assertRaisesRegex(ValueError, "玩家位置与角色位置补丁冲突"):
                        apply_branch_patch(self.package, state, {
                            "playerLocationId": state["playerLocationId"],
                            "characterLocationIds": {state["playerCharacterId"]: initial["playerLocationId"]},
                        }, current["sourceNodeRef"])
                    if selection["kind"] == "new_character":
                        self.assertEqual(state["branchLedger"]["entries"][0]["source"]["kind"], "player_profile")
                        self.assertNotIn("许川", state_character_locations(self.package, state))
                finally:
                    store.close()

    def test_continuation_keeps_source_facts_and_locations(self):
        resolver = ModuleContextResolver.for_package(self.path, self.package)
        continuation = LlmPlanner(object(), context_resolver=resolver)._continuation_prompt(
            self.context, self.selected, self.resolved, self.prefix,
        )
        self.assertIn("别让陈砚拿到储物柜里的录音。隧道里还有人。", continuation)
        self.assertIn("当前焦点场景地点：候车厅", continuation)
        self.assertIn("没有登记地点变更时必须留在此处", continuation)
        self.assertFalse(any(path.startswith("reader/") for path in resolver.loaded_paths))


if __name__ == "__main__":
    unittest.main()
