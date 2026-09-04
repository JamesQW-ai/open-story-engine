import unittest
import os
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from open_story_engine.cocreation import CoCreationService, LlmPlanner, MockPlanner, NarrativeFieldStream, guard_narrative, guard_source_character_names, source_continuity_context
from open_story_engine.content import load_story_package, package_path_from_root
from open_story_engine.environment import load_env_file
from open_story_engine.llm import Completion, OpenAICompatibleGateway, parse_json_content
from open_story_engine.play import PlayerTurnService
from open_story_engine.storage import SessionStore


class PythonRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.package = load_story_package(package_path_from_root())

    def test_ordinary_play_rebuilds_authoritative_state(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        result = PlayerTurnService(self.package, store, fixed_roll=6).play(session["id"], "许川检查十七号柜旁的铜牌", "turn-1")
        self.assertEqual(result["resolution"]["outcome"], "success")
        self.assertEqual(store.rebuild_state(session["id"], self.package["initialState"]), store.get_session(session["id"])["currentState"])
        store.close()

    def test_divergent_rescue_path_keeps_state_and_can_derive(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        self.assertEqual(rescue["canonicalRelation"], "diverged")
        self.assertEqual(rescue["branchState"]["tangStatus"], "located")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")
        completed = service.continue_direction(session["id"], lowered["id"], "direction_open_signal_room_without_proof", "选择方向：打开信号室")
        derived, entry = service.begin_derivative(session["id"], completed["id"], "先休养，再寻找证据缺口")
        self.assertEqual(derived["sourcePackageRef"], {"id": self.package["id"], "version": self.package["version"]})
        self.assertEqual(entry["branchState"]["storyScope"], "derived")
        self.assertEqual(self.package["id"], "rainy-waiting-room")
        started = service.continue_direction(session["id"], entry["id"], "direction_derivative_start", "选择方向：开始衍生篇")
        lead = service.continue_direction(session["id"], started["id"], "direction_derivative_follow_lead", "选择方向：暂离车站并跟进线索")
        named = service.continue_direction(session["id"], lead["id"], "direction_derivative_ask_identity", "选择方向：核验联系人身份")
        self.assertEqual(named["branchState"]["derivedCharacterReveals"], [{"characterId": "character_unidentified_contact", "name": "罗峥", "summary": "自称掌握相似项目编号线索的人；背景与动机仍待核验。"}])
        store.close()

    def test_narrative_guard_blocks_unconfirmed_train_departure(self):
        state = {"trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured"}
        with self.assertRaisesRegex(ValueError, "列车仍在等待放行"):
            guard_narrative("末班列车离开了临潮站。", state, [])

    def test_divergent_branch_can_rejoin_only_at_declared_anchor(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        rejoined = service.continue_direction(session["id"], rescue["id"], "direction_return_for_records", "选择方向：折返取证")
        self.assertEqual(rejoined["canonicalRelation"], "rejoined")
        self.assertEqual(rejoined["sourceNodeRef"], "node_records")
        self.assertEqual(rejoined["branchState"]["evidenceStatus"], "secured")
        self.assertEqual([item["id"] for item in rejoined["nextDirections"]], ["direction_enter_tunnel_with_proof"])
        store.close()

    def test_env_loader_uses_file_only_for_missing_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("STORY_TEST_FILE_ONLY=file-value\nSTORY_TEST_EXISTING=file-value\n", encoding="utf-8")
            previous = os.environ.get("STORY_TEST_EXISTING")
            os.environ["STORY_TEST_EXISTING"] = "process-value"
            try:
                load_env_file(path)
                self.assertEqual(os.environ["STORY_TEST_FILE_ONLY"], "file-value")
                self.assertEqual(os.environ["STORY_TEST_EXISTING"], "process-value")
            finally:
                os.environ.pop("STORY_TEST_FILE_ONLY", None)
                if previous is None:
                    os.environ.pop("STORY_TEST_EXISTING", None)
                else:
                    os.environ["STORY_TEST_EXISTING"] = previous

    def test_llm_planner_retries_invalid_json_before_returning_plan(self):
        class RetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = "{}" if self.calls == 1 else """{
                  "narrativeText":"许川和姜序踏进维修通道，先确认水位和信号室方向。",
                  "summary":"救援路线已展开，水位仍在上涨。",
                  "factDeltas":[],
                  "openThreads":["降低水位"],
                  "nextDirections":[{"id":"direction_lower_water_without_proof","title":"排开积水","summary":"打开手动阀降低水位。","statePatch":{"waterLevel":"lowered"}}],
                  "storyArc":{"activeGoal":"救援唐栖","currentPhase":"进入隧道","goalDisposition":"continued","chapter":{"title":"雨夜中的证词","status":"continuing"}},
                  "planning":{"citations":[{"kind":"branch_node","ref":"branch_test","rationale":"承接当前场景。"}],"confidence":"medium","stateChangeProposals":[]}
                }"""
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = RetryGateway()
        planner = LlmPlanner(gateway)
        result, audit = planner.plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 2)
        self.assertEqual(result["summary"], "救援路线已展开，水位仍在上涨。")
        self.assertEqual(len(audit["callObservations"]), 3)
        store.close()

    def test_gateway_accepts_delta_content_in_non_stream_json_response(self):
        class DeltaJsonGateway(OpenAICompatibleGateway):
            def _request(self, _body):
                payload = {"choices": [{"delta": {"content": "{\"ok\": true}"}}]}
                return io.BytesIO(json.dumps(payload).encode("utf-8"))

        completion = DeltaJsonGateway("http://localhost", "test-key", "test-model", stream=False).complete_json([])
        self.assertEqual(completion.content, '{"ok": true}')

    def test_stream_restores_double_escaped_paragraphs(self):
        displayed = []
        stream = NarrativeFieldStream(displayed.append)
        stream.feed('{"narrativeText":"第一段\\\\')
        stream.feed('n第二段"}')
        self.assertEqual("".join(displayed), "第一段\n第二段")

    def test_json_parser_accepts_transport_noise_around_complete_object(self):
        self.assertEqual(parse_json_content("说明文字\n{\"ok\": true}\n[DONE]"), {"ok": True})

    def test_narrative_guard_rejects_second_person(self):
        state = {"storyScope": "source", "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured"}
        with self.assertRaisesRegex(ValueError, "第三人称"):
            guard_narrative("你凑近门缝，听见唐栖在里面敲门。", state, [])

    def test_source_continuity_context_includes_item_ownership_and_parent_prose(self):
        lineage = [{"narrativeText": "陈砚腰间挂着黑色门卡和一串新钥匙。"}]
        context = source_continuity_context(self.package, lineage, {"hasLockerToken": True})
        self.assertIn("姜序持有维修通行证", context)
        self.assertIn("陈砚腰间挂着黑色门卡", context)

    def test_source_narrative_rejects_unregistered_named_character_and_locked_room_escape(self):
        state = {"storyScope": "source", "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured"}
        with self.assertRaisesRegex(ValueError, "未登记的新人物姓名: 秦戈"):
            guard_source_character_names("许川和秦戈跟在姜序身后。", self.package, state)
        guard_source_character_names("他低头看着积水，没有出声。", self.package, state)
        guard_source_character_names("许川又看了看姜序，没有立刻开口。", self.package, state)
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("唐栖从通风口爬出来，落在设备间。", state, [])
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("唐栖手里攥着一支录音笔，说证据有了。", state, [])

    def test_llm_planner_retries_invalid_state_patch_before_returning_draft(self):
        class PatchRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                patch = {"tangStatus": "injured"} if self.calls == 1 else {"waterLevel": "lowered"}
                content = json.dumps({
                    "narrativeText": "许川和姜序进入维修通道，唐栖仍被困在锁闭的信号室里。",
                    "summary": "救援路线已展开。",
                    "factDeltas": [],
                    "openThreads": ["降低水位"],
                    "nextDirections": [{"id": "direction_lower_water_without_proof", "title": "排开积水", "summary": "打开手动阀降低水位。", "statePatch": patch}],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "进入隧道", "goalDisposition": "continued", "chapter": {"title": "雨夜中的证词", "status": "continuing"}},
                    "planning": {"citations": [{"kind": "branch_node", "ref": "branch_test", "rationale": "承接当前场景。"}], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = PatchRetryGateway()
        result, _ = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 2)
        self.assertEqual(result["nextDirections"][0]["statePatch"], {"waterLevel": "lowered"})
        store.close()

    def test_llm_planner_retries_second_person_draft_before_returning_plan(self):
        class PerspectiveRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                subject = "你" if self.calls == 1 else "许川"
                content = json.dumps({
                    "narrativeText": subject + "和姜序进入维修通道，唐栖仍被困在锁闭的信号室里。",
                    "summary": "救援路线已展开。",
                    "factDeltas": [],
                    "openThreads": ["降低水位"],
                    "nextDirections": [{"id": "direction_lower_water_without_proof", "title": "排开积水", "summary": "打开手动阀降低水位。", "statePatch": {"waterLevel": "lowered"}}],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "进入隧道", "goalDisposition": "continued", "chapter": {"title": "雨夜中的证词", "status": "continuing"}},
                    "planning": {"citations": [{"kind": "branch_node", "ref": "branch_test", "rationale": "承接当前场景。"}], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = PerspectiveRetryGateway()
        result, _ = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "lineage": [root, token], "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 2)
        self.assertTrue(result["narrativeText"].startswith("许川"))
        store.close()


if __name__ == "__main__":
    unittest.main()
