import argparse
import unittest
import os
import io
import json
import sqlite3
import socket
import time
from threading import Event
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from open_story_engine import cli
from open_story_engine.cocreation import CoCreationService, DirectionEvaluator, LlmDirectionEvaluator, LlmPlanner, MockPlanner, NarrativeFieldStream, NarrativeReviewer, apply_branch_patch, guard_narrative, guard_source_character_names, initial_branch_state, normalize_optional_branch_items, parse_narrative_continuation, source_continuity_context, validate_branch_additions
from open_story_engine.content import load_story_package, package_path_from_root, validate_story_package
from open_story_engine.environment import load_env_file
from open_story_engine.llm import Completion, LlmError, OpenAICompatibleGateway, parse_json_content
from open_story_engine.play import PlayerTurnService
from open_story_engine.storage import SessionStore


def test_plain_narrative(content):
    """Keep legacy structured test fixtures from leaking into a prose-only gateway."""
    if not isinstance(content, str):
        return content
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    if not isinstance(payload, dict):
        return content
    return payload.get("narrativeText") or payload.get("narrativeContinuation") or content


class PythonRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.package = load_story_package(package_path_from_root())

    def _phase_root(self, service, session_id, root):
        arc_id = root["nextDirections"][0].get("arcId")
        if not arc_id:
            return root
        return service.continue_direction(session_id, root["id"], "arc:" + arc_id, "选择大方向")

    def test_package_path_uses_package_id_from_environment(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STORY_PACKAGE_ID": "cultivation-journey",
            "STORY_PACKAGE_VERSION": "1.2.3",
        }, clear=False):
            path = package_path_from_root(Path(directory))
        self.assertEqual(path, Path(directory) / "content" / "packages" / "cultivation-journey" / "1.2.3.json")

    def test_default_direction_evaluator_matches_current_menu_data_without_story_ids(self):
        parent = {
            "nextDirections": [
                {"id": "direction_join_sect", "title": "拜入宗门", "summary": "前往青云门参加入门考核。", "suggestedInput": "去青云门参加考核", "statePatch": {"phase": "sect"}},
                {"id": "direction_market_clue", "title": "追查坊市线索", "summary": "留在坊市核验失窃账册。", "suggestedInput": "留在坊市查账", "statePatch": {"phase": "market"}},
            ]
        }
        evaluation, audit = DirectionEvaluator().evaluate(parent, "先去青云门参加考核")
        self.assertIsNone(audit)
        self.assertEqual(evaluation["kind"], "accepted")
        self.assertEqual(evaluation["directionId"], "direction_join_sect")

    def test_ordinary_play_rebuilds_authoritative_state(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        result = PlayerTurnService(self.package, store, fixed_roll=6).play(session["id"], "许川检查十七号柜旁的铜牌", "turn-1")
        self.assertEqual(result["resolution"]["outcome"], "success")
        self.assertEqual(store.rebuild_state(session["id"], self.package["initialState"]), store.get_session(session["id"])["currentState"])
        store.close()

    def test_cocreation_can_write_to_a_database_with_the_legacy_branch_column_order(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "legacy.sqlite")
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE branch_nodes (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  parent_id TEXT, node_json TEXT NOT NULL, created_at TEXT NOT NULL, request_id TEXT,
                  UNIQUE(session_id, sequence)
                )
            """)
            connection.close()

            store = SessionStore(path)
            session = store.create_session(self.package)
            service = CoCreationService(self.package, store, MockPlanner())
            _, root = service.start(session["id"])
            phase_root = self._phase_root(service, session["id"], root)
            token = service.continue_direction(session["id"], phase_root["id"], "direction_find_token", "选择方向：追查十七号柜")

            self.assertEqual(root["kind"], "source_entry")
            self.assertEqual(token["parentId"], phase_root["id"])
            store.close()

    def test_macro_plot_requires_a_choice_before_chapter_phases_and_cycles_on_completion(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])

        self.assertEqual(root["nextDirections"][0]["directionLevel"], "arc")
        arc = self._phase_root(service, session["id"], root)
        self.assertEqual(arc["branchState"], root["branchState"])
        self.assertEqual(arc["storyArc"]["arcId"], "arc_rescue_tang")
        self.assertEqual(arc["nextDirections"][0]["directionLevel"], "phase")

        token = service.continue_direction(session["id"], arc["id"], "direction_find_token", "选择方向：追查十七号柜")
        evidence = service.continue_direction(session["id"], token["id"], "direction_secure_evidence", "选择方向：先取得证据")
        records = service.continue_direction(session["id"], evidence["id"], "direction_verify_records", "选择方向：核实失联线索")
        tunnel = service.continue_direction(session["id"], records["id"], "direction_enter_tunnel_with_proof", "选择方向：带着证据进入隧道")
        lowered = service.continue_direction(session["id"], tunnel["id"], "direction_lower_water_with_proof", "选择方向：排开积水")
        rescued = service.continue_direction(session["id"], lowered["id"], "direction_open_signal_room_with_proof", "选择方向：打开信号室")

        self.assertEqual(token["storyArc"]["chapter"], {"title": "追查十七号柜", "status": "complete"})
        self.assertEqual(rescued["storyArc"]["goalDisposition"], "completed")
        self.assertEqual(rescued["nextDirections"], [{
            "id": "arc:arc_hold_train", "title": "让事故真相进入公开程序",
            "summary": "在唐栖获救且证据仍在的前提下，迫使列车与站务系统暂停对事故的掩盖。",
            "directionLevel": "arc", "arcId": "arc_hold_train",
        }])

        accountability = service.continue_direction(session["id"], rescued["id"], "arc:arc_hold_train", "选择大方向：让事故真相进入公开程序")
        conclusion = service.continue_direction(session["id"], accountability["id"], "direction_hold_train", "选择方向：公开真相")
        self.assertEqual(accountability["nextDirections"][0]["directionLevel"], "phase")
        self.assertEqual(conclusion["storyArc"]["goalDisposition"], "completed")
        self.assertEqual(conclusion["nextDirections"], [])
        store.close()

    def test_divergent_rescue_path_keeps_state_and_can_derive(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        self.assertEqual(rescue["canonicalRelation"], "diverged")
        self.assertEqual(rescue["branchState"]["tangLocationId"], "location_signal_room")
        self.assertEqual(rescue["branchState"]["tangStatus"], "located")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")
        completed = service.continue_direction(session["id"], lowered["id"], "direction_open_signal_room_without_proof", "选择方向：打开信号室")
        self.assertEqual(completed["nextDirections"], [])
        self.assertEqual(completed["storyArc"]["chapter"]["status"], "complete")
        derived, entry = service.begin_derivative(session["id"], completed["id"], "先休养，再寻找证据缺口")
        self.assertEqual(derived["sourcePackageRef"], {"id": self.package["id"], "version": self.package["version"]})
        self.assertEqual(entry["branchState"]["storyScope"], "derived")
        self.assertEqual(self.package["id"], "rainy-waiting-room")
        started = service.continue_direction(session["id"], entry["id"], "direction_derivative_start", "选择方向：开始衍生篇")
        self.assertEqual(started["branchState"]["playerLocationId"], "location_station_office")
        self.assertEqual(started["branchState"]["tangLocationId"], "location_station_office")
        lead = service.continue_direction(session["id"], started["id"], "direction_derivative_follow_lead", "选择方向：暂离车站并跟进线索")
        named = service.continue_direction(session["id"], lead["id"], "direction_derivative_ask_identity", "选择方向：核验联系人身份")
        self.assertEqual(named["branchState"]["derivedCharacterReveals"], [{"characterId": "character_unidentified_contact", "name": "罗峥", "summary": "自称掌握相似项目编号线索的人；背景与动机仍待核验。"}])
        store.close()

    def test_completed_arc_prints_derivative_guidance_and_uses_the_followup_prompt(self):
        node = {
            "narrativeText": "结局正文。",
            "nextDirections": [],
            "branchState": self.package["initialState"],
            "storyArc": {
                "activeGoal": "让事故真相进入公开程序",
                "currentPhase": "公开真相",
                "goalDisposition": "completed",
                "chapter": {"title": "公开真相", "status": "complete"},
            },
            "planning": {},
        }
        output = io.StringIO()
        with patch("sys.stdout", output):
            cli.print_branch(self.package, node)

        self.assertIn("当前大方向已完成", output.getvalue())
        self.assertIn("derive <后续目标>", output.getvalue())
        self.assertEqual(cli.co_creation_input_prompt(node), "后续 > ")

    def test_mock_planner_marks_its_structural_fixture(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")

        self.assertEqual(rescue["planning"]["narrativeOrigin"], "mock_structural_fixture")
        store.close()

    def test_generation_status_only_starts_when_the_planner_is_used(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        started = []

        phase_root = self._phase_root(service, session["id"], root)
        token = service.continue_direction(
            session["id"], phase_root["id"], "direction_find_token", "选择方向：追查十七号柜",
            on_generation_start=lambda: started.append("planner"),
        )
        self.assertEqual(started, [])

        service.continue_direction(
            session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先",
            on_generation_start=lambda: started.append("planner"),
        )
        self.assertEqual(started, ["planner"])
        store.close()

    def test_narrative_guard_blocks_unconfirmed_train_departure(self):
        state = {"trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured"}
        with self.assertRaisesRegex(ValueError, "列车仍在等待放行"):
            guard_narrative("末班列车离开了临潮站。", state, [], package=self.package)
        guard_narrative("许川必须在列车发车前赶回站台。", state, [])

    def test_divergent_branch_can_rejoin_only_at_declared_anchor(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
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

    def test_free_text_request_id_returns_existing_branch_without_a_second_evaluation(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner(), DirectionEvaluator())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")

        first = service.continue_free_text(
            session["id"], token["id"], "请姜序带路去隧道确认唐栖的位置。", request_id="request-free-text-1",
        )
        second = service.continue_free_text(
            session["id"], token["id"], "请姜序带路去隧道确认唐栖的位置。", request_id="request-free-text-1",
        )

        self.assertEqual(first["node"]["id"], second["node"]["id"])
        self.assertEqual(len(store.branches(session["id"])), 4)
        self.assertEqual(len(store.direction_audits(session["id"])), 1)
        with self.assertRaisesRegex(ValueError, "不能用于不同的父分支或玩家输入"):
            service.continue_free_text(
                session["id"], token["id"], "先去站务室取录音。", request_id="request-free-text-1",
            )
        store.close()

    def test_llm_direction_evaluator_resolves_a_clear_first_goal_after_model_clarification(self):
        class ClarifyingGateway:
            model = "test-model"

            def complete_json(self, _messages):
                content = json.dumps({
                    "kind": "clarification_needed",
                    "message": "请明确本回合最优先的一个方向。",
                }, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": 1, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")

        evaluation, audit = LlmDirectionEvaluator(ClarifyingGateway()).evaluate(
            token,
            "先让姜序带路去积水尽头确认唐栖的情况，在救援中查清事故真相，并阻止列车放行。",
        )

        self.assertEqual(evaluation["kind"], "accepted")
        self.assertEqual(evaluation["directionId"], "direction_rescue_first")
        self.assertIn(
            {"attempt": 1, "outcome": "normalized", "normalization": "resolved_ordered_multi_goal_to_first_published_direction", "directionId": "direction_rescue_first"},
            audit["callObservations"],
        )
        store.close()

    def test_llm_direction_evaluator_keeps_an_actual_alternative_for_clarification(self):
        class ClarifyingGateway:
            model = "test-model"

            def complete_json(self, _messages):
                content = json.dumps({"kind": "clarification_needed", "message": "请选择方向。"}, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")

        evaluation, audit = LlmDirectionEvaluator(ClarifyingGateway()).evaluate(token, "先去隧道还是站务室？")

        self.assertEqual(evaluation["kind"], "clarification_needed")
        self.assertEqual(audit["callObservations"], [])
        store.close()

    def test_live_evaluation_runs_all_registered_scenarios_with_an_isolated_mock_runtime(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STORY_LIVE_EVALUATION": "1",
            "STORY_PLANNER": "openai",
            "STORY_LLM_BASE_URL": "https://example.invalid/v1",
            "STORY_LLM_API_KEY": "test-key",
            "STORY_LLM_MODEL": "test-model",
            "STORY_LLM_REASONING_EFFORT": "none",
        }, clear=False), patch(
            "open_story_engine.cli.create_live_evaluation_runtime",
            return_value=(MockPlanner(), DirectionEvaluator(), NarrativeReviewer(), "test JSON runtime"),
        ):
            output = Path(directory) / "live-evaluation.json"
            with patch("sys.stdout", new=io.StringIO()):
                result = cli.run_evaluate_live(argparse.Namespace(scenario=None, output=str(output)))
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(report["runStatus"], "completed")
        self.assertEqual([item["id"] for item in report["results"]], [item.identifier for item in cli.LIVE_EVALUATION_SCENARIOS])
        self.assertTrue(all(item["status"] == "passed" for item in report["results"]))
        self.assertEqual(report["transport"], {
            "responseMode": "json", "timeoutSeconds": 60, "fallbackEnabled": False, "reasoningEffort": "none",
        })

    def test_live_evaluation_scenario_option_filters_the_registry(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STORY_LIVE_EVALUATION": "1",
            "STORY_PLANNER": "openai",
        }, clear=False):
            output = Path(directory) / "canonical-evaluation.json"
            with patch("sys.stdout", new=io.StringIO()):
                result = cli.run_evaluate_live(argparse.Namespace(scenario="canonical_route_skips_model", output=str(output)))
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual([item["id"] for item in report["results"]], ["canonical_route_skips_model"])

    def test_live_evaluation_transport_counts_only_actual_requests(self):
        details = cli.transport_call_details([{
            "operation": "branch_planner",
            "callObservations": [
                {"outcome": "normalized", "normalization": "local"},
                {"outcome": "completed", "generationStage": "initial", "transport": {"responseMode": "json", "httpStatus": 200, "durationMs": 42}},
            ],
        }])
        self.assertEqual(details, [{
            "operation": "branch_planner", "generationStage": "initial", "outcome": "completed",
            "retryReason": None, "responseMode": "json", "httpStatus": 200, "durationMs": 42,
            "failureKind": None,
        }])

    def test_live_evaluation_reports_empty_response_shape_without_exposing_content(self):
        diagnostic = cli.raw_response_diagnostic(json.dumps({
            "choices": [{"message": {"reasoning_content": "仅推理"}, "finish_reason": "length"}],
        }, ensure_ascii=False))

        self.assertEqual(diagnostic["finishReason"], "length")
        self.assertEqual(diagnostic["messageFields"], ["reasoning_content"])
        self.assertNotIn("仅推理", json.dumps(diagnostic, ensure_ascii=False))

    def test_live_evaluation_runtime_forces_json_without_transport_fallback(self):
        with patch.dict(os.environ, {
            "STORY_LLM_BASE_URL": "https://example.invalid/v1",
            "STORY_LLM_API_KEY": "test-key",
            "STORY_LLM_MODEL": "test-model",
            "STORY_LLM_REASONING_EFFORT": "none",
        }, clear=False):
            planner, evaluator, _reviewer, _label = cli.create_live_evaluation_runtime()

        self.assertFalse(planner.gateway.stream)
        self.assertFalse(planner.gateway.allow_transport_fallback)
        self.assertEqual(planner.gateway.timeout_seconds, 60)
        self.assertIsInstance(evaluator, DirectionEvaluator)
        self.assertEqual(planner.gateway.reasoning_effort, "none")

    def test_gateway_can_disable_json_to_sse_transport_fallback(self):
        class JsonOnlyGateway(OpenAICompatibleGateway):
            def _json(self, _body, _timeout_seconds=None):
                raise socket.timeout("JSON 响应连接超时")

            def _stream(self, _body, _on_delta, _timeout_seconds=None):
                raise AssertionError("验收 JSON 请求不能回退到 SSE")

        gateway = JsonOnlyGateway("http://localhost", "test-key", "test-model", stream=False, allow_transport_fallback=False)
        with self.assertRaisesRegex(LlmError, "JSON 响应连接超时"):
            gateway.complete_json([])

    def test_llm_planner_rejects_invalid_json_without_generating_a_second_draft(self):
        class RetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = RetryGateway()
        planner = LlmPlanner(gateway)
        with self.assertRaisesRegex(LlmError, "只接受小说文本"):
            planner.plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        store.close()

    def test_llm_prompt_places_locked_room_state_gate_before_output_schema(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        self.assertEqual(selected["title"], "先确认唐栖位置")
        self.assertIn("不打开信号室", selected["summary"])

        prompt = LlmPlanner(object())._prompt(
            {"package": self.package, "parent": token, "characterDetails": []}, selected, resolved, None,
        )

        self.assertIn("必须遵守的当前状态门槛（优先级最高）", prompt)
        self.assertIn("唐栖仍被困在锁闭的信号室内", prompt)
        self.assertIn("证据尚未取得", prompt)
        self.assertIn("声明式状态断言", prompt)
        self.assertIn("证据尚未取得；可讨论其位置或风险", prompt)
        self.assertIn("本回合必须在正文中实际完成的状态变化", prompt)
        self.assertIn("正文只可完成上列 `from` 到 `to` 的状态变化", prompt)
        self.assertIn('"playerLocationId": {"from": "location_waiting_hall", "to": "location_signal_tunnel"}', prompt)
        self.assertIn("状态、物品、人物、地点、章节名、摘要和后续方向均由运行时处理", prompt)
        self.assertIn('"playerLocationId": "location_signal_tunnel"', prompt)
        self.assertIn("不能只写准备、讨论、寻找或尝试，却把结果留给下一回合", prompt)
        self.assertIn("请规划 2,200 至 2,800 个中文字符", prompt)
        self.assertIn("已登记地点（涉及这些地点时直接使用，不得重复登记）", prompt)
        self.assertIn("信号维修隧道：积水持续上升，信号室入口受损。", prompt)
        self.assertIn("受保护的世界历史", prompt)
        self.assertIn("唐栖录下陈砚的谈话并将录音藏入十七号柜。", prompt)
        self.assertIn("姜序：信号维修隧道（由 jiangLocationId 约束）", prompt)
        self.assertIn("不得首次引入会跨回合影响行动、取证或因果的命名人物、地点、物品", prompt)
        self.assertLess(prompt.index("必须遵守的当前状态门槛（优先级最高）"), prompt.index("只输出一次完整小说正文"))
        store.close()

    def test_branch_additions_reject_duplicate_registered_location_name(self):
        state = {**self.package["initialState"], "derivedLocations": [], "derivedCharacters": [], "derivedCharacterReveals": []}
        with self.assertRaisesRegex(ValueError, "地点与已登记地点重名"):
            validate_branch_additions(self.package, state, {
                "locations": [{"id": "location_duplicate_tunnel", "name": "信号维修隧道", "summary": "重复地点。"}],
                "characters": [],
                "characterReveals": [],
            })

    def test_optional_branch_items_with_missing_metadata_are_registered_deterministically(self):
        state = initial_branch_state(self.package["story"]["narrativeGraph"]["beats"][0])
        observations = []
        additions = normalize_optional_branch_items(self.package, state, {
            "locations": [],
            "characters": [],
            "items": [{"name": "白色箭头", "description": "刻有手动阀检修编号。"}],
            "characterReveals": [],
        }, observations)

        registered = validate_branch_additions(self.package, state, additions)

        self.assertEqual(registered["items"], [{
            "id": "item_generated_a065fd4a6d3b0088",
            "name": "白色箭头",
            "summary": "刻有手动阀检修编号。",
        }])
        self.assertEqual(observations[-1]["normalization"], "repaired_optional_branch_items")
        self.assertEqual(observations[-1]["repaired"], 2)

    def test_branch_patch_rejects_structured_state_patch_value_before_state_lookup(self):
        state = initial_branch_state(self.package["story"]["narrativeGraph"]["beats"][0])
        with self.assertRaisesRegex(ValueError, "playerLocationId 必须使用目标原始值"):
            apply_branch_patch(self.package, state, {
                "playerLocationId": {"from": "location_waiting_hall", "to": "location_signal_tunnel"},
            }, "node_arrival")

    def test_gateway_accepts_delta_content_in_non_stream_json_response(self):
        class DeltaJsonGateway(OpenAICompatibleGateway):
            def _request(self, _body, _timeout_seconds=None):
                payload = {"choices": [{"delta": {"content": "{\"ok\": true}"}}]}
                return io.BytesIO(json.dumps(payload).encode("utf-8"))

        completion = DeltaJsonGateway("http://localhost", "test-key", "test-model", stream=False).complete_json([])
        self.assertEqual(completion.content, '{"ok": true}')

    def test_gateway_requests_plain_text_without_response_format(self):
        class CapturingGateway(OpenAICompatibleGateway):
            def _json(self, body, _timeout_seconds=None):
                self.request_body = body
                return "许川停在积水边，先确认唐栖仍在门后。", "raw"

        gateway = CapturingGateway("http://localhost", "test-key", "test-model", stream=False, allow_transport_fallback=False)
        completion = gateway.complete_text([{"role": "user", "content": "写正文"}])

        self.assertEqual(completion.content, "许川停在积水边，先确认唐栖仍在门后。")
        self.assertNotIn("response_format", gateway.request_body)

    def test_gateway_keeps_a_contentless_json_response_for_local_audit(self):
        class ContentlessJsonGateway(OpenAICompatibleGateway):
            def _request(self, _body, _timeout_seconds=None):
                payload = {"choices": [{"message": {"reasoning_content": "仅推理，没有正文"}, "finish_reason": "length"}]}
                return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        with self.assertRaisesRegex(LlmError, "缺少 choices") as raised:
            ContentlessJsonGateway("http://localhost", "test-key", "test-model", stream=False).complete_json([])

        self.assertIn('"finish_reason": "length"', raised.exception.raw_response)
        self.assertEqual(raised.exception.observations[0]["failureKind"], "empty_json")

    def test_gateway_explains_when_reasoning_uses_the_entire_json_budget(self):
        class ReasoningOnlyGateway(OpenAICompatibleGateway):
            def _request(self, _body, _timeout_seconds=None):
                payload = {"choices": [{"message": {"reasoning_content": "仅推理，没有正文"}, "finish_reason": "length"}]}
                return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        with self.assertRaisesRegex(LlmError, "STORY_LLM_REASONING_EFFORT=none"):
            ReasoningOnlyGateway("http://localhost", "test-key", "test-model", stream=False).complete_json([])

    def test_gateway_requests_a_completion_budget_suitable_for_long_chapters(self):
        class CapturingGateway(OpenAICompatibleGateway):
            def _stream(self, body, _on_delta, _timeout_seconds=None):
                self.request_body = body
                return '{"ok": true}', "stream"

        gateway = CapturingGateway("http://localhost", "test-key", "test-model", stream=True)
        gateway.complete_json([])
        self.assertEqual(gateway.request_body["max_tokens"], 8192)
        self.assertEqual(gateway.request_body["temperature"], 0.35)

        configured = CapturingGateway("http://localhost", "test-key", "test-model", stream=True, max_tokens=6144)
        configured.complete_json([])
        self.assertEqual(configured.request_body["max_tokens"], 6144)

        no_reasoning = CapturingGateway("http://localhost", "test-key", "test-model", stream=True, reasoning_effort="none")
        no_reasoning.complete_json([])
        self.assertEqual(no_reasoning.request_body["reasoning_effort"], "none")

    def test_gateway_falls_back_when_sse_stalls_before_first_delta(self):
        class SilentResponse:
            def __init__(self):
                from threading import Event
                self.closed = Event()

            def __iter__(self):
                self.closed.wait()
                return iter(())

            def close(self):
                self.closed.set()

        class StalledSseGateway(OpenAICompatibleGateway):
            def __init__(self):
                super().__init__("http://localhost", "test-key", "test-model", stream=True, timeout_seconds=60)
                self.response = SilentResponse()
                self.fallback_timeout = None

            def _first_sse_delta_timeout_seconds(self):
                return 0.01

            def _request(self, body, timeout_seconds=None):
                if body["stream"]:
                    return self.response
                raise AssertionError("JSON fallback is stubbed directly")

            def _json(self, _body, _timeout_seconds=None):
                self.fallback_timeout = _timeout_seconds
                return '{"ok": true}', '{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}'

        gateway = StalledSseGateway()
        completion = gateway.complete_json([])
        self.assertEqual(completion.content, '{"ok": true}')
        self.assertTrue(completion.used_transport_fallback)
        self.assertEqual(OpenAICompatibleGateway("http://localhost", "test-key", "test-model", stream=True, timeout_seconds=60)._first_sse_delta_timeout_seconds(), 15)
        self.assertEqual(completion.observations[0]["transport"]["responseMode"], "sse")
        self.assertEqual(completion.observations[1]["transport"]["responseMode"], "json")
        self.assertTrue(gateway.response.closed.is_set())
        retry_completion = gateway.complete_json([])
        self.assertTrue(retry_completion.used_transport_fallback)
        self.assertEqual(retry_completion.observations[0]["transport"]["responseMode"], "sse")
        self.assertEqual(OpenAICompatibleGateway("http://localhost", "test-key", "test-model", stream=True, timeout_seconds=60)._fallback_json_timeout_seconds(), 45)
        self.assertGreater(gateway.fallback_timeout, 59)
        self.assertLessEqual(gateway.fallback_timeout, 60)

    def test_gateway_falls_back_from_silent_json_to_sse(self):
        class StalledJsonGateway(OpenAICompatibleGateway):
            def __init__(self):
                super().__init__("http://localhost", "test-key", "test-model", stream=False, timeout_seconds=60)
                self.sse_request = None

            def _json(self, _body, _timeout_seconds=None):
                raise socket.timeout("JSON 响应连接超时")

            def _stream(self, body, _on_delta, timeout_seconds=None):
                self.sse_request = (body, timeout_seconds)
                return '{"ok": true}', 'data: {"choices":[{"delta":{"content":"{\\"ok\\": true}"}}]}'

        gateway = StalledJsonGateway()
        completion = gateway.complete_json([], lambda _chunk: None)
        self.assertTrue(completion.used_transport_fallback)
        self.assertTrue(completion.body_was_streamed)
        self.assertEqual(gateway.sse_request[0]["stream"], True)
        self.assertGreater(gateway.sse_request[1], 44)
        self.assertLessEqual(gateway.sse_request[1], 45)
        self.assertEqual(completion.observations[0]["transport"]["responseMode"], "json")
        self.assertEqual(completion.observations[1]["transport"]["responseMode"], "sse")

    def test_gateway_records_a_failed_json_request_without_transport_fallback(self):
        class TimedOutJsonGateway(OpenAICompatibleGateway):
            def _json(self, _body, _timeout_seconds=None):
                raise socket.timeout("JSON 响应连接超时")

        gateway = TimedOutJsonGateway(
            "http://localhost", "test-key", "test-model", stream=False,
            allow_transport_fallback=False,
        )
        with self.assertRaisesRegex(LlmError, "JSON 响应连接超时") as raised:
            gateway.complete_json([])

        self.assertEqual(raised.exception.observations[0]["outcome"], "failed")
        self.assertEqual(raised.exception.observations[0]["transport"]["responseMode"], "json")

    def test_stream_allows_reasoning_events_before_first_prose_chunk(self):
        class ReasoningResponse:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                yield 'data: {"choices":[{"delta":{"reasoning_content":"正在思考"}}]}\n'.encode("utf-8")
                time.sleep(0.02)
                yield b'data: {"choices":[{"delta":{"content":"{\\"ok\\": true}"}}]}\n'
                yield b'data: [DONE]\n'

            def close(self):
                self.closed = True

        class ReasoningGateway(OpenAICompatibleGateway):
            def _first_sse_delta_timeout_seconds(self):
                return 0.05

            def _request(self, _body, _timeout_seconds=None):
                return self.response

        gateway = ReasoningGateway("http://localhost", "test-key", "test-model", stream=True)
        gateway.response = ReasoningResponse()
        content, _ = gateway._stream({"stream": True}, None, timeout_seconds=0.05)
        self.assertEqual(content, '{"ok": true}')
        self.assertTrue(gateway.response.closed)

    def test_stream_does_not_extend_first_prose_deadline_for_reasoning_only_events(self):
        class ReasoningOnlyResponse:
            def __init__(self):
                self.closed = False

            def __iter__(self):
                yield 'data: {"choices":[{"delta":{"reasoning_content":"正在思考"}}]}\n'.encode("utf-8")
                time.sleep(0.03)
                yield b'data: [DONE]\n'

            def close(self):
                self.closed = True

        class ReasoningOnlyGateway(OpenAICompatibleGateway):
            def _first_sse_delta_timeout_seconds(self):
                return 0.01

            def _request(self, _body, _timeout_seconds=None):
                return self.response

        gateway = ReasoningOnlyGateway("http://localhost", "test-key", "test-model", stream=True)
        gateway.response = ReasoningOnlyResponse()
        with self.assertRaisesRegex(socket.timeout, "SSE 在首段正文返回前超时"):
            gateway._stream({"stream": True}, None, timeout_seconds=0.05)
        for _ in range(20):
            if gateway.response.closed:
                break
            time.sleep(0.005)
        self.assertTrue(gateway.response.closed)

    def test_stream_timeout_does_not_wait_for_a_blocking_response_close(self):
        class BlockingCloseResponse:
            def __iter__(self):
                from threading import Event
                Event().wait()
                return iter(())

            def close(self):
                time.sleep(0.1)

        class BlockingCloseGateway(OpenAICompatibleGateway):
            def _first_sse_delta_timeout_seconds(self):
                return 0.01

            def _request(self, _body, _timeout_seconds=None):
                return BlockingCloseResponse()

        started = time.monotonic()
        with self.assertRaisesRegex(socket.timeout, "SSE 在首段正文返回前超时"):
            BlockingCloseGateway("http://localhost", "test-key", "test-model", stream=True)._stream(
                {"stream": True}, None, timeout_seconds=0.05,
            )
        self.assertLess(time.monotonic() - started, 0.05)

    def test_stream_uses_one_total_deadline_after_the_first_prose_chunk(self):
        class OneChunkThenStallsResponse:
            def __init__(self):
                from threading import Event
                self.closed = Event()

            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"{\\"ok\\": true}"}}]}\n'
                self.closed.wait()
                return iter(())

            def close(self):
                self.closed.set()

        class OneChunkThenStallsGateway(OpenAICompatibleGateway):
            def _first_sse_delta_timeout_seconds(self):
                return 0.01

            def _request(self, _body, _timeout_seconds=None):
                return self.response

        gateway = OneChunkThenStallsGateway("http://localhost", "test-key", "test-model", stream=True)
        gateway.response = OneChunkThenStallsResponse()
        started = time.monotonic()
        with self.assertRaisesRegex(socket.timeout, "SSE 正文生成超时"):
            gateway._stream({"stream": True}, None, timeout_seconds=0.03)
        self.assertLess(time.monotonic() - started, 0.08)
        for _ in range(20):
            if gateway.response.closed.is_set():
                break
            time.sleep(0.005)
        self.assertTrue(gateway.response.closed.is_set())

    def test_stream_connection_has_the_same_first_delta_deadline(self):
        class ShortDeadlineGateway(OpenAICompatibleGateway):
            def _first_sse_delta_timeout_seconds(self):
                return 0.01

        released = Event()

        def stalled_urlopen(_request, timeout):
            self.assertEqual(timeout, 0.01)
            released.wait()
            return io.BytesIO()

        gateway = ShortDeadlineGateway("http://localhost", "test-key", "test-model", stream=True)
        try:
            with patch("open_story_engine.llm.urlopen", stalled_urlopen):
                with self.assertRaisesRegex(socket.timeout, "SSE 连接或首段正文返回前超时"):
                    gateway._request({"stream": True, "model": "test-model", "messages": []})
        finally:
            released.set()

    def test_gateway_requests_close_each_completed_turn_connection(self):
        requests = []

        def capture_urlopen(request, timeout):
            requests.append(request)
            return io.BytesIO()

        gateway = OpenAICompatibleGateway("http://localhost", "test-key", "test-model", stream=False)
        with patch("open_story_engine.llm.urlopen", capture_urlopen):
            first = gateway._request({"stream": False, "model": "test-model", "messages": []})
            second = gateway._request({"stream": False, "model": "test-model", "messages": []})
        first.close()
        second.close()

        self.assertEqual(len(requests), 2)
        self.assertEqual([request.get_header("Connection") for request in requests], ["close", "close"])

    def test_stream_restores_double_escaped_paragraphs(self):
        displayed = []
        stream = NarrativeFieldStream(displayed.append)
        stream.feed('{"narrativeText":"第一段\\\\')
        stream.feed('n第二段"}')
        self.assertEqual("".join(displayed), "第一段\n第二段")

    def test_json_parser_accepts_transport_noise_around_complete_object(self):
        self.assertEqual(parse_json_content("说明文字\n{\"ok\": true}\n[DONE]"), {"ok": True})

    def test_continuation_parser_accepts_only_literal_newlines_in_its_single_field(self):
        self.assertEqual(
            parse_narrative_continuation('{"narrativeContinuation":"第一段。\n第二段。"}'),
            "第一段。\n第二段。",
        )
        with self.assertRaisesRegex(LlmError, "只能包含 narrativeContinuation"):
            parse_narrative_continuation('{"narrativeContinuation":"续写", "statePatch":{}}')

    def test_narrative_guard_rejects_second_person(self):
        state = {"storyScope": "source", "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured"}
        with self.assertRaisesRegex(ValueError, "第三人称"):
            guard_narrative("你凑近门缝，听见唐栖在里面敲门。", state, [])
        guard_narrative(
            "你凑近门缝，听见唐栖在里面敲门。", state, [],
            {"perspective": "first_person"},
        )

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
        guard_source_character_names("平时走过去只要五分钟，现在积水更深。", self.package, state)
        guard_source_character_names("门缝里的光很弱，但足够让许川看清里面的影子。", self.package, state)
        guard_source_character_names("那根备用线能连到信号室，却早已断开。", self.package, state)
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("唐栖从通风口爬出来，落在设备间。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("设备间里，唐栖靠在机柜旁，听见许川的声音。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("门轴脱出，唐栖跳下设备台，跨出门后抓住许川的手臂。", state, [], package=self.package)
        guard_narrative(
            "许川和姜序穿过设备间，来到锁闭的信号室门前。唐栖隔着铁门敲了三下，回应他们还在。",
            state,
            [],
            package=self.package,
        )
        guard_narrative("唐栖说：\"录音笔还在储物柜里。十七号柜。你拿到了吗？\"许川回答：\"拿到了铜牌。柜子还没开。\"", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("唐栖手里攥着一支录音笔，说证据有了。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("许川从十七号柜取出录音笔，贴身收好。", state, [], package=self.package)
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("许川持有录音笔，准备离开站务室。", state, [], package=self.package)

    def test_narrative_guard_uses_authoritative_final_character_locations(self):
        state = {
            "storyScope": "source",
            "playerLocationId": "location_waiting_hall",
            "tangLocationId": "location_waiting_hall",
            "jiangLocationId": "location_waiting_hall",
            "trainStatus": "pending_release",
            "signalRoomStatus": "opened",
            "tangStatus": "rescued",
            "evidenceStatus": "unsecured",
        }
        with self.assertRaisesRegex(ValueError, "姜序 的最终位置应为 候车厅"):
            guard_narrative("姜序从消防通道回站务室，准备翻找旧记录。", state, [], package=self.package)
        guard_narrative(
            "姜序先回站务室取工具。随后姜序和许川回到候车厅，雨衣还滴着水。",
            state,
            [],
            package=self.package,
        )

    def test_narrative_guard_ignores_dialogue_about_another_character_location(self):
        state = {
            "storyScope": "source",
            "playerLocationId": "location_signal_tunnel",
            "tangLocationId": "location_signal_tunnel",
            "jiangLocationId": "location_signal_tunnel",
            "trainStatus": "pending_release",
            "signalRoomStatus": "locked",
            "tangStatus": "located",
            "evidenceStatus": "unsecured",
        }
        guard_narrative(
            "许川和姜序站在锁闭的信号室门前。唐栖隔着门板回应，声音很轻："
            "“他有没有发现你不在候车厅？”",
            state,
            [],
            package=self.package,
        )

    def test_narrative_guard_ignores_another_character_location_in_the_same_sentence(self):
        state = {
            "storyScope": "source",
            "playerLocationId": "location_signal_tunnel",
            "tangLocationId": "location_signal_tunnel",
            "jiangLocationId": "location_signal_tunnel",
            "trainStatus": "pending_release",
            "signalRoomStatus": "locked",
            "tangStatus": "located",
            "evidenceStatus": "unsecured",
        }
        guard_narrative(
            "许川想起唐栖最早那段语音，也想起陈砚在候车厅里过分平稳的语气。",
            state,
            [],
            package=self.package,
        )

    def test_narrative_guard_does_not_place_a_second_character_inside_a_locked_room_by_clause_order(self):
        state = {
            "storyScope": "source",
            "playerLocationId": "location_signal_tunnel",
            "tangLocationId": "location_signal_room",
            "jiangLocationId": "location_signal_tunnel",
            "trainStatus": "pending_release",
            "signalRoomStatus": "locked",
            "tangStatus": "located",
            "evidenceStatus": "unsecured",
        }
        guard_narrative(
            "唐栖仍被困在锁闭的信号室里，许川和姜序只能先寻找打开滑栓的办法。",
            state,
            [],
            package=self.package,
        )

    def test_narrative_guard_ignores_an_intermediate_static_character_location(self):
        state = {
            "storyScope": "source",
            "playerLocationId": "location_signal_tunnel",
            "tangLocationId": "location_signal_tunnel",
            "jiangLocationId": "location_signal_tunnel",
            "trainStatus": "pending_release",
            "signalRoomStatus": "locked",
            "tangStatus": "located",
            "evidenceStatus": "unsecured",
        }
        guard_narrative(
            "姜序在候车厅尽头的铁门前停下，先确认门锁没有被人动过。"
            "许川跟着他走下台阶，抵达信号维修隧道时，积水已经没过鞋面。"
            "姜序走在前面，带许川绕开垂落的电缆。",
            state,
            [],
            package=self.package,
        )

    def test_mock_planner_produces_a_package_driven_structural_fixture(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")

        self.assertGreaterEqual(len("".join(rescue["narrativeText"].split())), 2000)
        self.assertIn("先确认唐栖位置", rescue["narrativeText"])
        self.assertIn("信号维修隧道", rescue["narrativeText"])
        self.assertNotIn("绿漆铁门", rescue["narrativeText"])
        self.assertEqual(rescue["storyArc"]["chapter"], {"title": "先确认唐栖位置", "status": "complete"})
        store.close()

    def test_narrative_guard_allows_discussing_uncollected_recorder_after_a_token_is_found(self):
        state = {
            "storyScope": "source",
            "trainStatus": "pending_release",
            "signalRoomStatus": "locked",
            "tangStatus": "located",
            "evidenceStatus": "unsecured",
        }
        guard_narrative(
            "许川说：“我拿到了铜牌。”唐栖隔着门回应：“十七号柜里有录音笔和原始记录，先别让陈砚发现。”",
            state,
            [],
        )

    def test_llm_planner_rejects_a_live_chapter_shorter_than_2000_characters(self):
        class ShortChapterGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = (
                    {"narrativeText": "许川和姜序进入维修通道。唐栖仍被困在锁闭的信号室里。"}
                    if self.calls == 1
                    else {"narrativeContinuation": "门后的金属回声很快又沉了下去。"}
                )
                return Completion(
                    content=test_plain_narrative(json.dumps(content, ensure_ascii=False)),
                    raw_response="short",
                    observations=[{"attempt": 1, "outcome": "completed"}],
                )

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}

        gateway = ShortChapterGateway()
        with self.assertRaisesRegex(LlmError, "少于 2000 个非空白字符") as raised:
            LlmPlanner(gateway, minimum_narrative_characters=2000).plan(
                {"package": self.package, "parent": token, "characterDetails": []}, selected, resolved,
            )
        self.assertEqual(gateway.calls, 2)
        self.assertLess(raised.exception.audit["rejectedNarrativeCharacters"], 2000)
        self.assertEqual(
            next(item["rejectedNarrativeCharacters"] for item in raised.exception.audit["callObservations"] if "rejectedNarrativeCharacters" in item),
            raised.exception.audit["rejectedNarrativeCharacters"],
        )
        store.close()

    def test_llm_planner_appends_one_continuation_when_first_chapter_is_short(self):
        class ContinuationGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.messages = []

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.messages.append(messages)
                content = (
                    {"narrativeText": "许川沿着潮湿的维修通道向前走。" + "甲" * 1_900}
                    if self.calls == 1
                    else {"narrativeContinuation": "姜序在岔口停下，抬手压住墙上的旧编号牌。" + "乙" * 400}
                )
                return Completion(
                    content=test_plain_narrative(json.dumps(content, ensure_ascii=False)),
                    raw_response=f"response-{self.calls}",
                    observations=[{"attempt": 1, "outcome": "completed", "transport": "json"}],
                )

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = ContinuationGateway()

        result, audit = LlmPlanner(gateway, minimum_narrative_characters=2000).plan(
            {"package": self.package, "parent": token, "characterDetails": []}, selected, resolved,
        )

        self.assertEqual(gateway.calls, 2)
        self.assertGreaterEqual(len("".join(result["narrativeText"].split())), 2000)
        self.assertIn("姜序在岔口停下", result["narrativeText"])
        self.assertIn("续写已经生成但篇幅不足", gateway.messages[1][1]["content"])
        self.assertGreater(
            gateway.messages[1][1]["content"].rfind("唐栖仍被困在锁闭的信号室内"),
            gateway.messages[1][1]["content"].index("已有正文："),
        )
        self.assertEqual(audit["rawResponse"], "response-1\n\nresponse-2")
        self.assertTrue(any(item.get("generationStage") == "initial" for item in audit["callObservations"]))
        self.assertTrue(any(item.get("generationStage") == "continuation" for item in audit["callObservations"]))
        store.close()

    def test_narrative_guard_supports_package_declared_character_location_binding(self):
        package = json.loads(json.dumps(self.package))
        package["world"]["narrativeGuidelines"]["characterLocationStateFields"] = {
            "character_chen_yan": "observerLocationId",
        }
        state = {
            "storyScope": "source",
            "playerLocationId": "location_waiting_hall",
            "observerLocationId": "location_station_office",
            "trainStatus": "pending_release",
            "signalRoomStatus": "opened",
            "tangStatus": "rescued",
            "evidenceStatus": "secured",
        }
        with self.assertRaisesRegex(ValueError, "陈砚 的最终位置应为 站务室"):
            guard_narrative("陈砚回到候车厅，隔着玻璃门看向站台。", state, [], package=package)

    def test_story_package_rejects_invalid_declared_character_location_binding(self):
        package = json.loads(json.dumps(self.package))
        package["world"]["narrativeGuidelines"]["characterLocationStateFields"] = {
            "character_missing": "companionLocationId",
        }
        with self.assertRaisesRegex(ValueError, "characterLocationStateFields"):
            validate_story_package(package)

    def test_story_package_versions_keep_the_historical_package_loadable(self):
        historical = load_story_package(package_path_from_root(version="0.1.0"))
        self.assertEqual(historical["version"], "0.1.0")
        self.assertEqual(self.package["version"], "0.1.1")
        self.assertNotIn("stateModel", historical)
        self.assertIn("stateModel", self.package)
        with patch.dict(os.environ, {"STORY_PACKAGE_VERSION": "0.1.0"}):
            self.assertEqual(package_path_from_root().name, "0.1.0.json")

    def test_package_declared_invariant_controls_branch_state_without_engine_specific_names(self):
        state = initial_branch_state(self.package["story"]["narrativeGraph"]["beats"][0])
        package = json.loads(json.dumps(self.package))
        package["stateModel"]["invariants"][0]["message"] = "包声明的不变量被违反"
        with self.assertRaisesRegex(ValueError, "包声明的不变量被违反"):
            apply_branch_patch(
                package,
                state,
                {"tangStatus": "located", "tangLocationId": "location_waiting_hall"},
                "node_arrival",
            )

    def test_llm_planner_keeps_source_draft_when_model_directions_have_invalid_patches(self):
        class PatchRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = json.dumps({
                    "narrativeText": "许川和姜序进入维修通道，唐栖仍被困在锁闭的信号室里。",
                    "summary": "救援路线已展开。",
                    "factDeltas": [],
                    "openThreads": ["降低水位"],
                    "nextDirections": [
                        {"id": "dir_equipment_mezzanine", "title": "查看设备夹层", "summary": "前往未登记的设备夹层。", "statePatch": {"playerLocationId": "location_equipment_mezzanine"}},
                        {"id": "dir_drain", "title": "抽干积水", "summary": "将积水完全抽干。", "statePatch": {"waterLevel": "drained"}},
                    ],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "进入隧道", "goalDisposition": "continued", "chapter": {"title": "雨夜中的证词", "status": "continuing"}},
                    "planning": {"citations": [{"kind": "branch_node", "ref": "branch_test", "rationale": "承接当前场景。"}], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = PatchRetryGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual([item["id"] for item in result["nextDirections"]], ["direction_lower_water_without_proof", "direction_return_for_records"])
        self.assertEqual(audit["callObservations"][-1]["normalization"], "generated_scripted_turn_metadata")
        store.close()

    def test_llm_planner_keeps_lower_water_draft_when_model_invents_followup_state(self):
        class InvalidFollowupGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = json.dumps({
                    "narrativeText": "手动阀终于转动，积水退到脚背。唐栖仍被困在锁闭的信号室里，许川和姜序只能先寻找打开滑栓的办法。",
                    "summary": "排水暂时奏效，信号室仍待打开。",
                    "factDeltas": [],
                    "openThreads": ["打开信号室"],
                    "nextDirections": [
                        {"id": "dir_equipment_mezzanine", "title": "查看设备夹层", "summary": "前往未登记的设备夹层。", "statePatch": {"playerLocationId": "location_equipment_mezzanine"}},
                        {"id": "dir_drain", "title": "抽干积水", "summary": "将积水完全抽干。", "statePatch": {"waterLevel": "drained"}},
                    ],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "降低水位", "goalDisposition": "continued", "chapter": {"title": "水线以下", "status": "continuing"}},
                    "planning": {"citations": [{"kind": "branch_node", "ref": "branch_test", "rationale": "承接当前场景。"}], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        selected = next(item for item in rescue["nextDirections"] if item["id"] == "direction_lower_water_without_proof")
        resolved = {**rescue["branchState"], **selected["statePatch"]}

        gateway = InvalidFollowupGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": rescue, "characterDetails": []}, selected, resolved)

        self.assertEqual(gateway.calls, 1)
        self.assertEqual([item["id"] for item in result["nextDirections"]], ["direction_open_signal_room_without_proof"])
        self.assertEqual(result["nextDirections"][0]["statePatch"]["signalRoomStatus"], "opened")
        self.assertEqual(result["nextDirections"][0]["statePatch"]["tangStatus"], "rescued")
        self.assertEqual(result["nextDirections"][0]["statePatch"]["playerLocationId"], "location_waiting_hall")
        self.assertEqual(audit["callObservations"][-1]["normalization"], "generated_scripted_turn_metadata")
        store.close()

    def test_llm_planner_closes_declared_source_terminal_without_a_hidden_menu(self):
        class TerminalGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.prompt = ""

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.prompt = messages[-1]["content"]
                content = json.dumps({
                    "narrativeText": "许川和姜序把唐栖带回候车厅。她裹紧雨衣，望着仍在雨幕中的列车，没有再提立刻折返取证。",
                    "summary": "唐栖获救，未取得的证据成为后续调查的缺口。",
                    "factDeltas": [],
                    "openThreads": ["未取得的证据"],
                    "branchAdditions": {"locations": [], "characters": [], "characterReveals": []},
                    "nextDirections": [{"id": "direction_hidden_followup", "title": "立刻取证", "summary": "返回站务室取回录音。", "statePatch": {"evidenceStatus": "secured"}}],
                    "storyArc": {"activeGoal": "救出唐栖", "currentPhase": "救援收束", "goalDisposition": "continued", "chapter": {"title": "雨夜中的证词", "status": "continuing"}},
                    "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")
        selected = next(item for item in lowered["nextDirections"] if item["id"] == "direction_open_signal_room_without_proof")
        resolved = {**lowered["branchState"], **selected["statePatch"]}

        gateway = TerminalGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": lowered, "characterDetails": []}, selected, resolved)

        self.assertEqual(gateway.calls, 1)
        self.assertIn("抵达已声明的源分支终点", gateway.prompt)
        self.assertEqual(result["nextDirections"], [])
        self.assertEqual(result["storyArc"]["goalDisposition"], "completed")
        self.assertEqual(result["storyArc"]["chapter"]["status"], "complete")
        self.assertEqual(audit["callObservations"][-1]["normalization"], "generated_scripted_turn_metadata")
        store.close()

    def test_llm_planner_registers_dynamic_branch_entities_and_keeps_valid_dynamic_direction(self):
        class DynamicWorldGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = json.dumps({
                    "narrativeText": "姜序在检修口旁让开一步，罗峥从废弃泵房的阴影里走出来，只说自己见过相同的外包编号。",
                    "summary": "一名掌握旧案线索的人在废弃泵房现身。",
                    "factDeltas": [],
                    "openThreads": ["核验罗峥的线索"],
                    "branchAdditions": {
                        "locations": [{"id": "location_abandoned_pump_room", "name": "废弃泵房", "summary": "靠近信号隧道的闲置泵房。"}],
                        "characters": [{"id": "character_luo_zheng", "name": "罗峥", "summary": "掌握旧案项目编号的陌生来客。"}],
                        "characterReveals": [],
                    },
                    "nextDirections": [{
                        "id": "direction_verify_luo_zheng", "title": "核验罗峥的线索", "summary": "前往废弃泵房比对项目编号。",
                        "statePatch": {"playerLocationId": "location_abandoned_pump_room"},
                    }],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "出现新的调查协助者", "goalDisposition": "continued", "chapter": {"title": "泵房来客", "status": "continuing"}},
                    "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                }, ensure_ascii=False)
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, LlmPlanner(DynamicWorldGateway()))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        node = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")

        self.assertEqual(node["branchState"]["derivedLocations"], [])
        self.assertEqual(node["branchState"]["derivedCharacters"], [])
        directions = {item["id"]: item for item in node["nextDirections"]}
        self.assertIn("direction_lower_water_without_proof", directions)
        store.close()

    def test_llm_planner_preserves_rescue_progress_after_branch_location_addition(self):
        class BranchLocationGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                if self.calls == 1:
                    content = {
                        "narrativeText": "姜序指向维修通道低洼段，水声正从那里逼近。唐栖仍被困在锁闭的信号室里，许川决定先降低水位。",
                        "summary": "发现低洼段，救援需要先降低水位。",
                        "factDeltas": [], "openThreads": ["降低水位"],
                        "branchAdditions": {
                            "locations": [{"id": "location_low_lying_passage", "name": "维修通道低洼段", "summary": "靠近信号室的积水低洼处。"}],
                            "characters": [], "characterReveals": [],
                        },
                        "nextDirections": [{"id": "direction_lower_water_without_proof", "title": "排开积水", "summary": "打开手动阀降低水位。", "statePatch": {"waterLevel": "lowered"}}],
                        "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "确认积水入口", "goalDisposition": "continued", "chapter": {"title": "水线以下", "status": "continuing"}},
                        "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                    }
                else:
                    content = {
                        "narrativeText": "手动阀转动后，维修通道低洼段的水位慢慢退下。唐栖仍在锁闭的信号室内，许川听见门后传来一次敲击。",
                        "summary": "水位已降低，信号室仍需打开。",
                        "factDeltas": [], "openThreads": ["打开信号室", "保全证据"],
                        "branchAdditions": {"locations": [], "characters": [], "characterReveals": []},
                        "nextDirections": [{"id": "direction_return_for_records", "title": "返回站务室取证据", "summary": "带着铜牌返回站务室，取得录音和维修图纸。", "statePatch": {"playerLocationId": "location_station_office", "jiangLocationId": "location_waiting_hall", "evidenceStatus": "secured"}}],
                        "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "降低水位", "goalDisposition": "continued", "chapter": {"title": "水线以下", "status": "continuing"}},
                        "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                    }
                serialized = json.dumps(content, ensure_ascii=False)
                return Completion(content=test_plain_narrative(serialized), raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = BranchLocationGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(lowered["branchState"]["waterLevel"], "lowered")
        self.assertEqual(lowered["branchState"]["derivedLocations"], [])
        self.assertEqual(
            [item["id"] for item in lowered["nextDirections"]],
            ["direction_open_signal_room_without_proof"],
        )
        store.close()

    def test_llm_planner_keeps_registered_branch_entities_across_turns(self):
        class DynamicWorldGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                if self.calls == 1:
                    content = {
                        "narrativeText": "姜序在检修口旁让开一步，罗峥从废弃泵房的阴影里走出来，只说自己见过相同的外包编号。",
                        "summary": "一名掌握旧案线索的人在废弃泵房现身。",
                        "factDeltas": [], "openThreads": ["核验罗峥的线索"],
                        "branchAdditions": {
                            "locations": [{"id": "location_abandoned_pump_room", "name": "废弃泵房", "summary": "靠近信号隧道的闲置泵房。"}],
                            "characters": [{"id": "character_luo_zheng", "name": "罗峥", "summary": "掌握旧案项目编号的陌生来客。"}],
                            "characterReveals": [],
                        },
                        "nextDirections": [{"id": "direction_verify_luo_zheng", "title": "核验罗峥的线索", "summary": "前往废弃泵房比对项目编号。", "statePatch": {"playerLocationId": "location_abandoned_pump_room"}}],
                        "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "出现新的调查协助者", "goalDisposition": "continued", "chapter": {"title": "泵房来客", "status": "continuing"}},
                        "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                    }
                else:
                    content = {
                        "narrativeText": "废弃泵房里，罗峥把项目编号抄在纸角，提醒许川先核验排水泵的独立线路。",
                        "summary": "罗峥的线索指向排水泵线路。",
                        "factDeltas": [], "openThreads": ["降低水位"],
                        "branchAdditions": {"locations": [], "characters": [], "characterReveals": []},
                        "nextDirections": [{"id": "direction_lower_water_without_proof", "title": "排开积水", "summary": "打开手动阀降低水位。", "statePatch": {"waterLevel": "lowered"}}],
                        "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "核验泵房线路", "goalDisposition": "continued", "chapter": {"title": "泵房来客", "status": "continuing"}},
                        "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                    }
                serialized = json.dumps(content, ensure_ascii=False)
                return Completion(content=test_plain_narrative(serialized), raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = DynamicWorldGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        first = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        self.assertEqual(gateway.calls, 1)
        self.assertNotIn("direction_verify_luo_zheng", {item["id"] for item in first["nextDirections"]})
        self.assertEqual(first["branchState"]["derivedLocations"], [])
        self.assertEqual(first["branchState"]["derivedCharacters"], [])
        store.close()

    def test_llm_planner_keeps_registered_branch_items_across_turns(self):
        class ItemGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.second_prompt = ""

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                if self.calls == 2:
                    self.second_prompt = messages[-1]["content"]
                content = {
                    "narrativeText": "姜序从积水里捞起一枚白色箭头标记，背面刻着通往手动阀的检修编号。",
                    "summary": "白色箭头标记为排水阀提供了可核验的指引。",
                    "factDeltas": [], "openThreads": ["降低水位"],
                    "branchAdditions": {
                        "locations": [], "characters": [],
                        "items": ([{"id": "item_white_arrow_marker", "name": "白色箭头标记", "summary": "刻有手动阀检修编号的塑料箭头。"}] if self.calls == 1 else []),
                        "characterReveals": [],
                    },
                    "nextDirections": [],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "核验排水阀", "goalDisposition": "continued", "chapter": {"title": "水下标记", "status": "continuing"}},
                    "planning": {"citations": [], "confidence": "medium", "stateChangeProposals": []},
                }
                serialized = json.dumps(content, ensure_ascii=False)
                return Completion(content=test_plain_narrative(serialized), raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = ItemGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")

        self.assertEqual(rescue["branchState"]["derivedItems"], [])
        self.assertEqual(lowered["branchState"]["derivedItems"], rescue["branchState"]["derivedItems"])
        self.assertNotIn("分支登记物品", gateway.second_prompt)
        store.close()

    def test_llm_planner_does_not_retry_a_transport_failure_as_a_draft_rewrite(self):
        class TransportFailureGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                raise LlmError("JSON 响应连接超时", "transport_error")

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = TransportFailureGateway()
        with self.assertRaisesRegex(LlmError, "JSON 响应连接超时"):
            LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        store.close()

    def test_llm_planner_keeps_valid_first_draft_when_only_directions_are_noops(self):
        class NoProgressDirectionGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = "许川和姜序踏进维修通道。唐栖仍被困在锁闭的信号室里，积水没有停止上涨。"
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = NoProgressDirectionGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(result["narrativeText"], "许川和姜序踏进维修通道。唐栖仍被困在锁闭的信号室里，积水没有停止上涨。")
        self.assertEqual([item["id"] for item in result["nextDirections"]], ["direction_lower_water_without_proof", "direction_return_for_records"])
        self.assertEqual(audit["callObservations"][-1]["normalization"], "generated_scripted_turn_metadata")
        store.close()

    def test_llm_planner_rejects_second_person_draft_without_rewrite(self):
        class PerspectiveRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_text(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                subject = "你"
                content = subject + "和姜序进入维修通道，唐栖仍被困在锁闭的信号室里。"
                return Completion(content=test_plain_narrative(content), raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], self._phase_root(service, session["id"], root)["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = PerspectiveRetryGateway()
        with self.assertRaisesRegex(LlmError, "不能把玩家写成"):
            LlmPlanner(gateway).plan({"package": self.package, "parent": token, "lineage": [root, token], "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        store.close()


if __name__ == "__main__":
    unittest.main()
