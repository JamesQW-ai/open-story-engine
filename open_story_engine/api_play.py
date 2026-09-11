"""Play-mode write service: session start and turn continuation over the engine core.

Enabled only when the app is created in play mode. Reads ``.env`` from the
repository root so the model configuration matches the CLI; all generation,
direction validation and state patching still go through ``CoCreationService``
and its guards. Every write is serialized behind app-level locks because the
LLM planner keeps mutable transport state.
"""

from __future__ import annotations

import os
import threading
import uuid
import json
import hashlib
import copy
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

from .api_read import ReadError, ReadService
from .api_openings import identity_opening_package
from .api_journey import preferences, save_preferences, journey, player_directions
from .api_narrative import PlayerNarrativePlanner, player_package
from .api_profiles import profile_evidence, summarize_profile
from .cocreation import (
    CoCreationService,
    DirectionEvaluator,
    LlmNarrativeReviewer,
    LlmPlanner,
    MockPlanner,
    NarrativeReviewer,
    entry_initial_state,
    create_contract,
    entry_node,
    normalize_entry_selection,
)
from .environment import load_env_file
from .llm import LlmError, OpenAICompatibleGateway
from .module_context import ModuleContextResolver
from .storage import SessionStore


def _env_bool(name: str, fallback: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return fallback
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return False
    raise ValueError(f"{name} 仅支持 true、false、1 或 0")


def _env_integer(name: str, fallback: int, minimum: int, maximum: int) -> int:
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


def _env_reasoning_effort() -> Optional[str]:
    value = os.environ.get("STORY_LLM_REASONING_EFFORT", "").strip().lower()
    if not value:
        return None
    if value not in ("none", "minimal", "low", "medium", "high"):
        raise ValueError("STORY_LLM_REASONING_EFFORT 仅支持 none、minimal、low、medium 或 high")
    return value


class PlayService:
    """Coordinates writable sessions against the fixed StoryPackage set."""

    def __init__(self, read: ReadService, repository_root: Path) -> None:
        self.read = read
        self.database_path = read.database_path
        load_env_file(repository_root / ".env")
        self.mode = os.environ.get("STORY_PLANNER", "mock").strip().lower()
        self._lock = threading.Lock()
        self._profile_cache = OrderedDict()
        self._profile_lock = threading.Lock()
        self._planner_error: Optional[str] = None
        self._planner: Any = None
        self._evaluator: Any = None
        self._reviewer: NarrativeReviewer = NarrativeReviewer()
        try:
            self._build_runtime()
        except ValueError as error:
            self._planner_error = str(error)

    @property
    def generation_available(self) -> bool:
        return self._planner_error is None

    def character_profile(self, sid, bid, character_id):
        journal = self.read.journey(sid, bid)
        card = next((p for p in journal['people'] if p['id'] == character_id), None)
        if card is None:
            raise ReadError(404, 'character_not_known', '这段故事中还未认识这个人物')
        with self.read.store() as store:
            evidence = profile_evidence(store.lineage(sid, bid))
        key = (sid, bid, character_id, hashlib.sha256(evidence.encode()).hexdigest())
        with self._profile_lock:
            if key in self._profile_cache:
                self._profile_cache.move_to_end(key)
                return self._profile_cache[key]
            gateway = getattr(self._planner, 'gateway', None)
            if gateway is None:
                return card
            # Separate mutable transport counters from the ongoing story planner.
            try:
                result = summarize_profile(copy.copy(gateway), card, evidence, journal['role_name'])
            except LlmError as error:
                raise ReadError(503, 'profile_unavailable', '人物概况暂未整理完成，请重试。') from error
            self._profile_cache[key] = result
            if len(self._profile_cache) > 128:
                self._profile_cache.popitem(last=False)
            return result

    def _build_runtime(self) -> None:
        if self.mode == "mock":
            self._planner = MockPlanner()
            self._evaluator = DirectionEvaluator()
            return
        if self.mode != "openai":
            raise ValueError("STORY_PLANNER 仅支持 mock 或 openai")
        required = {key: os.environ.get(key, "").strip() for key in ("STORY_LLM_BASE_URL", "STORY_LLM_API_KEY", "STORY_LLM_MODEL")}
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError("真实模型写作缺少配置：" + "、".join(missing))
        stream = _env_bool("STORY_LLM_STREAM", True)
        timeout = _env_integer("STORY_LLM_TIMEOUT_SECONDS", 30, 5, 120)
        max_tokens = _env_integer("STORY_LLM_MAX_TOKENS", 8192, 1024, 8192)
        allow_fallback = _env_bool("STORY_LLM_TRANSPORT_FALLBACK", True)
        reasoning_effort = _env_reasoning_effort()
        self._planner = PlayerNarrativePlanner(
            OpenAICompatibleGateway(
                required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
                stream, timeout, max_tokens, allow_transport_fallback=allow_fallback,
                reasoning_effort=reasoning_effort,
            ),
            minimum_narrative_characters=0,
            verify_source_facts=False,
            concise=True,
        )
        self._evaluator = DirectionEvaluator()
        if _env_bool("STORY_LLM_QUALITY_REVIEW", False):
            self._reviewer = LlmNarrativeReviewer(
                OpenAICompatibleGateway(
                    required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
                    False, timeout, reasoning_effort=reasoning_effort,
                )
            )

    def delete_session(self, session_id: str) -> None:
        """Serialize deletion with generation so a completed turn cannot revive a save."""
        with self._lock:
            if not self.database_path.is_file():
                raise ReadError(404, "session_not_found", "存档不存在")
            store = SessionStore(str(self.database_path))
            try:
                if not store.delete_session(session_id):
                    raise ReadError(404, "session_not_found", "存档不存在")
            finally:
                store.close()

    def rename_session(self, session_id, title):
        title = title.strip()
        if not title or len(title) > 80:
            raise ReadError(422, 'invalid_title', '存档名称需要1—80个字符')
        with self._lock:
            self.read.session_view(session_id, False)
            store = SessionStore(str(self.database_path))
            try:
                save_preferences(store, session_id, title=title)
            finally:
                store.close()
        return {'title': title}

    def end_route(self, session_id, branch_id):
        with self._lock:
            self.read.branch_view(session_id, branch_id)
            store = SessionStore(str(self.database_path))
            try:
                if store.connection.execute('SELECT 1 FROM branch_nodes WHERE session_id=? AND parent_id=? LIMIT 1', (session_id, branch_id)).fetchone():
                    raise ReadError(409, 'route_has_continuation', '这里已有后续故事，请在路线末尾结束。')
                saved = preferences(store, session_id)
                saved['ended'][branch_id] = 'abandoned'
                save_preferences(store, session_id, ended=saved['ended'])
            finally:
                store.close()
        return {'status': 'abandoned'}

    def _service(self, package_path: Path, package: dict[str, Any], store: SessionStore) -> CoCreationService:
        planner = self._planner
        if isinstance(planner, LlmPlanner):
            resolver = ModuleContextResolver.for_package(package_path, package)
            if resolver is not None:
                planner.context_resolver = resolver
        return CoCreationService(package, store, planner, self._evaluator, self._reviewer)

    def _require_planner(self) -> None:
        if self._planner_error is not None:
            raise ReadError(503, "planner_unavailable", self._planner_error + "；请检查 .env 后重启写作服务")

    def create_session(
        self,
        package_id: str,
        version: str,
        entry_point_id: str,
        source_character_id: Optional[str] = None,
        new_character: Optional[dict[str, Any]] = None,
        identity_opening: bool = False,
        request_id: Optional[str] = None,
        stream: Optional[Callable[[str], None]] = None,
        stream_reset: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        path, package = self.read.load_package(package_id, version)
        if (source_character_id is None) == (new_character is None):
            raise ReadError(422, "invalid_request", "source_character_id 与 new_character 必须且只能提供一个")
        try:
            if new_character is not None:
                selection = normalize_entry_selection(package, {
                    "kind": "new_character",
                    "profile": dict(new_character),
                    "entryPointId": entry_point_id,
                })
            else:
                # Web 玩法玩家可自由选择任意原著角色 × 任意剧情入口；
                # 包内每个入口声明的角色仅为引导建议。
                selection = normalize_entry_selection(package, {
                    "kind": "source_character",
                    "sourceCharacterId": source_character_id,
                    "entryPointId": entry_point_id,
                }, allow_any_source_character=True)
        except ValueError as error:
            raise ReadError(409, "invalid_entry", str(error)) from error
        if identity_opening:
            self._require_planner()
            package = identity_opening_package(package, selection)
            package = player_package(package, source_character_id)
        signature = hashlib.sha256(json.dumps([package_id, version, selection, identity_opening],
                                             sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'open-story:start:' + request_id)) if request_id else str(uuid.uuid4())
        with self._lock:
            store = SessionStore(str(self.database_path))
            try:
                if store.connection.execute('SELECT 1 FROM game_sessions WHERE id=?', (sid,)).fetchone():
                    if store.contract(sid).get('webStartSignature') != signature:
                        raise ReadError(409, 'request_conflict', '同一请求不能用于不同的身份或小说')
                    return {'session': store.get_session(sid), 'branch': store.branches(sid)[0]}
                contract = create_contract(package, sid, selection, allow_any_source_character=new_character is None)
                root = entry_node(package, contract, allow_any_source_character=new_character is None)
                if identity_opening:
                    root['nextDirections'] = player_directions(package, root['branchState'])
                if identity_opening and isinstance(self._planner, PlayerNarrativePlanner):
                    try:
                        root['narrativeText'] = self._planner.opening(package, root, contract['persona']['name'], stream, stream_reset)
                    except (LlmError, ValueError) as error:
                        raise ReadError(503, 'generation_failed', '开场未能完成，请重试。') from error
                elif stream:
                    for paragraph in root['narrativeText'].split('\n\n'):
                        stream(paragraph + '\n\n')
                contract['webStartSignature'] = signature
                session = store.create_session(
                    package,
                    session_id=sid,
                    initial_state=entry_initial_state(
                        package, selection, allow_any_source_character=new_character is None,
                    ),
                )
                store.save_contract(contract)
                base_title = f"《{package['metadata']['title']}》· {contract['persona']['name']}"
                titles = {row[0] for row in store.connection.execute('SELECT title FROM session_play_preferences')}
                title, number = base_title, 2
                while title in titles:
                    title = f'{base_title} · {number}'
                    number += 1
                save_preferences(store, sid, title=title)
                root = store.create_branch_root(sid, root)
            finally:
                store.close()
        return {"session": session, "branch": root}

    def continue_turn(
        self,
        session_id: str,
        parent_branch_id: str,
        direction_id: Optional[str] = None,
        text: Optional[str] = None,
        request_id: Optional[str] = None,
        stream: Optional[Callable[[str], None]] = None,
        stream_reset: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        self._require_planner()
        if (direction_id is None) == (text is None):
            raise ReadError(422, "invalid_request", "direction_id 与 text 必须且只能提供一个")
        if text is not None and not text.strip():
            raise ReadError(422, "invalid_request", "自由行动不能为空")
        request_id = request_id or "http-" + uuid.uuid4().hex

        with self._lock:
            store = SessionStore(str(self.database_path))
            try:
                try:
                    session = store.get_session(session_id)
                except ValueError as error:
                    raise ReadError(404, "session_not_found", "会话不存在") from error
                try:
                    path, package = self.read.load_package(
                        session["storyPackageId"], session["storyPackageVersion"],
                    )
                except ReadError as error:
                    raise ReadError(409, "package_binding_changed", "会话绑定的故事包当前不可用") from error
                persona = store.contract(session_id)['persona']
                package = player_package(package, persona.get('sourceCharacterId'))
                self._evaluator.package = package
                service = self._service(path, package, store)
                pre_existing = (
                    store.find_branch_request(session_id, request_id) is not None
                    or store.find_direction_request(session_id, request_id) is not None
                )
                try:
                    route_status = journey(store, session_id, parent_branch_id, package)['status']
                except ValueError as error:
                    raise ReadError(404, 'branch_not_found', '该会话中不存在指定分支') from error
                if not pre_existing and route_status != 'active':
                    raise ReadError(409, 'route_ended', '这条路线已收尾，可以回到更早的选择重新尝试。')
                try:
                    if text is not None:
                        outcome = service.continue_free_text(session_id, parent_branch_id, text.strip(), request_id=request_id, stream=stream, stream_reset=stream_reset)
                    else:
                        node = service.continue_direction(session_id, parent_branch_id, direction_id, request_id=request_id, stream=stream, stream_reset=stream_reset)
                        outcome = {"kind": "accepted", "node": node}
                except ValueError as error:
                    raise ReadError(409, "play_rejected", str(error)) from error
                except LlmError as error:
                    raise ReadError(503, "generation_failed", "正文生成失败，未写入任何进度：" + str(error)) from error

                if outcome.get("kind") != "accepted":
                    return {
                        "status": "rejected", "request_id": request_id, "deduplicated": pre_existing,
                        "branch": None, "kind": outcome.get("kind"),
                        "reason": outcome.get("message") or outcome.get("rationale") or "该行动未被接受",
                    }
                return {
                    "status": "written", "request_id": request_id, "deduplicated": pre_existing,
                    "branch": outcome["node"], "kind": None, "reason": outcome.get("rationale"),
                }
            finally:
                store.close()
