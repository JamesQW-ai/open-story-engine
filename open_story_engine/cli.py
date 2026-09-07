"""CLI entry points for normal play, co-creation, validation and live evaluation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .cocreation import CoCreationService, DirectionEvaluator, LlmDirectionEvaluator, LlmNarrativeReviewer, LlmPlanner, MockPlanner, NarrativeReviewer, location_name
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


def database_path() -> str:
    return os.environ.get("STORY_DATABASE_PATH", str(Path("data") / "open-story-engine.sqlite"))


def print_directions(directions: list[Dict[str, Any]], label: str = "可能的剧情方向") -> None:
    if not directions:
        return
    print("\n" + label + "：")
    for index, direction in enumerate(directions, start=1):
        print(f"{index}. {direction['title']}：{direction['summary']}")
    print("   自定义方向：直接描述许川下一步想推动的剧情或行动。")


def print_branch(package: Dict[str, Any], node: Dict[str, Any], narrative_shown: bool = False) -> None:
    arc = node.get("storyArc")
    if arc:
        print(f"章节：{arc['chapter']['title']}\n主线目标：{arc['activeGoal']}\n当前阶段：{arc['currentPhase']}\n")
    if not narrative_shown:
        print(node["narrativeText"])
    if arc and arc["chapter"]["status"] == "complete":
        print("\n本章完：" + arc["chapter"]["title"])
    print_directions(node["nextDirections"])


def print_state(package: Dict[str, Any], node: Dict[str, Any]) -> None:
    state = node["branchState"]
    labels = {"missing": "失联", "located": "已定位", "rescued": "已获救", "unsecured": "未取得", "secured": "已保全", "rising": "上涨中", "lowered": "已降低", "locked": "锁闭", "opened": "已打开", "pending_release": "等待放行", "held": "已阻止放行", "departed": "已离站"}
    print("\n许川：" + location_name(package, state))
    print("唐栖：" + labels[state["tangStatus"]] + "，" + location_name(package, {**state, "playerLocationId": state["tangLocationId"]}))
    print("姜序：" + location_name(package, {**state, "playerLocationId": state["jiangLocationId"]}))
    print(f"证据：{labels[state['evidenceStatus']]} | 水位：{labels[state['waterLevel']]} | 信号室：{labels[state['signalRoomStatus']]} | 列车：{labels[state['trainStatus']]}")
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
        return MockPlanner(), DirectionEvaluator(), NarrativeReviewer(), "Mock Planner + Direction Evaluator"
    if mode != "openai":
        raise ValueError("STORY_PLANNER 仅支持 mock 或 openai")
    required = {key: os.environ.get(key, "").strip() for key in ("STORY_LLM_BASE_URL", "STORY_LLM_API_KEY", "STORY_LLM_MODEL")}
    if not all(required.values()):
        raise ValueError("使用 STORY_PLANNER=openai 时必须设置 STORY_LLM_BASE_URL、STORY_LLM_API_KEY 与 STORY_LLM_MODEL")
    stream = environment_bool("STORY_LLM_STREAM", True)
    timeout = environment_integer("STORY_LLM_TIMEOUT_SECONDS", 30, 5, 120)
    max_tokens = environment_integer("STORY_LLM_MAX_TOKENS", 4096, 1024, 8192)
    planner = LlmPlanner(
        OpenAICompatibleGateway(required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"], stream, timeout, max_tokens),
        minimum_narrative_characters=2000,
    )
    evaluator = LlmDirectionEvaluator(OpenAICompatibleGateway(required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"], False, timeout, max_tokens))
    reviewer: NarrativeReviewer = NarrativeReviewer()
    if environment_bool("STORY_LLM_QUALITY_REVIEW", False):
        reviewer = LlmNarrativeReviewer(OpenAICompatibleGateway(required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"], False, timeout))
    return planner, evaluator, reviewer, f"LLM Planner + Direction Evaluator ({required['STORY_LLM_MODEL']}, 正文 {'SSE' if stream else 'JSON'} / 方向 JSON)"


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
    print_branch(package, current)
    try:
        while True:
            try:
                player_input = input("\n方向 > ").strip()
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
    print(f"\n{package['metadata']['title']} | 开发试玩会话 {session['id']}\n输入方向编号，或直接描述许川下一步想做什么。输入 help 查看开发命令。\n\n{beat.get('sourceExcerpt', {}).get('text', beat['narrativeAnchor'])}")
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
    max_calls = int(os.environ.get("STORY_LIVE_EVALUATION_MAX_CALLS", "4"))
    if not 1 <= max_calls <= 20:
        raise ValueError("STORY_LIVE_EVALUATION_MAX_CALLS 必须是 1 至 20 的整数")
    package = load_story_package(package_path_from_root())
    store = SessionStore(":memory:")
    session = store.create_session(package)
    planner, evaluator, reviewer, label = create_cocreation_runtime()
    service = CoCreationService(package, store, planner, evaluator, reviewer)
    _, root = service.start(session["id"])
    try:
        token = service.continue_direction(session["id"], root["id"], "direction_find_token", "选择方向：追查十七号柜")
        result = service.continue_free_text(session["id"], token["id"], "先让姜序带路去积水尽头确认唐栖的情况，在救援中查清事故真相，并阻止列车放行。")
        if result.get("kind") != "accepted":
            raise ValueError(result.get("message", "真实模型未接受宽泛目标"))
        node = result["node"]
        audits = store.llm_audits(session["id"]) + store.direction_evaluator_audits(session["id"])
        calls = sum(len(audit.get("callObservations", [])) or 1 for audit in audits)
        passed = node["branchState"]["tangStatus"] == "located" and node["branchState"]["playerLocationId"] == "location_signal_tunnel" and node["storyArc"]["currentPhase"]
        output = {"runStatus": "completed" if passed and calls <= max_calls else "failed", "package": package["id"] + "@" + package["version"], "model": label, "maxCalls": max_calls, "totalModelCalls": calls, "results": [{"id": args.scenario or "broad_goal_starts_current_phase", "status": "passed" if passed else "failed", "checks": ["宽泛目标锚定为救援优先", "主线以当前阶段开始且正文通过状态校验"], "modelCalls": calls}], "conclusion": {"status": "passed" if passed and calls <= max_calls else "failed", "statement": "本次 Python 真实模型验收通过。" if passed and calls <= max_calls else "真实模型验收未满足调用上限或状态断言。"}}
    except Exception as error:
        diagnostic: Dict[str, Any] = {}
        audit = getattr(error, "audit", None)
        if isinstance(error, LlmError) and isinstance(audit, dict):
            diagnostic = {
                "model": audit.get("model"),
                "plannerError": audit.get("error"),
                "modelCalls": len(audit.get("callObservations", [])),
            }
            if audit.get("rejectedNarrativeCharacters") is not None:
                diagnostic["rejectedNarrativeCharacters"] = audit["rejectedNarrativeCharacters"]
        result: Dict[str, Any] = {"id": args.scenario or "broad_goal_starts_current_phase", "status": "failed", "error": str(error)}
        if diagnostic:
            result["diagnostic"] = diagnostic
        output = {"runStatus": "failed", "results": [result], "conclusion": {"status": "failed", "statement": "Python 真实模型验收失败。"}}
    finally:
        store.close()
    if args.output:
        Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
