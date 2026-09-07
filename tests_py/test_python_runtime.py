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

from open_story_engine.cocreation import CoCreationService, LlmPlanner, MockPlanner, NarrativeFieldStream, apply_branch_patch, guard_narrative, guard_source_character_names, initial_branch_state, source_continuity_context, validate_branch_additions
from open_story_engine.content import load_story_package, package_path_from_root, validate_story_package
from open_story_engine.environment import load_env_file
from open_story_engine.llm import Completion, LlmError, OpenAICompatibleGateway, parse_json_content
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
            token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")

            self.assertEqual(root["kind"], "source_entry")
            self.assertEqual(token["parentId"], root["id"])
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

    def test_generation_status_only_starts_when_the_planner_is_used(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        started = []

        token = service.continue_direction(
            session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜",
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

    def test_llm_planner_rejects_invalid_json_without_generating_a_second_draft(self):
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
        with self.assertRaisesRegex(LlmError, "缺少 narrativeText"):
            planner.plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        store.close()

    def test_llm_prompt_places_locked_room_state_gate_before_output_schema(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}

        prompt = LlmPlanner(object())._prompt(
            {"package": self.package, "parent": token, "characterDetails": []}, selected, resolved, None,
        )

        self.assertIn("必须遵守的当前状态门槛（优先级最高）", prompt)
        self.assertIn("唐栖仍被困在锁闭的信号室内", prompt)
        self.assertIn("证据尚未取得", prompt)
        self.assertIn("当前证据边界（不可违反）", prompt)
        self.assertIn("绝不可写任何人已拿到、查看、持有、使用、保全或带走它们", prompt)
        self.assertIn("本回合必须在正文中实际完成的状态变化", prompt)
        self.assertIn('"playerLocationId": {"from": "location_waiting_hall", "to": "location_signal_tunnel"}', prompt)
        self.assertIn("`nextDirections[].statePatch` 只使用目标原始值", prompt)
        self.assertIn('"playerLocationId": "location_signal_tunnel"', prompt)
        self.assertIn("任何 `statePatch` 字段都不得是对象或数组", prompt)
        self.assertIn("不能只写准备、讨论、寻找或尝试，却把结果留给下一回合", prompt)
        self.assertIn("请规划 2,200 至 2,800 个中文字符的 `narrativeText`", prompt)
        self.assertIn("已登记地点（涉及这些地点时直接使用，不得重复登记）", prompt)
        self.assertIn("信号维修隧道：积水持续上升，信号室入口受损。", prompt)
        self.assertIn("受保护的世界历史", prompt)
        self.assertIn("唐栖录下陈砚的谈话并将录音藏入十七号柜。", prompt)
        self.assertIn("姜序：信号维修隧道（由 jiangLocationId 约束）", prompt)
        self.assertIn('"branchAdditions":{"locations":[],"characters":[],"items":[],"characterReveals":[]}', prompt)
        self.assertIn("既有地点内的通道、角落、入口、台阶、门前、低洼段或设备区域只是场景细节", prompt)
        self.assertLess(prompt.index("必须遵守的当前状态门槛（优先级最高）"), prompt.index("按此 JSON 格式输出"))
        store.close()

    def test_branch_additions_reject_duplicate_registered_location_name(self):
        state = {**self.package["initialState"], "derivedLocations": [], "derivedCharacters": [], "derivedCharacterReveals": []}
        with self.assertRaisesRegex(ValueError, "地点与已登记地点重名"):
            validate_branch_additions(self.package, state, {
                "locations": [{"id": "location_duplicate_tunnel", "name": "信号维修隧道", "summary": "重复地点。"}],
                "characters": [],
                "characterReveals": [],
            })

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

    def test_gateway_requests_a_completion_budget_suitable_for_long_chapters(self):
        class CapturingGateway(OpenAICompatibleGateway):
            def _stream(self, body, _on_delta, _timeout_seconds=None):
                self.request_body = body
                return '{"ok": true}', "stream"

        gateway = CapturingGateway("http://localhost", "test-key", "test-model", stream=True)
        gateway.complete_json([])
        self.assertEqual(gateway.request_body["max_tokens"], 4096)

        configured = CapturingGateway("http://localhost", "test-key", "test-model", stream=True, max_tokens=6144)
        configured.complete_json([])
        self.assertEqual(configured.request_body["max_tokens"], 6144)

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
            guard_narrative("唐栖从通风口爬出来，落在设备间。", state, [])
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("设备间里，唐栖靠在机柜旁，听见许川的声音。", state, [])
        with self.assertRaisesRegex(ValueError, "信号室仍锁闭"):
            guard_narrative("门轴脱出，唐栖跳下设备台，跨出门后抓住许川的手臂。", state, [])
        guard_narrative(
            "许川和姜序穿过设备间，来到锁闭的信号室门前。唐栖隔着铁门敲了三下，回应他们还在。",
            state,
            [],
        )
        guard_narrative("唐栖说：\"录音笔还在储物柜里。十七号柜。你拿到了吗？\"许川回答：\"拿到了铜牌。柜子还没开。\"", state, [])
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("唐栖手里攥着一支录音笔，说证据有了。", state, [])
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("许川从十七号柜取出录音笔，贴身收好。", state, [])
        with self.assertRaisesRegex(ValueError, "证据尚未取得"):
            guard_narrative("许川持有录音笔，准备离开站务室。", state, [])

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

    def test_mock_planner_produces_direction_specific_readable_prose(self):
        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")

        self.assertGreaterEqual(len("".join(rescue["narrativeText"].split())), 2000)
        self.assertIn("绿漆铁门", rescue["narrativeText"])
        self.assertIn("唐栖仍被困在锁闭的信号室内", rescue["narrativeText"])
        self.assertNotIn("并非一句口号", rescue["narrativeText"])
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

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = (
                    {"narrativeText": "许川和姜序进入维修通道。唐栖仍被困在锁闭的信号室里。"}
                    if self.calls == 1
                    else {"narrativeContinuation": "门后的金属回声很快又沉了下去。"}
                )
                return Completion(
                    content=json.dumps(content, ensure_ascii=False),
                    raw_response="short",
                    observations=[{"attempt": 1, "outcome": "completed"}],
                )

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
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

            def complete_json(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.messages.append(messages)
                content = (
                    {"narrativeText": "许川沿着潮湿的维修通道向前走。" + "甲" * 1_900}
                    if self.calls == 1
                    else {"narrativeContinuation": "姜序在岔口停下，抬手压住墙上的旧编号牌。" + "乙" * 400}
                )
                return Completion(
                    content=json.dumps(content, ensure_ascii=False),
                    raw_response=f"response-{self.calls}",
                    observations=[{"attempt": 1, "outcome": "completed", "transport": "json"}],
                )

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
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

    def test_llm_planner_keeps_source_draft_when_model_directions_have_invalid_patches(self):
        class PatchRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        selected = next(item for item in token["nextDirections"] if item["id"] == "direction_rescue_first")
        resolved = {**token["branchState"], **selected["statePatch"]}
        gateway = PatchRetryGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual([item["id"] for item in result["nextDirections"]], ["direction_lower_water_without_proof", "direction_return_for_records"])
        self.assertEqual(audit["callObservations"][-1]["normalization"], "replaced_invalid_model_directions_with_storypackage_templates")
        store.close()

    def test_llm_planner_keeps_lower_water_draft_when_model_invents_followup_state(self):
        class InvalidFollowupGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
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
        self.assertEqual(audit["callObservations"][-1]["normalization"], "replaced_invalid_model_directions_with_storypackage_templates")
        store.close()

    def test_llm_planner_closes_declared_source_terminal_without_a_hidden_menu(self):
        class TerminalGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.prompt = ""

            def complete_json(self, messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
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
        self.assertEqual(audit["callObservations"][-1]["normalization"], "completed_storypackage_terminal_arc")
        store.close()

    def test_llm_planner_registers_dynamic_branch_entities_and_keeps_valid_dynamic_direction(self):
        class DynamicWorldGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=content, raw_response=content, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, LlmPlanner(DynamicWorldGateway()))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        node = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")

        self.assertEqual(node["branchState"]["derivedLocations"][-1]["name"], "废弃泵房")
        self.assertEqual(node["branchState"]["derivedCharacters"][-1]["name"], "罗峥")
        directions = {item["id"]: item for item in node["nextDirections"]}
        self.assertIn("direction_verify_luo_zheng", directions)
        self.assertIn("direction_lower_water_without_proof", directions)
        self.assertEqual(directions["direction_verify_luo_zheng"]["statePatch"]["playerLocationId"], "location_abandoned_pump_room")
        store.close()

    def test_llm_planner_preserves_rescue_progress_after_branch_location_addition(self):
        class BranchLocationGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=serialized, raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = BranchLocationGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(lowered["branchState"]["waterLevel"], "lowered")
        self.assertEqual(lowered["branchState"]["derivedLocations"][-1]["name"], "维修通道低洼段")
        self.assertEqual(
            [item["id"] for item in lowered["nextDirections"]],
            ["direction_open_signal_room_without_proof", "direction_return_for_records"],
        )
        store.close()

    def test_llm_planner_keeps_registered_branch_entities_across_turns(self):
        class DynamicWorldGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=serialized, raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = DynamicWorldGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        first = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        second = service.continue_direction(session["id"], first["id"], "direction_verify_luo_zheng", "核验罗峥的线索")

        self.assertEqual(gateway.calls, 2)
        self.assertIn("罗峥", second["narrativeText"])
        self.assertEqual(second["branchState"]["derivedLocations"][-1]["id"], "location_abandoned_pump_room")
        self.assertEqual(second["branchState"]["derivedCharacters"][-1]["name"], "罗峥")
        store.close()

    def test_llm_planner_keeps_registered_branch_items_across_turns(self):
        class ItemGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.second_prompt = ""

            def complete_json(self, messages, _on_delta=None, _on_reset=None):
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
                return Completion(content=serialized, raw_response=serialized, observations=[{"attempt": self.calls, "outcome": "completed"}])

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        gateway = ItemGateway()
        service = CoCreationService(self.package, store, LlmPlanner(gateway))
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "先救唐栖，暂不取证")
        lowered = service.continue_direction(session["id"], rescue["id"], "direction_lower_water_without_proof", "选择方向：排开积水")

        self.assertEqual(rescue["branchState"]["derivedItems"], [{"id": "item_white_arrow_marker", "name": "白色箭头标记", "summary": "刻有手动阀检修编号的塑料箭头。"}])
        self.assertEqual(lowered["branchState"]["derivedItems"], rescue["branchState"]["derivedItems"])
        self.assertIn("分支登记物品：白色箭头标记（刻有手动阀检修编号的塑料箭头。）", gateway.second_prompt)
        store.close()

    def test_llm_planner_does_not_retry_a_transport_failure_as_a_draft_rewrite(self):
        class TransportFailureGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                raise LlmError("JSON 响应连接超时", "transport_error")

        store = SessionStore(":memory:")
        session = store.create_session(self.package)
        service = CoCreationService(self.package, store, MockPlanner())
        _, root = service.start(session["id"])
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
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

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                content = json.dumps({
                    "narrativeText": "许川和姜序踏进维修通道。唐栖仍被困在锁闭的信号室里，积水没有停止上涨。",
                    "summary": "许川抵达隧道，救援必须先处理上涨的水位。",
                    "factDeltas": [],
                    "openThreads": ["降低水位", "打开信号室"],
                    "nextDirections": [{"id": "dir_inspect", "title": "查看通风口", "summary": "确认通风口能否递送工具。", "statePatch": {"playerLocationId": {"from": "location_waiting_hall", "to": "location_signal_tunnel"}}}],
                    "storyArc": {"activeGoal": "救援唐栖", "currentPhase": "进入隧道", "goalDisposition": "continued", "chapter": {"title": "水线之下", "status": "continuing"}},
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
        gateway = NoProgressDirectionGateway()
        result, audit = LlmPlanner(gateway).plan({"package": self.package, "parent": token, "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(result["narrativeText"], "许川和姜序踏进维修通道。唐栖仍被困在锁闭的信号室里，积水没有停止上涨。")
        self.assertEqual([item["id"] for item in result["nextDirections"]], ["direction_lower_water_without_proof", "direction_return_for_records"])
        self.assertEqual(audit["callObservations"][-1]["normalization"], "replaced_invalid_model_directions_with_storypackage_templates")
        store.close()

    def test_llm_planner_rejects_second_person_draft_without_rewrite(self):
        class PerspectiveRetryGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0

            def complete_json(self, _messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                subject = "你"
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
        with self.assertRaisesRegex(LlmError, "不能把玩家写成"):
            LlmPlanner(gateway).plan({"package": self.package, "parent": token, "lineage": [root, token], "characterDetails": []}, selected, resolved)
        self.assertEqual(gateway.calls, 1)
        store.close()


if __name__ == "__main__":
    unittest.main()
