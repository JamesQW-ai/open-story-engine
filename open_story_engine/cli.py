"""CLI entry points for normal play, co-creation, validation and live evaluation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cocreation import CoCreationService, DirectionEvaluator, LlmNarrativeReviewer, LlmPlanner, MockPlanner, NarrativeReviewer, location_name, state_character_locations
from .content import load_story_package, package_path_from_root
from .environment import load_env_file
from .llm import LlmError, OpenAICompatibleGateway
from .play import PlayerTurnService
from .storage import SessionStore


def environment_bool(name: str, fallback: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return fallback
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise ValueError(f"{name} 仅支持 true、false、1 或 0")


def environment_integer(name: str, fallback: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} 必须是 {minimum} 至 {maximum} 的整数") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum} 至 {maximum} 的整数")
    return value


def environment_reasoning_effort() -> Optional[str]:
    value = os.environ.get("STORY_LLM_REASONING_EFFORT", "").strip().lower()
    if not value:
        return None
    if value not in ("none", "minimal", "low", "medium", "high"):
        raise ValueError("STORY_LLM_REASONING_EFFORT 仅支持 none、minimal、low、medium 或 high")
    return value


def database_path() -> str:
    return os.environ.get("STORY_DATABASE_PATH", str(Path("data") / "open-story-engine.sqlite"))


def print_directions(directions: list[Dict[str, Any]], label: str = "可能的剧情方向") -> None:
    if not directions:
        return
    if label == "可能的剧情方向":
        level = directions[0].get("directionLevel")
        label = "可选择的大方向" if level == "arc" else "本章可选小方向" if level == "phase" else label
    print("\n" + label + "：")
    for index, direction in enumerate(directions, start=1):
        print(f"{index}. {direction['title']}：{direction['summary']}")
    if directions[0].get("directionLevel") != "arc":
        print("   自定义方向：直接描述下一步想推动的剧情或行动。")


def co_creation_input_prompt(node: Dict[str, Any]) -> str:
    return "后续 > " if not node.get("nextDirections") else "方向 > "


def print_branch(package: Dict[str, Any], node: Dict[str, Any], narrative_shown: bool = False) -> None:
    arc = node.get("storyArc")
    if arc:
        chapter = arc.get("chapter", {})
        if chapter.get("title"):
            print(f"章节：{chapter['title']}")
        print(f"主线目标：{arc['activeGoal']}\n当前阶段：{arc['currentPhase']}\n")
    if not narrative_shown:
        print(node["narrativeText"])
    if arc and arc.get("chapter", {}).get("status") == "complete" and arc["chapter"].get("title"):
        print("\n本章完：" + arc["chapter"]["title"])
    print_directions(node["nextDirections"])
    if not node["nextDirections"]:
        if arc and arc.get("goalDisposition") == "completed":
            print("\n当前大方向已完成；当前状态下没有可进入的后续大方向。")
        else:
            print("\n当前分支已结束，暂无后续剧情方向。")
        print("可输入 derive <后续目标> 创建独立衍生故事包，或输入 state、history 查看记录，输入 quit 退出。")
    if not narrative_shown and node.get("planning", {}).get("narrativeOrigin") == "mock_structural_fixture":
        print("\n[以上为 Mock Planner 的结构测试草稿，不代表模型生成质量或产品章节效果。]")


def print_state(package: Dict[str, Any], node: Dict[str, Any]) -> None:
    state = node["branchState"]
    positions = state_character_locations(package, state)
    if positions:
        print("\n人物位置：")
        for name, position in positions.items():
            print(f"- {name}：{position['locationName']}")
    state_model = package.get("stateModel", {})
    location_fields = set(state_model.get("locationReferenceFields", []))
    private_fields = {"derivedLocations", "derivedCharacters", "derivedCharacterReveals"}
    excluded = location_fields | private_fields | {"storyScope", "derivativeStage", "derivedTurn"}
    facts = [f"{key}={value}" for key, value in state.items() if key not in excluded and not isinstance(value, (dict, list))]
    if facts:
        print("状态：" + " | ".join(facts))
    if state["derivedLocations"] or state["derivedCharacters"]:
        names = {item["characterId"]: item["name"] for item in state["derivedCharacterReveals"]}
        characters = "、".join(names.get(item["id"], item["name"]) for item in state["derivedCharacters"]) or "暂无"
        print(f"分支扩展：地点 {'、'.join(item['name'] for item in state['derivedLocations']) or '暂无'} | 人物 {characters}")
    if state["storyScope"] == "derived":
        print(f"衍生包：{state['derivativeStage']} | 回合：{state['derivedTurn']}")


def paced_writer() -> tuple[Callable[[str], None], Callable[[str], None], Callable[[], bool]]:
    visible = [False]
    def write(delta: str) -> None:
        if not visible[0]:
            visible[0] = True
            sys.stdout.write("\n剧情草稿（生成中，尚未提交）：\n")
        for offset in range(0, len(delta), 4):
            sys.stdout.write(delta[offset: offset + 4])
            sys.stdout.flush()
            time.sleep(0.012)
    def reset(reason: str) -> None:
        if reason == "transport_fallback" and not visible[0]:
            sys.stdout.write("\n[SSE 在首段正文返回前超时，改用兼容 JSON 响应继续生成。]\n")
            sys.stdout.flush()
        elif reason == "json_transport_fallback" and not visible[0]:
            sys.stdout.write("\n[JSON 在首段正文返回前未响应，改用 SSE 流式生成。]\n")
            sys.stdout.flush()
        elif visible[0]:
            messages = {
                "transport_fallback": "流式响应中断，改用兼容 JSON 响应继续生成。",
            }
            message = messages.get(reason, "草稿未通过校验。")
            sys.stdout.write("\n\n[草稿未采纳：" + message + "]\n")
            sys.stdout.flush()
        visible[0] = False
    return write, reset, lambda: visible[0]


def create_cocreation_runtime() -> tuple[Any, Any, NarrativeReviewer, str]:
    mode = os.environ.get("STORY_PLANNER", "mock")
    if mode == "mock":
        return MockPlanner(), DirectionEvaluator(), NarrativeReviewer(), "Mock Planner（仅结构测试）+ Direction Evaluator"
    if mode != "openai":
        raise ValueError("STORY_PLANNER 仅支持 mock 或 openai")
    required = {key: os.environ.get(key, "").strip() for key in ("STORY_LLM_BASE_URL", "STORY_LLM_API_KEY", "STORY_LLM_MODEL")}
    if not all(required.values()):
        raise ValueError("使用 STORY_PLANNER=openai 时必须设置 STORY_LLM_BASE_URL、STORY_LLM_API_KEY 与 STORY_LLM_MODEL")
    stream = environment_bool("STORY_LLM_STREAM", True)
    timeout = environment_integer("STORY_LLM_TIMEOUT_SECONDS", 30, 5, 120)
    max_tokens = environment_integer("STORY_LLM_MAX_TOKENS", 8192, 1024, 8192)
    reasoning_effort = environment_reasoning_effort()
    planner = LlmPlanner(
        OpenAICompatibleGateway(required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"], stream, timeout, max_tokens, reasoning_effort=reasoning_effort),
        minimum_narrative_characters=2000,
    )
    evaluator = DirectionEvaluator()
    reviewer: NarrativeReviewer = NarrativeReviewer()
    if environment_bool("STORY_LLM_QUALITY_REVIEW", False):
        reviewer = LlmNarrativeReviewer(OpenAICompatibleGateway(required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"], False, timeout, reasoning_effort=reasoning_effort))
    return planner, evaluator, reviewer, f"LLM Planner（{required['STORY_LLM_MODEL']}，正文 {'SSE' if stream else 'JSON'}）+ 本地方向判定"


def create_live_evaluation_runtime() -> tuple[LlmPlanner, DirectionEvaluator, NarrativeReviewer, str]:
    required = {key: os.environ.get(key, "").strip() for key in ("STORY_LLM_BASE_URL", "STORY_LLM_API_KEY", "STORY_LLM_MODEL")}
    if not all(required.values()):
        raise ValueError("真实模型评估必须设置 STORY_LLM_BASE_URL、STORY_LLM_API_KEY 与 STORY_LLM_MODEL")
    max_tokens = environment_integer("STORY_LLM_MAX_TOKENS", 8192, 1024, 8192)
    reasoning_effort = environment_reasoning_effort()
    gateway_args = (
        required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
        False, 60, max_tokens, False, reasoning_effort,
    )
    planner = LlmPlanner(OpenAICompatibleGateway(*gateway_args), minimum_narrative_characters=2000)
    evaluator = DirectionEvaluator()
    return planner, evaluator, NarrativeReviewer(), f"LLM Planner（{required['STORY_LLM_MODEL']}，纯正文，JSON 传输，60 秒，无传输降级）+ 本地方向判定"


@dataclass(frozen=True)
class LiveEvaluationScenario:
    identifier: str
    checks: List[str]
    needs_live_model: bool
    run: Callable[[Dict[str, Any], Any, Any, NarrativeReviewer], List[Dict[str, Any]]]


def _start_evaluation_service(package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer) -> tuple[SessionStore, Dict[str, Any], CoCreationService, Dict[str, Any]]:
    store = SessionStore(":memory:")
    session = store.create_session(package)
    service = CoCreationService(package, store, planner, evaluator, reviewer)
    _, root = service.start(session["id"])
    return store, session, service, root


def _scenario_audits(store: SessionStore, session_id: str) -> List[Dict[str, Any]]:
    return store.llm_audits(session_id) + store.direction_evaluator_audits(session_id)


def _canonical_token(service: CoCreationService, session_id: str, root: Dict[str, Any]) -> Dict[str, Any]:
    first_direction = root["nextDirections"][0]
    if first_direction.get("directionLevel") == "arc":
        root = service.continue_direction(session_id, root["id"], first_direction["id"], "选择大方向：" + first_direction["title"])
    return service.continue_direction(session_id, root["id"], "direction_find_token", "选择方向：追查十七号柜")


def _assert_evaluation(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _scenario_canonical_route(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    class NeverCalledPlanner:
        def plan(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("规范路径不应调用 Planner")

    store, session, service, root = _start_evaluation_service(package, NeverCalledPlanner(), DirectionEvaluator(), reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        _assert_evaluation(token["canonicalRelation"] == "on_line", "规范方向未复用原著节点")
        source_beat = next(item for item in package["story"]["narrativeGraph"]["beats"] if item["id"] == "beat_token_found")
        _assert_evaluation(token["narrativeText"] == source_beat["sourceExcerpt"]["text"], "规范方向没有复用原著正文")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_broad_goal(package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, root = _start_evaluation_service(package, planner, evaluator, reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        result = service.continue_free_text(session["id"], token["id"], "先让姜序带路去积水尽头确认唐栖的情况，在救援中查清事故真相，并阻止列车放行。")
        _assert_evaluation(result.get("kind") == "accepted", result.get("message", "宽泛目标未被接受"))
        node = result["node"]
        _assert_evaluation(node["branchState"]["tangStatus"] == "located", "宽泛目标未锚定为救援优先")
        _assert_evaluation(node["branchState"]["playerLocationId"] == "location_signal_tunnel", "宽泛目标没有进入信号维修隧道")
        _assert_evaluation(bool(node["storyArc"].get("currentPhase")), "正文没有从当前阶段开始")
        return _scenario_audits(store, session["id"])
    except Exception as error:
        error.evaluation_audits = _scenario_audits(store, session["id"])
        raise
    finally:
        store.close()


def _scenario_locked_signal_room(package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, root = _start_evaluation_service(package, planner, evaluator, reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        node = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        state = node["branchState"]
        _assert_evaluation(state["signalRoomStatus"] == "locked", "锁闭信号室被错误改写")
        _assert_evaluation(state["tangStatus"] == "located", "唐栖在未开门前被错误标为获救")
        _assert_evaluation(
            state["signalRoomStatus"] == "locked" and state["tangStatus"] == "located",
            "正文没有通过锁闭信号室状态守卫",
        )
        return _scenario_audits(store, session["id"])
    except Exception as error:
        error.evaluation_audits = _scenario_audits(store, session["id"])
        raise
    finally:
        store.close()


def _scenario_controlled_rejoin(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        rescue = service.continue_direction(session["id"], token["id"], "direction_rescue_first", "选择方向：救援优先")
        rejoined = service.continue_direction(session["id"], rescue["id"], "direction_return_for_records", "选择方向：折返取证")
        source_beat = next(item for item in package["story"]["narrativeGraph"]["beats"] if item["id"] == "beat_office_entered")
        source_text = source_beat.get("sourceExcerpt", {}).get("text", "")
        _assert_evaluation(rejoined["canonicalRelation"] == "rejoined", "折返取证没有受控汇合")
        _assert_evaluation(not source_text or source_text not in rejoined["narrativeText"], "汇合正文拼接了未展示的原著节选")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_request_idempotency(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        request_id = "live-evaluation-idempotency"
        player_direction = "请姜序带路去隧道确认唐栖的位置。"
        first = service.continue_free_text(session["id"], token["id"], player_direction, request_id=request_id)
        before = len(store.branches(session["id"]))
        second = service.continue_free_text(session["id"], token["id"], player_direction, request_id=request_id)
        _assert_evaluation(first["kind"] == "accepted" and second["kind"] == "accepted", "自由文本方向没有被接受")
        _assert_evaluation(first["node"]["id"] == second["node"]["id"], "重复 requestId 没有返回原分支")
        _assert_evaluation(len(store.branches(session["id"])) == before, "重复 requestId 追加了新的分支")
        _assert_evaluation(len(store.direction_audits(session["id"])) == 1, "重复 requestId 重复执行了方向判定")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_forbidden_supernatural(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer)
    try:
        token = _canonical_token(service, session["id"], root)
        before = len(store.branches(session["id"]))
        result = service.continue_free_text(session["id"], token["id"], "用魔法瞬移进信号室救出唐栖。")
        _assert_evaluation(result["kind"] == "rejected", "超自然行动没有被拒绝")
        _assert_evaluation(result["citations"] == [{"kind": "immutable_fact", "ref": "fact_no_supernatural"}], "超自然拒绝未引用世界禁则")
        _assert_evaluation(len(store.branches(session["id"])) == before, "被拒绝的行动仍追加了剧情分支")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


LIVE_EVALUATION_SCENARIOS = (
    LiveEvaluationScenario("canonical_route_skips_model", ["规范节点复用不调用模型"], False, _scenario_canonical_route),
    LiveEvaluationScenario("broad_goal_starts_current_phase", ["宽泛目标锚定为救援优先", "主线以当前阶段开始且正文通过状态校验"], True, _scenario_broad_goal),
    LiveEvaluationScenario("locked_signal_room_state", ["锁闭信号室和唐栖状态不被正文越权", "正文通过锁闭状态守卫"], True, _scenario_locked_signal_room),
    LiveEvaluationScenario("controlled_rejoin_uses_new_narration", ["折返取证满足受控汇合", "正文不拼接未展示原著节选"], False, _scenario_controlled_rejoin),
    LiveEvaluationScenario("free_text_request_idempotency", ["重复 requestId 不重复判定或追加分支"], False, _scenario_request_idempotency),
    LiveEvaluationScenario("forbidden_supernatural_action", ["超自然行动被拒绝", "拒绝引用 fact_no_supernatural"], False, _scenario_forbidden_supernatural),
)


def transport_call_details(audits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    for audit in audits:
        for observation in audit.get("callObservations", []):
            transport = observation.get("transport")
            if not isinstance(transport, dict):
                continue
            details.append({
                "operation": audit["operation"],
                "generationStage": observation.get("generationStage"),
                "outcome": observation.get("outcome"),
                "retryReason": observation.get("retryReason"),
                "responseMode": transport.get("responseMode"),
                "httpStatus": transport.get("httpStatus"),
                "durationMs": transport.get("durationMs"),
                "failureKind": observation.get("failureKind"),
            })
    return details


def raw_response_diagnostic(raw_response: Any) -> Dict[str, Any]:
    """Expose compact response-shape evidence for failed live evaluations."""
    if not isinstance(raw_response, str) or not raw_response:
        return {"rawResponseCharacters": 0}
    diagnostic: Dict[str, Any] = {"rawResponseCharacters": len(raw_response)}
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError:
        diagnostic["rawResponseJson"] = "invalid"
        diagnostic["rawResponseTail"] = raw_response[-500:]
        return diagnostic
    choice = ((payload.get("choices") or [{}])[0]) if isinstance(payload, dict) else {}
    if not isinstance(choice, dict):
        diagnostic["rawResponseJson"] = "unexpected_shape"
        return diagnostic
    diagnostic["finishReason"] = choice.get("finish_reason")
    diagnostic["choiceFields"] = sorted(choice)
    message = choice.get("message")
    if isinstance(message, dict):
        diagnostic["messageFields"] = sorted(message)
    delta = choice.get("delta")
    if isinstance(delta, dict):
        diagnostic["deltaFields"] = sorted(delta)
    return diagnostic


def write_live_evaluation_report(output: Dict[str, Any], output_path: Optional[str]) -> None:
    if output_path:
        Path(output_path).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_co_create(_: argparse.Namespace) -> int:
    package = load_story_package(package_path_from_root())
    store = SessionStore(database_path())
    session = store.create_session(package)
    planner, evaluator, reviewer, label = create_cocreation_runtime()
    streams_body = os.environ.get("STORY_PLANNER", "mock").strip().lower() == "openai" and environment_bool("STORY_LLM_STREAM", True)
    service = CoCreationService(package, store, planner, evaluator, reviewer)
    contract, current = service.start(session["id"])
    print(f"\n{package['metadata']['title']} | 共创树开发试玩 {session['id']}")
    print("原著前史：" + " -> ".join(contract["canonicalPrefixNodeIds"]))
    print("当前使用 " + label + "；输入方向编号或自然语言方向继续。当前分支结束后，可输入 derive <后续目标> 创建独立衍生故事包；输入 state 查看当前状态，输入 history 查看分支，输入 audits 查看方向判定，输入 llm-audits 查看模型调用，输入 quit 退出。\n")
    if isinstance(planner, MockPlanner):
        print("提示：Mock Planner 仅验证状态流转、分支与存储契约。偏离原著后的结构测试草稿不代表真实模型正文，不能用于篇幅或可读性验收。\n")
    print_branch(package, current)
    try:
        while True:
            try:
                player_input = input("\n" + co_creation_input_prompt(current)).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\n已退出共创试玩。")
                break
            if not player_input:
                continue
            if player_input in ("quit", "exit"):
                break
            if player_input == "state":
                print_state(package, current); continue
            if player_input == "history":
                for item in store.branches(session["id"]):
                    print(f"#{item['sequence']} {item['canonicalRelation']} {item.get('selectedDirectionId', 'source_entry')} -> {item['summary']}")
                continue
            if player_input == "audits":
                for audit in store.direction_audits(session["id"]):
                    print(f"#{audit['id']} {audit['kind']} | {audit['playerDirection']} -> {audit.get('directionId', audit.get('message'))}")
                continue
            if player_input == "llm-audits":
                audits = store.llm_audits(session["id"]) + store.direction_evaluator_audits(session["id"])
                for audit in audits:
                    observations = audit.get("callObservations", [])
                    requests = sum(1 for item in observations if "transport" in item)
                    suffix = f" | {requests} 次模型请求" if requests else ""
                    if any(item.get("generationStage") == "continuation" for item in observations):
                        suffix += " | 含短稿续写"
                    rejected_characters = next(
                        (item["rejectedNarrativeCharacters"] for item in observations if "rejectedNarrativeCharacters" in item),
                        None,
                    )
                    if rejected_characters is not None:
                        suffix += f" | 被拒正文 {rejected_characters} 字"
                    print(f"#{audit['id']} {audit['operation']} | {audit['model']} | {audit.get('error') or 'ok'}{suffix}")
                    for item in observations:
                        if item.get("outcome") == "failed" and item.get("error"):
                            origin = "传输失败" if item.get("transport") or item.get("failureKind") == "transport_error" else "被本地拒绝"
                            print(f"  尝试 {item['attempt']} {origin}：{item['error']}")
                continue
            if player_input.startswith("derive "):
                try:
                    derived, current = service.begin_derivative(session["id"], current["id"], player_input[7:])
                    print(f"\n已创建独立衍生故事包：{derived['title']}\n来源固定为 {derived['sourcePackageRef']['id']}@{derived['sourcePackageRef']['version']}；原始故事包未改写。")
                    print_branch(package, current)
                except ValueError as error:
                    print(error)
                continue
            stream, stream_reset, has_output = paced_writer()
            def generation_status() -> None:
                if isinstance(planner, MockPlanner):
                    print("正在生成结构测试草稿（不代表模型正文质量）...")
                    return
                print(
                    "正在生成正文草稿（等待首段 SSE；无响应将自动切换 JSON）..."
                    if streams_body else "正在生成正文草稿..."
                )
            try:
                if player_input.isdigit():
                    print("\n正在确认剧情连续性...")
                    index = int(player_input) - 1
                    if index < 0 or index >= len(current["nextDirections"]):
                        print("没有这个剧情方向。请输入显示的编号。"); continue
                    selected = current["nextDirections"][index]
                    current = service.continue_direction(
                        session["id"], current["id"], selected["id"], "选择方向：" + selected["title"],
                        stream, stream_reset, generation_status,
                    )
                else:
                    print("\n正在判定自由方向...")
                    result = service.continue_free_text(
                        session["id"],
                        current["id"],
                        player_input,
                        stream,
                        stream_reset,
                        lambda evaluation: print("方向已确认：" + evaluation["rationale"]),
                        generation_status,
                    )
                    if result["kind"] != "accepted":
                        print(result["message"]); continue
                    current = result["node"]
                if has_output():
                    print("\n\n剧情已确认。")
                print_branch(package, current, has_output())
            except KeyboardInterrupt:
                print("\n本次生成已取消，未写入分支。")
            except Exception as error:
                print("\n草稿未通过校验，未写入分支：" + str(error))
    finally:
        store.close()
    return 0


def run_play(_: argparse.Namespace) -> int:
    package = load_story_package(package_path_from_root())
    store = SessionStore(database_path())
    fixed = os.environ.get("STORY_FIXED_ROLL")
    service = PlayerTurnService(package, store, int(fixed) if fixed else None)
    session = store.create_session(package)
    beat = next(item for item in package["story"]["narrativeGraph"]["beats"] if item["id"] == package["story"]["narrativeGraph"]["startBeatId"])
    directions = beat["nextDirections"]
    print(f"\n{package['metadata']['title']} | 开发试玩会话 {session['id']}\n输入方向编号，或直接描述下一步想做什么。输入 help 查看开发命令。\n\n{beat.get('sourceExcerpt', {}).get('text', beat['narrativeAnchor'])}")
    print_directions(directions)
    try:
        while True:
            try:
                player_input = input("\n行动 > ").strip()
            except EOFError:
                break
            if player_input in ("quit", "exit"):
                break
            if player_input == "history":
                for event in store.list_events(session["id"]): print(f"#{event['sequence']} {event['playerInput']} -> {event['resolution']['outcome']}")
                continue
            if player_input == "rebuild":
                rebuilt = store.rebuild_state(session["id"], package["initialState"])
                print("事件重建与当前快照一致。" if rebuilt == store.get_session(session["id"])["currentState"] else "事件重建与当前快照不一致。"); continue
            if player_input == "help":
                print("history 查看事件，rebuild 校验事件重建，quit 退出。"); continue
            result = service.play(session["id"], player_input)
            if result["kind"] == "clarification":
                print(result["message"]); continue
            print("\n" + result["narration"])
            directions = result["directions"]
            print_directions(directions)
            if result["resolution"].get("endingId"):
                ending = next(item for item in package["story"]["endings"] if item["id"] == result["resolution"]["endingId"])
                print("本局结束：" + ending["title"]); break
    finally:
        store.close()
    return 0


def run_validate(_: argparse.Namespace) -> int:
    package = load_story_package(package_path_from_root())
    print(f"故事包校验通过：{package['id']}@{package['version']}")
    return 0


def run_evaluate_live(args: argparse.Namespace) -> int:
    if not environment_bool("STORY_LIVE_EVALUATION", False):
        raise ValueError("真实模型评估需显式设置 STORY_LIVE_EVALUATION=1")
    if os.environ.get("STORY_PLANNER") != "openai":
        raise ValueError("Python 真实模型评估需设置 STORY_PLANNER=openai")
    max_calls = int(os.environ.get("STORY_LIVE_EVALUATION_MAX_CALLS", "8"))
    if not 1 <= max_calls <= 20:
        raise ValueError("STORY_LIVE_EVALUATION_MAX_CALLS 必须是 1 至 20 的整数")
    scenarios = [item for item in LIVE_EVALUATION_SCENARIOS if args.scenario in (None, item.identifier)]
    if not scenarios:
        available = "、".join(item.identifier for item in LIVE_EVALUATION_SCENARIOS)
        raise ValueError("未知真实模型评估场景: " + args.scenario + "；可用场景：" + available)
    package = load_story_package(package_path_from_root())
    output: Dict[str, Any] = {
        "runStatus": "incomplete",
        "package": package["id"] + "@" + package["version"],
        "transport": {
            "responseMode": "json",
            "timeoutSeconds": 60,
            "fallbackEnabled": False,
            "reasoningEffort": environment_reasoning_effort(),
        },
        "maxCalls": max_calls,
        "totalModelCalls": 0,
        "results": [],
        "conclusion": {"status": "incomplete", "statement": "Python 真实模型验收尚未完成。"},
    }
    write_live_evaluation_report(output, args.output)
    planner: Optional[LlmPlanner] = None
    evaluator: Optional[DirectionEvaluator] = None
    reviewer: NarrativeReviewer = NarrativeReviewer()
    try:
        for scenario in scenarios:
            if scenario.needs_live_model and output["totalModelCalls"] >= max_calls:
                output["results"].append({"id": scenario.identifier, "status": "not_run", "checks": scenario.checks, "error": "已达到 STORY_LIVE_EVALUATION_MAX_CALLS 调用上限"})
                write_live_evaluation_report(output, args.output)
                continue
            if scenario.needs_live_model and planner is None:
                planner, evaluator, reviewer, label = create_live_evaluation_runtime()
                output["model"] = label
            try:
                scenario_planner = planner if scenario.needs_live_model else MockPlanner()
                scenario_evaluator = evaluator if scenario.needs_live_model else DirectionEvaluator()
                assert scenario_planner is not None and scenario_evaluator is not None
                calls = transport_call_details(scenario.run(package, scenario_planner, scenario_evaluator, reviewer))
                result: Dict[str, Any] = {"id": scenario.identifier, "status": "passed", "checks": scenario.checks, "modelCalls": len(calls), "calls": calls}
            except Exception as error:
                audits = getattr(error, "evaluation_audits", None)
                audit = getattr(error, "audit", None)
                if not isinstance(audits, list):
                    audits = [audit] if isinstance(audit, dict) else []
                calls = transport_call_details(audits)
                result = {"id": scenario.identifier, "status": "failed", "checks": scenario.checks, "error": str(error), "modelCalls": len(calls), "calls": calls}
                if isinstance(error, LlmError) and isinstance(audit, dict):
                    diagnostic: Dict[str, Any] = {"model": audit.get("model"), "plannerError": audit.get("error")}
                    if audit.get("rejectedNarrativeCharacters") is not None:
                        diagnostic["rejectedNarrativeCharacters"] = audit["rejectedNarrativeCharacters"]
                    diagnostic.update(raw_response_diagnostic(audit.get("rawResponse")))
                    result["diagnostic"] = diagnostic
            output["results"].append(result)
            output["totalModelCalls"] += result["modelCalls"]
            write_live_evaluation_report(output, args.output)
    except KeyboardInterrupt:
        output["runStatus"] = "incomplete"
        output["conclusion"] = {"status": "incomplete", "statement": "Python 真实模型验收被中断；已完成场景的结果已保留。"}
        write_live_evaluation_report(output, args.output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 130
    all_passed = len(output["results"]) == len(scenarios) and all(item["status"] == "passed" for item in output["results"])
    output["runStatus"] = "completed" if all_passed and output["totalModelCalls"] <= max_calls else "failed"
    output["conclusion"] = {"status": "passed" if output["runStatus"] == "completed" else "failed", "statement": "本次 Python 真实模型验收通过。" if output["runStatus"] == "completed" else "真实模型验收未满足场景断言或调用上限。"}
    write_live_evaluation_report(output, args.output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["conclusion"]["status"] == "passed" else 1


def main(argv: Optional[list[str]] = None) -> int:
    load_env_file(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(prog="open-story")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("play").set_defaults(handler=run_play)
    commands.add_parser("co-create").set_defaults(handler=run_co_create)
    commands.add_parser("validate").set_defaults(handler=run_validate)
    evaluation = commands.add_parser("evaluate-live")
    evaluation.add_argument("--scenario")
    evaluation.add_argument("--output")
    evaluation.set_defaults(handler=run_evaluate_live)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
