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

from .cocreation import CoCreationService, DirectionEvaluator, LlmNarrativeReviewer, LlmPlanner, MockPlanner, NarrativeReviewer, chapter_title_for_direction, entry_initial_state, entry_points_for_selection, entry_source_characters, location_name, new_character_profile_fields, normalize_entry_selection, script_generated_package, state_character_locations
from .content import load_runtime_story_package, load_story_package, package_path_from_root, reader_path_from_package_path
from .module_context import ModuleContextResolver
from .authoring import StoryAuthoringError, compile_entry_model
from .package_builder import StoryPackageBuildError, analyze_standard_novel, audit_story_package, audit_story_package_modules, build_source_reader, build_story_package, build_story_package_modules, read_story_package_modules, write_story_package_modules
from .environment import load_env_file
from .llm import LlmError, OpenAICompatibleGateway
from .play import PlayerTurnService
from .source import SourceNovelError, draft_entry_review, inspect_standard_novel
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


def load_source_reader(package_path: Path, package: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Load the optional local reader sidecar without exposing it to planners."""
    reader_path = reader_path_from_package_path(package_path)
    if not reader_path.is_file():
        return None
    try:
        reader = json.loads(reader_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("无法读取本地章节阅读器：" + str(error)) from error
    if (
        reader.get("schemaVersion") != "source-reader/0.1"
        or reader.get("package") != {"id": package["id"], "version": package["version"]}
        or reader.get("source", {}).get("sha256") != package.get("sourceAnalysis", {}).get("sha256")
        or not isinstance(reader.get("chapters"), list)
    ):
        raise ValueError("本地章节阅读器与当前 StoryPackage 不匹配。请重新运行 build-story-package。")
    return reader


def print_entry_chapter(reader: Optional[Dict[str, Any]], contract: Dict[str, Any]) -> None:
    if reader is None:
        print("\n[当前故事包未附带本地章节阅读器；不会读取或发送母本 TXT。]\n")
        return
    chapter_id = contract.get("entrySourceChapterId")
    chapter = next((item for item in reader["chapters"] if item.get("id") == chapter_id), None)
    if chapter is None:
        print("\n[本地章节阅读器未找到所选章节；不会读取或发送母本 TXT。]\n")
        return
    print("\n原著章节：《" + chapter["title"] + "》\n\n" + chapter["text"].strip() + "\n")


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


def print_branch(
    package: Dict[str, Any], node: Dict[str, Any], narrative_shown: bool = False,
    chapter_heading_shown: bool = False,
) -> None:
    arc = node.get("storyArc")
    entry_chapter = node.get("entryChapter", {})
    if not chapter_heading_shown and not arc and entry_chapter.get("title"):
        print(f"章节：{entry_chapter['title']}\n")
    if arc:
        chapter = arc.get("chapter", {})
        if not chapter_heading_shown and chapter.get("title"):
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
        if reason == "fact_review" and not visible[0]:
            sys.stdout.write("\n正在核对正文与已确认事实...\n")
            sys.stdout.flush()
        elif reason == "semantic_repair" and not visible[0]:
            sys.stdout.write("\n正文存在事实冲突，正在修复（最多一次）...\n")
            sys.stdout.flush()
        elif reason == "transport_fallback" and not visible[0]:
            sys.stdout.write("\n[SSE 在首段正文返回前超时，改用兼容 JSON 响应继续生成。]\n")
            sys.stdout.flush()
        elif reason == "json_transport_fallback" and not visible[0]:
            sys.stdout.write("\n[JSON 在首段正文返回前未响应，改用 SSE 流式生成。]\n")
            sys.stdout.flush()
        elif visible[0]:
            messages = {
                "transport_fallback": "流式响应中断，改用兼容 JSON 响应继续生成。",
                "semantic_repair": "草稿触发连续性守卫，正按已确认事实修复。",
            }
            message = messages.get(reason, "草稿未通过校验。")
            sys.stdout.write("\n\n[草稿未采纳：" + message + "]\n")
            sys.stdout.flush()
        visible[0] = False
    return write, reset, lambda: visible[0]


def confirm_chapter_draft(draft: Dict[str, Any], narrative_already_shown: bool) -> str:
    """Let the player revise a generated chapter before the branch is persisted."""
    narrative = draft.get("narrativeText")
    if not isinstance(narrative, str) or not narrative.strip():
        raise ValueError("没有可确认的章节草稿")
    candidate = narrative.strip()
    shown = narrative_already_shown
    while True:
        if not shown:
            print("\n章节草稿：\n\n" + candidate + "\n")
            shown = True
        command = input("草稿（confirm / append / replace / cancel）> ").strip().lower()
        if command == "confirm":
            return candidate
        if command == "append":
            addition = input("追加正文 > ").strip()
            if addition:
                candidate += "\n\n" + addition
            else:
                print("追加正文不能为空。")
            continue
        if command == "replace":
            print("输入完整替换正文；单独一行 . 结束：")
            lines: List[str] = []
            while True:
                line = input()
                if line == ".":
                    break
                lines.append(line)
            replacement = "\n".join(lines).strip()
            if replacement:
                candidate = replacement
                shown = False
            else:
                print("替换正文不能为空。")
            continue
        if command == "cancel":
            raise ValueError("草稿未确认，未写入分支")
        print("请输入 confirm、append、replace 或 cancel。")


def is_draft_confirmation_command(value: str) -> bool:
    """Keep draft-only commands from becoming free-text story directions."""
    return value.strip().lower() in ("confirm", "append", "replace", "cancel")


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
    allow_transport_fallback = environment_bool("STORY_LLM_TRANSPORT_FALLBACK", True)
    reasoning_effort = environment_reasoning_effort()
    planner = LlmPlanner(
        OpenAICompatibleGateway(
            required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
            stream, timeout, max_tokens, allow_transport_fallback=allow_transport_fallback,
            reasoning_effort=reasoning_effort,
        ),
        minimum_narrative_characters=2000,
        verify_source_facts=True,
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
    planner = LlmPlanner(OpenAICompatibleGateway(*gateway_args), minimum_narrative_characters=2000, verify_source_facts=True)
    evaluator = DirectionEvaluator()
    return planner, evaluator, NarrativeReviewer(), f"LLM Planner（{required['STORY_LLM_MODEL']}，纯正文，JSON 传输，60 秒，无传输降级）+ 本地方向判定"


@dataclass(frozen=True)
class LiveEvaluationScenario:
    identifier: str
    checks: List[str]
    needs_live_model: bool
    run: Callable[[Dict[str, Any], Any, Any, NarrativeReviewer], List[Dict[str, Any]]]


def _start_evaluation_service(
    package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer,
    selection: Optional[Dict[str, Any]] = None,
) -> tuple[SessionStore, Dict[str, Any], CoCreationService, Dict[str, Any]]:
    store = SessionStore(":memory:")
    initial_state = entry_initial_state(package, selection) if selection is not None else None
    session = store.create_session(package, initial_state=initial_state)
    service = CoCreationService(package, store, planner, evaluator, reviewer)
    _, root = service.start(session["id"], selection)
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


def _scripted_source_selection(package: Dict[str, Any]) -> Dict[str, Any]:
    characters = entry_source_characters(package)
    if not characters:
        raise ValueError("脚本生成包没有可选原著角色")
    selection = {"kind": "source_character", "sourceCharacterId": characters[0]["id"]}
    entries = entry_points_for_selection(package, selection)
    if not entries:
        raise ValueError("脚本生成包没有该角色可进入的剧情节点")
    return normalize_entry_selection(package, {**selection, "entryPointId": entries[0]["id"]})


def _scripted_new_character_selection(package: Dict[str, Any]) -> Dict[str, Any]:
    values = {
        "name": "林舟", "gender": "女", "age": 26, "occupation": "记者",
        "sourceRelationship": "与原著角色曾共同整理档案",
        "background": "调查城市公共工程事故的自由记者，因收到求助来到临潮站。",
    }
    profile = {
        field["id"]: values[field["id"]]
        for field in new_character_profile_fields(package)
        if field["id"] in values
    }
    selection: Dict[str, Any] = {"kind": "new_character", "profile": profile}
    entries = entry_points_for_selection(package, selection)
    if not entries:
        raise ValueError("脚本生成包没有新角色可进入的剧情节点")
    return normalize_entry_selection(package, {**selection, "entryPointId": entries[0]["id"]})


def _scripted_phase_parent(
    package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer,
) -> tuple[SessionStore, Dict[str, Any], CoCreationService, Dict[str, Any], Dict[str, Any]]:
    selection = _scripted_source_selection(package)
    store, session, service, root = _start_evaluation_service(package, planner, evaluator, reviewer, selection)
    macro_direction = root["nextDirections"][0]
    _assert_evaluation(macro_direction.get("directionLevel") == "arc", "脚本生成包入口没有公布大方向")
    macro = service.continue_direction(
        session["id"], root["id"], macro_direction["id"], "选择大方向：" + macro_direction["title"],
    )
    _assert_evaluation(bool(macro["nextDirections"]), "大方向没有公布本章细化方向")
    _assert_evaluation(macro["nextDirections"][0].get("directionLevel") == "phase", "大方向未进入细化方向菜单")
    return store, session, service, root, macro


def _scenario_modular_lazy_catalog(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    beats = package["story"]["narrativeGraph"]["beats"]
    _assert_evaluation(hasattr(beats, "loaded_ids"), "模块化运行时未保留按需剧情节点索引")
    selection = _scripted_source_selection(package)
    store, session, _service, _root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer, selection)
    try:
        _assert_evaluation(0 < len(beats.loaded_ids) < len(beats), "进入菜单时加载了全部剧情节点索引")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_scripted_source_entry(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    selection = _scripted_source_selection(package)
    store, session, _service, root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer, selection)
    try:
        entry = next(item for item in entry_points_for_selection(package, selection) if item["id"] == selection["entryPointId"])
        _assert_evaluation(root["entryChapter"].get("sourceChapterId") == entry["sourceChapterId"], "原著角色入口未绑定阅读章节")
        _assert_evaluation(root["canonicalRelation"] in ("on_line", "diverged"), "原著角色入口没有声明连续性关系")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_scripted_new_character_entry(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    selection = _scripted_new_character_selection(package)
    store, session, _service, root = _start_evaluation_service(package, MockPlanner(), DirectionEvaluator(), reviewer, selection)
    try:
        _assert_evaluation(root["canonicalRelation"] == "diverged", "新角色入口未从母本连续性边界分叉")
        _assert_evaluation(root["branchState"]["storyScope"] == "source", "新角色入口错误进入衍生故事状态")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


def _scenario_modular_context_current_chapter(package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, _root, macro = _scripted_phase_parent(package, planner, evaluator, reviewer)
    try:
        selected = macro["nextDirections"][0]
        node = service.continue_direction(
            session["id"], macro["id"], selected["id"], "选择方向：" + selected["title"],
        )
        audits = _scenario_audits(store, session["id"])
        audit = next((item for item in audits if item.get("operation") == "branch_planner"), None)
        prompt_context = audit.get("promptContext", {}) if isinstance(audit, dict) else {}
        _assert_evaluation(prompt_context.get("mode") == "modules", "真实 Planner 没有使用模块化上下文")
        paths = prompt_context.get("modulePaths", [])
        _assert_evaluation(isinstance(paths, list) and paths and not any(str(path).startswith("reader/") for path in paths), "模块化上下文加载了阅读器或没有记录模块路径")
        _assert_evaluation(node["branchState"]["sourceProgress"] == selected["statePatch"]["sourceProgress"], "当前章节状态没有按细化方向推进")
        return audits
    except Exception as error:
        error.evaluation_audits = _scenario_audits(store, session["id"])
        raise
    finally:
        store.close()


def _scenario_scripted_chapter_switch(package: Dict[str, Any], planner: Any, evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, _root, macro = _scripted_phase_parent(package, planner, evaluator, reviewer)
    try:
        first_direction = macro["nextDirections"][0]
        first = service.continue_direction(
            session["id"], macro["id"], first_direction["id"], "选择方向：" + first_direction["title"],
        )
        second_parent = first
        second_direction = second_parent["nextDirections"][0]
        if second_direction.get("directionLevel") == "arc":
            second_parent = service.continue_direction(
                session["id"], second_parent["id"], second_direction["id"], "选择大方向：" + second_direction["title"],
            )
            second_direction = second_parent["nextDirections"][0]
        second = service.continue_direction(
            session["id"], second_parent["id"], second_direction["id"], "选择方向：" + second_direction["title"],
        )
        _assert_evaluation(first["storyArc"]["chapter"]["title"] != second["storyArc"]["chapter"]["title"], "章节切换后标题没有变化")
        _assert_evaluation(first["branchState"]["sourceProgress"] != second["branchState"]["sourceProgress"], "章节切换后状态没有连续推进")
        _assert_evaluation(len(second["branchState"].get("branchLedger", {}).get("entries", [])) > len(first["branchState"].get("branchLedger", {}).get("entries", [])), "章节切换没有追加状态账本")
        state = second["branchState"]
        player_id = state.get("playerCharacterId")
        expected_location = second_direction["statePatch"].get("playerLocationId")
        _assert_evaluation(player_id and state["playerLocationId"] == expected_location
                           and state.get("characterLocationIds", {}).get(player_id) == expected_location,
                           "章节切换后玩家位置与角色位置不一致")
        return _scenario_audits(store, session["id"])
    except Exception as error:
        error.evaluation_audits = _scenario_audits(store, session["id"])
        raise
    finally:
        store.close()


def _scenario_scripted_request_idempotency(package: Dict[str, Any], _planner: Any, _evaluator: Any, reviewer: NarrativeReviewer) -> List[Dict[str, Any]]:
    store, session, service, _root, macro = _scripted_phase_parent(package, MockPlanner(), DirectionEvaluator(), reviewer)
    try:
        selected = macro["nextDirections"][0]
        request_id = "modular-live-evaluation-idempotency"
        first = service.continue_direction(
            session["id"], macro["id"], selected["id"], "选择方向：" + selected["title"], request_id=request_id,
        )
        before = len(store.branches(session["id"]))
        second = service.continue_direction(
            session["id"], macro["id"], selected["id"], "选择方向：" + selected["title"], request_id=request_id,
        )
        _assert_evaluation(first["id"] == second["id"], "重复 requestId 没有返回原分支")
        _assert_evaluation(len(store.branches(session["id"])) == before, "重复 requestId 追加了新分支")
        return _scenario_audits(store, session["id"])
    finally:
        store.close()


MODULAR_LIVE_EVALUATION_SCENARIOS = (
    LiveEvaluationScenario("modular_catalogs_are_lazy", ["菜单只读取总清单，不加载全部剧情节点索引"], False, _scenario_modular_lazy_catalog),
    LiveEvaluationScenario("source_character_entry", ["原著角色入口绑定当前阅读章节与连续性关系"], False, _scenario_scripted_source_entry),
    LiveEvaluationScenario("new_character_entry", ["新角色入口独立分叉且保留原故事状态范围"], False, _scenario_scripted_new_character_entry),
    LiveEvaluationScenario("module_context_current_chapter", ["真实 Planner 只装载当前章节模块上下文，不读取 reader"], True, _scenario_modular_context_current_chapter),
    LiveEvaluationScenario("chapter_switch_state_continuity", ["真实 Planner 跨章节保持标题、状态和账本连续性"], True, _scenario_scripted_chapter_switch),
    LiveEvaluationScenario("request_idempotency", ["重复 requestId 不重复生成或追加分支"], False, _scenario_scripted_request_idempotency),
)


def live_evaluation_scenarios(package: Dict[str, Any]) -> tuple[LiveEvaluationScenario, ...]:
    return MODULAR_LIVE_EVALUATION_SCENARIOS if script_generated_package(package) else LIVE_EVALUATION_SCENARIOS


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


def choose_co_creation_entry(package: Dict[str, Any]) -> Dict[str, Any]:
    """Console adapter for the package-declared entry choices.

    This is intentionally only a CLI adapter. Product UI can use the same
    `entry_source_characters` and `entry_points_for_selection` data directly.
    """
    while True:
        characters = entry_source_characters(package)
        print("\n选择进入身份：")
        for index, character in enumerate(characters, start=1):
            description = character.get("menuDescription", character.get("description", ""))
            print(f"{index}. {character['name']}：{description}")
        new_index = len(characters) + 1
        entry_model = package.get("story", {}).get("entryModel")
        new_enabled = isinstance(entry_model, dict) and entry_model.get("newCharacter", {}).get("enabled")
        if new_enabled:
            fields = new_character_profile_fields(package)
            labels = "、".join(field["label"] for field in fields)
            print(f"{new_index}. 新建角色（需填写：{labels}）")
        raw = input("身份 > ").strip()
        try:
            selected_index = int(raw)
        except ValueError:
            print("请输入身份编号。")
            continue
        if 1 <= selected_index <= len(characters):
            selection: Dict[str, Any] = {"kind": "source_character", "sourceCharacterId": characters[selected_index - 1]["id"]}
        elif new_enabled and selected_index == new_index:
            fields = new_character_profile_fields(package)
            if fields:
                print("请填写新角色档案：")
                profile = {field["id"]: input(field["label"] + " > ").strip() for field in fields}
                selection = {"kind": "new_character", "profile": profile}
            else:
                selection = {"kind": "new_character", "name": input("新角色名 > ").strip()}
        else:
            print("身份编号不存在。")
            continue
        entries = entry_points_for_selection(package, selection)
        if (entry_model or {}).get("policy") == "official_unknown_reader/1":
            return normalize_entry_selection(package, selection)
        print("\n选择进入的关键剧情节点：")
        for index, entry in enumerate(entries, start=1):
            print(f"{index}. {entry['chapterTitle']} | {entry['title']}：{entry['summary']}")
        raw = input("剧情节点 > ").strip()
        try:
            selected_index = int(raw)
        except ValueError:
            print("请输入剧情节点编号。")
            continue
        if not 1 <= selected_index <= len(entries):
            print("剧情节点编号不存在。")
            continue
        try:
            return normalize_entry_selection(package, {**selection, "entryPointId": entries[selected_index - 1]["id"]})
        except ValueError as error:
            print(str(error))


def run_co_create(_: argparse.Namespace) -> int:
    package_path = package_path_from_root()
    package = load_runtime_story_package(package_path, lazy=True)
    reader = load_source_reader(package_path, package)
    store = SessionStore(database_path())
    selection = choose_co_creation_entry(package)
    session = store.create_session(package, initial_state=entry_initial_state(package, selection))
    planner, evaluator, reviewer, label = create_cocreation_runtime()
    if isinstance(planner, LlmPlanner):
        resolver = ModuleContextResolver.for_package(package_path, package)
        if resolver is not None:
            planner.context_resolver = resolver
            label += "（模块索引按需上下文）"
    streams_body = os.environ.get("STORY_PLANNER", "mock").strip().lower() == "openai" and environment_bool("STORY_LLM_STREAM", True)
    service = CoCreationService(package, store, planner, evaluator, reviewer)
    contract, current = service.start(session["id"], selection)
    print(f"\n{package['metadata']['title']} | 共创树开发试玩 {session['id']}")
    print("进入身份：" + contract["persona"]["name"] + " | 节点：" + contract["entryChapterTitle"])
    print("原著前史：" + " -> ".join(contract["canonicalPrefixNodeIds"]))
    print_entry_chapter(reader, contract)
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
            if is_draft_confirmation_command(player_input):
                print("草稿命令仅在生成成功后的草稿确认提示中有效；当前没有可确认的草稿。请选择方向或输入自定义行动。")
                continue
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
                    if any(item.get("generationStage") in ("continuation", "expansion", "semantic_repair_continuation", "semantic_repair_expansion") for item in observations):
                        suffix += " | 含篇幅补足"
                    if any(item.get("generationStage") in ("fact_review", "semantic_repair_fact_review") for item in observations):
                        suffix += " | 含正文事实核对"
                    rejected_characters = next(
                        (item["rejectedNarrativeCharacters"] for item in observations if "rejectedNarrativeCharacters" in item),
                        None,
                    )
                    if rejected_characters is not None:
                        suffix += f" | 被拒正文 {rejected_characters} 字"
                    prompt_context = audit.get("promptContext", {})
                    if prompt_context.get("mode") == "modules":
                        suffix += " | 模块按需上下文：" + str(prompt_context.get("currentChapterId", "unknown"))
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
            pending_chapter_title: Optional[str] = None
            chapter_heading_shown = False

            def set_pending_chapter_title(direction: Dict[str, Any]) -> None:
                nonlocal pending_chapter_title
                title = direction.get("title")
                if isinstance(title, str) and title.strip():
                    pending_chapter_title = chapter_title_for_direction(package, direction)

            def generation_status() -> None:
                nonlocal chapter_heading_shown
                if pending_chapter_title and not chapter_heading_shown:
                    print("\n章节：" + pending_chapter_title + "\n")
                    chapter_heading_shown = True
                if isinstance(planner, MockPlanner):
                    print("正在生成结构测试草稿（不代表模型正文质量）...")
                    return
                print(
                    "正在生成正文草稿（等待首段 SSE；无响应将自动切换 JSON）..."
                    if streams_body else "正在生成正文草稿..."
                )

            def edit_and_confirm_draft(draft: Dict[str, Any]) -> str:
                return confirm_chapter_draft(draft, has_output())

            try:
                if player_input.isdigit():
                    print("\n正在确认剧情连续性...")
                    index = int(player_input) - 1
                    if index < 0 or index >= len(current["nextDirections"]):
                        print("没有这个剧情方向。请输入显示的编号。"); continue
                    selected = current["nextDirections"][index]
                    set_pending_chapter_title(selected)
                    current = service.continue_direction(
                        session["id"], current["id"], selected["id"], "选择方向：" + selected["title"],
                        stream, stream_reset, generation_status, draft_editor=edit_and_confirm_draft,
                    )
                else:
                    print("\n正在判定自由方向...")

                    def direction_accepted(evaluation: Dict[str, Any]) -> None:
                        print("方向已确认：" + evaluation["rationale"])
                        set_pending_chapter_title(
                            evaluation.get("direction") or next(
                                (item for item in current["nextDirections"] if item["id"] == evaluation["directionId"]),
                                {},
                            )
                        )

                    result = service.continue_free_text(
                        session["id"],
                        current["id"],
                        player_input,
                        stream,
                        stream_reset,
                        direction_accepted,
                        generation_status,
                        draft_editor=edit_and_confirm_draft,
                    )
                    if result["kind"] != "accepted":
                        print(result["message"]); continue
                    current = result["node"]
                if has_output():
                    print("\n\n剧情已确认。")
                print_branch(package, current, has_output(), chapter_heading_shown)
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


def run_inspect_source(args: argparse.Namespace) -> int:
    manifest = inspect_standard_novel(Path(args.input))
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"待审核清单已写入：{args.output}")
    else:
        print(rendered, end="")
    return 0


def run_compile_entry_model(args: argparse.Namespace) -> int:
    package_path = Path(args.package)
    review_path = Path(args.review)
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        review = json.loads(review_path.read_text(encoding="utf-8"))
        compiled = compile_entry_model(package, review)
    except (OSError, json.JSONDecodeError, StoryAuthoringError) as error:
        raise ValueError("无法编译入口故事包：" + str(error)) from error
    Path(args.output).write_text(json.dumps(compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"入口模型已编译并校验：{args.output}")
    return 0


def run_draft_entry_review(args: argparse.Namespace) -> int:
    package_path = Path(args.package)
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        draft = draft_entry_review(Path(args.input), package, args.minimum_mentions)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("无法提取入口候选：" + str(error)) from error
    Path(args.output).write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"入口候选已写入待审核文件：{args.output}")
    return 0


def run_analyze_source(args: argparse.Namespace) -> int:
    try:
        analysis = analyze_standard_novel(Path(args.input), args.maximum_fragment_characters)
    except (OSError, SourceNovelError, StoryPackageBuildError) as error:
        raise ValueError("无法完成小说母本语义分析：" + str(error)) from error
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"本地章节语义候选已写入：{args.output}（{len(analysis['chapters'])} 章；待脚本构建与审计）")
    return 0


def run_build_story_package(args: argparse.Namespace) -> int:
    try:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
        package = build_story_package(Path(args.input), analysis, args.package_id, args.version)
        review_path = getattr(args, "opening_review", None)
        if review_path:
            from .official_openings import apply_opening_review
            review = json.loads(Path(review_path).read_text(encoding="utf-8"))
            package = apply_opening_review(package, Path(args.input), review)
        audit = audit_story_package(Path(args.input), analysis, package)
        reader = build_source_reader(Path(args.input), analysis, package)
        modules = build_story_package_modules(Path(args.input), analysis, package, reader)
        module_audit = audit_story_package_modules(Path(args.input), analysis, package, reader, modules)
    except (OSError, json.JSONDecodeError, SourceNovelError, StoryPackageBuildError, ValueError) as error:
        raise ValueError("无法构建 StoryPackage：" + str(error)) from error
    if audit["status"] != "passed":
        raise ValueError("StoryPackage 构建后的完整性审计失败：" + "；".join(item["message"] for item in audit["issues"]))
    if module_audit["status"] != "passed":
        raise ValueError("StoryPackage 模块构建后的完整性审计失败：" + "；".join(item["message"] for item in module_audit["issues"]))
    package_output = Path(args.output)
    if package_output.name != "package.json":
        raise ValueError("StoryPackage 输出文件必须命名为 package.json，并置于 <package-id>/<version>/ 目录。")
    package_output.parent.mkdir(parents=True, exist_ok=True)
    package_output.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if review_path:
        package_output.with_name("opening-review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    analysis_output = Path(args.analysis_output) if args.analysis_output else package_output.with_name("analysis.json")
    analysis_output.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    reader_output = Path(args.reader_output) if args.reader_output else reader_path_from_package_path(package_output)
    reader_output.write_text(json.dumps(reader, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    modules_output = Path(args.modules_output) if args.modules_output else package_output.with_name("modules")
    write_story_package_modules(modules_output, modules)
    module_audit_output = Path(args.module_audit_output) if args.module_audit_output else modules_output / "module-audit.json"
    module_audit_output.write_text(json.dumps(module_audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_output = Path(args.audit_output) if args.audit_output else package_output.with_name("audit.json")
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"StoryPackage 已构建并通过完整性审计：{package_output}；语义候选：{analysis_output}；本地章节阅读器：{reader_output}；模块索引：{modules_output / 'package-index.json'}")
    return 0


def run_audit_story_package(args: argparse.Namespace) -> int:
    try:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
        package = json.loads(Path(args.package).read_text(encoding="utf-8"))
        audit = audit_story_package(Path(args.input), analysis, package)
    except (OSError, json.JSONDecodeError, SourceNovelError, StoryPackageBuildError, ValueError) as error:
        raise ValueError("无法审计 StoryPackage：" + str(error)) from error
    rendered = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if audit["status"] == "passed" else 1


def run_audit_story_package_modules(args: argparse.Namespace) -> int:
    try:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
        package_path = Path(args.package)
        package = json.loads(package_path.read_text(encoding="utf-8"))
        reader_path = Path(args.reader) if args.reader else reader_path_from_package_path(package_path)
        reader = json.loads(reader_path.read_text(encoding="utf-8"))
        modules = read_story_package_modules(Path(args.modules_directory))
        audit = audit_story_package_modules(Path(args.input), analysis, package, reader, modules)
    except (OSError, json.JSONDecodeError, SourceNovelError, StoryPackageBuildError, ValueError) as error:
        raise ValueError("无法审计 StoryPackage 模块：" + str(error)) from error
    rendered = json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if audit["status"] == "passed" else 1


def run_evaluate_live(args: argparse.Namespace) -> int:
    if not environment_bool("STORY_LIVE_EVALUATION", False):
        raise ValueError("真实模型评估需显式设置 STORY_LIVE_EVALUATION=1")
    if os.environ.get("STORY_PLANNER") != "openai":
        raise ValueError("Python 真实模型评估需设置 STORY_PLANNER=openai")
    max_calls = int(os.environ.get("STORY_LIVE_EVALUATION_MAX_CALLS", "8"))
    if not 1 <= max_calls <= 20:
        raise ValueError("STORY_LIVE_EVALUATION_MAX_CALLS 必须是 1 至 20 的整数")
    package_path = package_path_from_root()
    package = load_runtime_story_package(package_path, lazy=True)
    registered_scenarios = live_evaluation_scenarios(package)
    scenarios = [item for item in registered_scenarios if args.scenario in (None, item.identifier)]
    if not scenarios:
        available = "、".join(item.identifier for item in registered_scenarios)
        raise ValueError("未知真实模型评估场景: " + args.scenario + "；可用场景：" + available)
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
                if isinstance(planner, LlmPlanner):
                    planner.gateway.remaining_calls = max_calls
                resolver = ModuleContextResolver.for_package(package_path, package)
                if resolver is not None:
                    planner.context_resolver = resolver
                    label += "（模块按需上下文）"
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
    source = commands.add_parser("inspect-source", help="检查标准 TXT 小说母本并输出待审核清单")
    source.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    source.add_argument("--output", help="待审核清单 JSON 的输出路径")
    source.set_defaults(handler=run_inspect_source)
    entry_draft = commands.add_parser("draft-entry-review", help="从 TXT 与基础故事包提取待审核的入口候选")
    entry_draft.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    entry_draft.add_argument("package", help="基础 StoryPackage JSON")
    entry_draft.add_argument("--minimum-mentions", type=int, default=2, help="推荐重要角色的最少出现次数")
    entry_draft.add_argument("--output", required=True, help="待审核入口候选 JSON 的输出路径")
    entry_draft.set_defaults(handler=run_draft_entry_review)
    compiler = commands.add_parser("compile-entry-model", help="将审核通过的入口候选编译为 StoryPackage")
    compiler.add_argument("package", help="待完善的 StoryPackage JSON")
    compiler.add_argument("review", help="审核通过的入口候选 JSON")
    compiler.add_argument("--output", required=True, help="新 StoryPackage JSON 的输出路径")
    compiler.set_defaults(handler=run_compile_entry_model)
    analysis = commands.add_parser("analyze-source", help="按受限章节片段提取带原文证据的语义候选")
    analysis.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    analysis.add_argument("--maximum-fragment-characters", type=int, default=12000, help="每个本地处理片段允许的最大原文字符数")
    analysis.add_argument("--output", required=True, help="语义候选 JSON 的输出路径")
    analysis.set_defaults(handler=run_analyze_source)
    builder = commands.add_parser("build-story-package", help="将语义候选编译为完整并经审计的 StoryPackage")
    builder.add_argument("--opening-review", help="与冻结母本绑定的官方人物开局审核 JSON")
    builder.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    builder.add_argument("analysis", help="analyze-source 产生的语义候选 JSON")
    builder.add_argument("--id", dest="package_id", required=True, help="目标 StoryPackage 的 kebab-case 标识")
    builder.add_argument("--version", required=True, help="目标 StoryPackage 版本，例如 0.1.0")
    builder.add_argument("--output", required=True, help="版本目录中的 package.json 输出路径")
    builder.add_argument("--analysis-output", help="语义候选 JSON 输出路径，默认写入故事包同目录的 analysis.json")
    builder.add_argument("--reader-output", help="本地章节阅读器 JSON 输出路径，默认写入故事包同目录的 reader.json")
    builder.add_argument("--modules-output", help="脚本生成的模块目录，默认写入故事包同目录的 modules/")
    builder.add_argument("--module-audit-output", help="模块完整性审计 JSON 输出路径，默认写入模块目录")
    builder.add_argument("--audit-output", help="完整性审计 JSON 输出路径，默认写入故事包同目录的 audit.json")
    builder.set_defaults(handler=run_build_story_package)
    package_audit = commands.add_parser("audit-story-package", help="审计 TXT、语义候选与 StoryPackage 的完整性关系")
    package_audit.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    package_audit.add_argument("analysis", help="语义候选 JSON")
    package_audit.add_argument("package", help="待审计 StoryPackage JSON")
    package_audit.add_argument("--output", help="审计报告 JSON 的输出路径")
    package_audit.set_defaults(handler=run_audit_story_package)
    module_audit = commands.add_parser("audit-story-package-modules", help="审计脚本生成的 StoryPackage 模块目录")
    module_audit.add_argument("input", help="UTF-8 编码的 .txt 小说母本")
    module_audit.add_argument("analysis", help="analyze-source 产生的语义候选 JSON")
    module_audit.add_argument("package", help="StoryPackage JSON")
    module_audit.add_argument("modules_directory", help="模块目录")
    module_audit.add_argument("--reader", help="本地章节阅读器 JSON，默认读取故事包同目录的 reader.json")
    module_audit.add_argument("--output", help="模块审计报告 JSON 的输出路径")
    module_audit.set_defaults(handler=run_audit_story_package_modules)
    evaluation = commands.add_parser("evaluate-live")
    evaluation.add_argument("--scenario")
    evaluation.add_argument("--output")
    evaluation.set_defaults(handler=run_evaluate_live)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
