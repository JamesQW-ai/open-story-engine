"""Play-mode write service: session start and turn continuation over the engine core.

Enabled only when the app is created in play mode. Reads ``.env`` from the
repository root so the model configuration matches the CLI; all generation,
direction validation and state patching still go through ``CoCreationService``
and its guards. Turn generation uses private runtimes and deferred stores;
only validated, selected turns acquire the commit lock.
"""

from __future__ import annotations

import os
import threading
import uuid
import json
import hashlib
import copy
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Optional

from .api_read import ReadError, ReadService
from .api_openings import identity_opening_package
from .api_journey import preferences, save_preferences, journey, player_directions, player_beat
from .api_narrative import MIN_SCENE_CJK, PlayerNarrativePlanner, player_package
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
from .storage import SessionStore, now
from .api_turn_drafts import TurnDrafts, TurnSnapshot, visible_choices, digest, reported_usage, RULES_VERSION


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


def _stream_text(text: str, stream: Optional[Callable[[str], None]], chunk_size: int = 96) -> None:
    """Emit saved or authored prose in the same incremental shape as live prose."""
    if not stream:
        return
    for start in range(0, len(text), chunk_size):
        stream(text[start:start + chunk_size])


class PlayService:
    """Coordinates writable sessions against the fixed StoryPackage set."""

    def __init__(self, read: ReadService, repository_root: Path) -> None:
        self.read = read
        self.database_path = read.database_path
        load_env_file(repository_root / ".env")
        self.mode = os.environ.get("STORY_PLANNER", "mock").strip().lower()
        self._lock = threading.Lock()
        self._profile_cache = OrderedDict()
        self._turn_context_cache = OrderedDict()
        self._turn_context_lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self.drafts = TurnDrafts(self.database_path.with_suffix(".turn-drafts.sqlite"), self._generate_turn)
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
        first_delta_timeout = _env_integer("STORY_LLM_FIRST_DELTA_TIMEOUT_SECONDS", 30, 5, 120)
        max_tokens = _env_integer("STORY_LLM_MAX_TOKENS", 8192, 1024, 8192)
        allow_fallback = _env_bool("STORY_LLM_TRANSPORT_FALLBACK", True)
        reasoning_effort = _env_reasoning_effort()
        self._planner = PlayerNarrativePlanner(
            OpenAICompatibleGateway(
                required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
                stream, timeout, max_tokens, allow_transport_fallback=allow_fallback,
                reasoning_effort=reasoning_effort,
                first_delta_timeout_seconds=first_delta_timeout,
            ),
            minimum_narrative_characters=MIN_SCENE_CJK,
            verify_source_facts=True,
            concise=True,
        )
        self._evaluator = DirectionEvaluator()
        if _env_bool("STORY_LLM_QUALITY_REVIEW", False):
            self._reviewer = LlmNarrativeReviewer(
                OpenAICompatibleGateway(
                    required["STORY_LLM_BASE_URL"], required["STORY_LLM_API_KEY"], required["STORY_LLM_MODEL"],
                    False, timeout, reasoning_effort=reasoning_effort,
                    first_delta_timeout_seconds=first_delta_timeout,
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
                self.drafts.discard_session(session_id)
                with self._turn_context_lock:
                    for key, snapshot in list(self._turn_context_cache.items()):
                        if snapshot.session['id'] == session_id:
                            self._turn_context_cache.pop(key)
            finally:
                store.close()

    def record_display(self, session_id, branch_id):
        # Resolve the binding from the authoritative branch, not a client draft ID.
        with self._lock:
            with self.read.store() as store:
                branch = self.read.branch(store, session_id, branch_id)
            key = branch.get('preparedTurnKey')
            if not key or not branch.get('narrativeText', '').strip():
                return dict(recorded=False, reason='measurement_unavailable')
            return self.drafts.record_display(session_id, branch_id, branch.get('parentId'), key, branch.get('preparedAttemptId'))

    def turn_usage(self, session_id):
        with self._lock:
            with self.read.store() as store:
                try:
                    store.get_session(session_id)
                except ValueError as error:
                    raise ReadError(404, 'session_not_found', '存档不存在') from error
            return self.drafts.usage_summary(session_id)

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

    def _closing_context(self, store, session_id, branch_id):
        from .api_journey import route_status
        session = self.read.session(store, session_id)
        self.read.branch_state(self.read.branch(store, session_id, branch_id))
        _, package = self.read.load_package(session['storyPackageId'], session['storyPackageVersion'])
        try:
            store.assert_session_package(session_id, package)
            nodes = store.lineage(session_id, branch_id)
            status = route_status(store, session_id, nodes, package)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ReadError(409, 'route_history_unavailable', '路线记录或故事包绑定无效') from error
        if store.connection.execute('SELECT 1 FROM branch_nodes WHERE session_id=? AND parent_id=? LIMIT 1', (session_id, branch_id)).fetchone():
            raise ReadError(409, 'route_has_continuation', '这里已有后续故事，请在路线末尾操作。')
        return package, nodes, status

    def plan_route_closure(self, session_id, branch_id, intended_type):
        from .route_closure import preparation
        from .route_lifecycle import MODES, records, save_record, view
        if intended_type not in (*MODES, None):
            raise ReadError(422, 'invalid_ending_type', '收束意图仅支持正常、偏离、失败或撤销')
        with self._lock:
            self.read.branch_view(session_id, branch_id)
            store = SessionStore(str(self.database_path))
            try:
                with store.connection:
                    store.connection.execute('BEGIN IMMEDIATE')
                    package, nodes, status = self._closing_context(store, session_id, branch_id)
                    if status != 'active':
                        raise ReadError(409, 'route_ended', '这条路线已结束，不能重新进入收束。')
                    try:
                        result = preparation(package, store.contract(session_id), nodes, status)
                        record = records(store, session_id, nodes).get(branch_id, {})
                        changed = 'intent' not in record or record['intent'] != intended_type or 'revision' not in record
                        record['intent'] = intended_type
                        if changed:
                            record['revision'] = uuid.uuid4().hex
                        save_record(store, session_id, branch_id, record)
                        current_closing_digest = digest(dict(intended_type=intended_type, branch_id=branch_id, revision=record['revision']))
                        result['lifecycle'] = view(store, session_id, nodes, result)
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        raise ReadError(409, 'route_history_unavailable', '路线记录无效，无法登记收束意图') from error
                if changed:
                    self.drafts.discard_parent(session_id, branch_id, closing_digest=current_closing_digest)
                return result
            finally:
                store.close()

    def propose_ending(self, session_id, branch_id, request_id, outcome_summary, ending_quote):
        from .route_endings import propose
        return propose(self, session_id, branch_id, request_id, outcome_summary, ending_quote)

    def commit_ending(self, session_id, branch_id, proposal_id):
        from .route_endings import commit
        return commit(self, session_id, branch_id, proposal_id)

    def cancel_ending(self, session_id, branch_id, proposal_id):
        from .route_endings import cancel
        return cancel(self, session_id, branch_id, proposal_id)

    def end_route(self, session_id, branch_id):
        from .route_closure import preparation
        from .route_lifecycle import early_receipt, records, save_record
        with self._lock:
            self.read.branch_view(session_id, branch_id)
            store = SessionStore(str(self.database_path))
            try:
                with store.connection:
                    # Share SQLite's write lock with turn commit across services.
                    store.connection.execute('BEGIN IMMEDIATE')
                    package, nodes, status = self._closing_context(store, session_id, branch_id)
                    if status not in ('active', 'abandoned'):
                        raise ReadError(409, 'route_ended', '这条路线已结束，不能改为提前结束。')
                    if status == 'active':
                        try:
                            closure = preparation(package, store.contract(session_id), nodes, status)
                        except (ValueError, TypeError, KeyError, AttributeError):
                            # Early exit remains available when a ledger is
                            # unreadable; unknown is never a cleared checklist.
                            closure = dict(readiness='unknown', coverage=dict(goals='unknown', threads='unknown', state='unknown'),
                                           outstanding=[], cleared=[])
                        record = records(store, session_id, nodes).get(branch_id, {})
                        record['receipt'] = early_receipt(branch_id, closure)
                        save_record(store, session_id, branch_id, record)
                        saved = preferences(store, session_id)
                        saved['ended'][branch_id] = 'abandoned'
                        save_preferences(store, session_id, ended=saved['ended'])
                self.drafts.discard_parent(session_id, branch_id)
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
        if package['story'].get('entryModel', {}).get('policy') != 'official_unknown_reader/1':
            raise ReadError(409, 'package_retired', '这本故事已停用，请选择当前官方长篇。')
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
                # Official packages enforce their role/default-entry binding in
                # the shared resolver, including callers using this legacy flag.
                selection = normalize_entry_selection(package, {
                    "kind": "source_character",
                    "sourceCharacterId": source_character_id,
                    "entryPointId": entry_point_id,
                }, allow_any_source_character=True)
        except ValueError as error:
            raise ReadError(409, "invalid_entry", str(error)) from error
        if identity_opening:
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
                # Official longform entries already carry an authored opening
                # for each selectable source character.  Keep that exact
                # snapshot in the player flow; identity_opening means "start
                # this identity", not "rewrite its opening with the LLM".
                entry = next(
                    item for item in package['story']['entryModel']['entryPoints']
                    if item['id'] == selection['entryPointId']
                )
                authored_opening = (
                    selection.get('kind') == 'source_character'
                    and isinstance(entry.get('sourceCharacterNarratives'), dict)
                    and bool(entry['sourceCharacterNarratives'].get(selection.get('sourceCharacterId')))
                )
                if identity_opening and not authored_opening:
                    self._require_planner()
                    root['nextDirections'] = player_directions(package, root['branchState'])
                if identity_opening and authored_opening:
                    if stream:
                        _stream_text(root['narrativeText'], stream)
                elif identity_opening and isinstance(self._planner, PlayerNarrativePlanner):
                    try:
                        root['narrativeText'] = self._planner.opening(package, root, contract['persona']['name'], stream, stream_reset)
                    except (LlmError, ValueError) as error:
                        code = getattr(error, 'code', 'validation_error')
                        logging.getLogger(__name__).warning('Opening failed [%s]: %s', code,
                            str(error) if code in ('model_output_rejected', 'validation_error') else 'provider request failed')
                        raise ReadError(503, 'generation_failed', '开场未能完成，请重试。') from error
                elif stream:
                    _stream_text(root['narrativeText'], stream)
                contract['webStartSignature'] = signature
                from .route_outline import build_outline
                try:
                    root['routeOutline'] = build_outline(package, contract, [root])
                except (ValueError, TypeError, KeyError, AttributeError) as error:
                    raise ReadError(409, 'play_rejected', '开局局部大纲记录无效，未创建存档') from error
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

    @staticmethod
    def _validate_history_id(history_id):
        if history_id is not None and (not isinstance(history_id, str) or not history_id.strip() or len(history_id) > 200):
            raise ReadError(422, 'invalid_request', '重选历史标识无效')

    def _turn_snapshot(self, sid, bid, history_id=None):
        self._validate_history_id(history_id)
        self._require_planner()
        with self.read.store() as store:
            try:
                session = store.get_session(sid)
                contract = store.contract(sid)
                lineage = store.lineage(sid, bid)
            except ValueError as error:
                raise ReadError(404, 'branch_not_found', '会话或分支不存在') from error
            path, package = self.read.load_package(session['storyPackageId'], session['storyPackageVersion'])
            store.assert_session_package(sid, package)
            if package['story'].get('entryModel', {}).get('policy') != 'official_unknown_reader/1':
                raise ReadError(409, 'package_retired', '这本故事已停用，请选择当前官方长篇。')
            if journey(store, sid, bid, package)['status'] != 'active':
                raise ReadError(409, 'route_ended', '这条路线已收尾，可以回到更早的选择重新尝试。')
            derived = store.derived(sid)
            try:
                closing_intent = store.closing_intent(sid, bid)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '收束意图记录无效，无法继续') from error
        parent = lineage[-1]
        binding = dict(session_id=sid, parent_branch_id=bid, package_id=package['id'],
                       package_version=package['version'], rules_version=RULES_VERSION,
                       model=getattr(getattr(self._planner, 'gateway', None), 'model', self.mode),
                       closing_digest=digest(closing_intent),
                       parent_digest=digest([parent, contract, derived, session.get('storyPackageModuleIndexSha256')]))
        # Reuse immutable source material/history for both choices and an
        # eventual custom action. Recheck package binding and state above first.
        cache_key = digest(binding)
        with self._turn_context_lock:
            if cache_key in self._turn_context_cache:
                snapshot = self._turn_context_cache[cache_key]
                self._turn_context_cache.move_to_end(cache_key)
            else:
                snapshot = TurnSnapshot(package, session, contract, lineage, derived, closing_intent)
                snapshot.path = path
                self._turn_context_cache[cache_key] = snapshot
                if len(self._turn_context_cache) > 16:
                    self._turn_context_cache.popitem(last=False)
        # Explicit rechoosing shares immutable context, but not old prepared
        # results or saved branches. Polls and retries retain the same history.
        if history_id is not None:
            binding = dict(binding, history_id=history_id)
        return binding, snapshot

    def prepare_choices(self, sid, bid, subscriber, history_id=None):
        from .scene_library import SceneLibrary
        binding, snapshot = self._turn_snapshot(sid, bid, history_id)
        choices = []
        for choice in visible_choices(snapshot.history[-1], snapshot.package, snapshot.story_contract, snapshot.history):
            choice_binding = dict(binding, choice_id=choice['id'])
            # Saved results outlive disposable drafts. Check under the same
            # lock as selection and route ending before scheduling any work.
            with self._lock:
                with self.read.store() as store:
                    if journey(store, sid, bid, snapshot.package)['status'] != 'active':
                        raise ReadError(409, 'route_ended', '这条路线已收尾，可以回到更早的选择重新尝试。')
                    self._check_closing_binding(store, binding)
                    saved = self._prepared_branch(store, sid, digest(choice_binding))
                if saved:
                    view = dict(draft_id=digest(choice_binding), status='ready', metrics={'source': 'saved_branch'})
                    node = saved
                else:
                    job = self.drafts.ensure(choice_binding, choice['payload'], snapshot, subscriber=subscriber)
                    view = self.drafts.view(job)
                    node = job.get('artifact', {}).get('node', {})
            result = {k: choice[k] for k in ('id', 'title', 'summary')} | view
            if result['status'] == 'ready':
                art = SceneLibrary().resolve(snapshot.package['id'], snapshot.package['version'], node)
                if art:
                    result['image_prefetch_url'] = art['url']
            choices.append(result)
        return dict(parent_branch_id=bid, choices=choices)

    def _ensure_turn(self, binding, payload, snapshot, **kwargs):
        # End-route and enqueue share a lock: a snapshot obtained before ending
        # must not restart a discarded job afterwards.
        with self._lock:
            with self.read.store() as store:
                if journey(store, binding['session_id'], binding['parent_branch_id'], snapshot.package)['status'] != 'active':
                    raise ReadError(409, 'route_ended', '这条路线已收尾，可以回到更早的选择重新尝试。')
                self._check_closing_binding(store, binding)
            return self.drafts.ensure(binding, payload, snapshot, **kwargs)

    @staticmethod
    def _check_closing_binding(store, binding):
        try:
            current = store.closing_intent(binding['session_id'], binding['parent_branch_id'])
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ReadError(409, 'route_history_unavailable', '收束意图记录无效，无法继续') from error
        if binding.get('closing_digest') != digest(current):
            raise ReadError(409, 'draft_expired', '收束意图已变化，请重新选择方向。')

    def _generate_turn(self, source, payload, stream, reset, validating, check):
        # Each worker gets independent planner, gateway, evaluator, lazy modules
        # and mutable state. Frozen history is shared only as input to deepcopy.
        snapshot = copy.deepcopy(source)
        snapshot.on_validating = validating
        package = player_package(snapshot.package, snapshot.story_contract['persona'].get('sourceCharacterId'))
        planner = copy.deepcopy(self._planner)
        evaluator = copy.deepcopy(self._evaluator)
        reviewer = copy.deepcopy(self._reviewer)
        evaluator.package = package
        if isinstance(planner, LlmPlanner):
            planner.context_resolver = ModuleContextResolver.for_package(snapshot.path, package)
            # Check cancellation before each provider call, including JSON
            # validation calls that do not emit prose deltas.
            for component in (planner, reviewer, evaluator):
                gateway = getattr(component, 'gateway', None)
                if gateway is not None:
                    for name in ('complete_text', 'complete_json'):
                        method = getattr(gateway, name)
                        def guarded(*args, _method=method, _name=name, **kwargs):
                            check()
                            if _name == 'complete_json':
                                validating()
                            snapshot.usage['calls'] += 1
                            try:
                                completion = _method(*args, **kwargs)
                            except Exception as error:
                                tokens, incomplete = reported_usage(error)
                                snapshot.usage['reported_tokens'] += tokens
                                snapshot.usage['unreported_calls'] += int(incomplete)
                                raise
                            tokens, incomplete = reported_usage(completion)
                            snapshot.usage['reported_tokens'] += tokens
                            snapshot.usage['unreported_calls'] += int(incomplete)
                            return completion
                        setattr(gateway, name, guarded)
        service = CoCreationService(package, snapshot, planner, evaluator, reviewer)
        sid, bid = snapshot.session['id'], snapshot.history[-1]['id']
        try:
            if 'text' in payload:
                outcome = service.continue_free_text(sid, bid, payload['text'], stream=stream, stream_reset=reset)
            else:
                outcome = dict(kind='accepted', node=service.continue_direction(sid, bid, payload['direction_id'], stream=stream, stream_reset=reset))
            validating()
            if outcome['kind'] == 'accepted' and isinstance(planner, PlayerNarrativePlanner):
                from .reader_consequences import enabled
                from .reader_choices import generate_choices
                state = snapshot.node['branchState'] if snapshot.node else {}
                beat = player_beat(package, state)
                ending_ids = package['story'].get('narrativeGraph', {}).get('endingBeatIds', {}).values()
                terminal = state.get('storyScope') == 'source' and beat and beat['id'] in ending_ids
                if enabled(snapshot.package) and snapshot.node and not terminal:
                    menu, menu_audit = generate_choices(planner.gateway, package, snapshot.story_contract, snapshot.history, snapshot.node)
                    snapshot.audits.append([menu_audit, bid])
                    if menu:
                        snapshot.node['readerChoices'] = menu
            return snapshot.artifact(outcome)
        except Exception as error:
            if isinstance(error, LlmError) and error.code in ('action_clarification_needed', 'action_conflict'):
                outcome = {'kind': 'clarification_needed' if error.code == 'action_clarification_needed' else 'rejected', 'message': str(error)}
                if snapshot.evaluation:
                    snapshot.evaluation['evaluation'] = outcome
                return snapshot.artifact(outcome)
            error.draft_usage = snapshot.usage
            error.draft_audits = snapshot.audits
            raise

    @staticmethod
    def _existing_turn(store, sid, bid, payload, request_id):
        if store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_requests'").fetchone():
            row = store.connection.execute('SELECT * FROM turn_requests WHERE session_id=? AND request_id=?', (sid, request_id)).fetchone()
            if row:
                if row['parent_id'] != bid or json.loads(row['input_json']) != payload:
                    raise ReadError(409, 'request_conflict', '同一请求不能用于不同的行动')
                return dict(json.loads(row['result_json']), deduplicated=True)
        node = store.find_branch_request(sid, request_id)
        evaluation = store.find_direction_request(sid, request_id)
        if node:
            original = node.get('turnInput')
            if original is None:
                original = {'text': node['playerDirection']} if node.get('playerDirection') else {'direction_id': node.get('selectedDirectionId')}
            if node['parentId'] != bid or original != payload:
                raise ReadError(409, 'request_conflict', '同一请求不能用于不同的行动')
            return dict(status='written', request_id=request_id, deduplicated=True, branch=node)
        if evaluation:
            if evaluation['parentBranchId'] != bid or payload != {'text': evaluation['playerDirection']}:
                raise ReadError(409, 'request_conflict', '同一请求不能用于不同的行动')
            if evaluation['kind'] != 'accepted':
                return dict(status='rejected', request_id=request_id, deduplicated=True, branch=None,
                            kind=evaluation['kind'], reason=evaluation.get('message') or evaluation.get('rationale'))
        return None

    @staticmethod
    def _prepared_branch(store, sid, key):
        row = store.connection.execute("SELECT id FROM branch_nodes WHERE session_id=? AND json_extract(node_json,'$.preparedTurnKey')=?", (sid, key)).fetchone()
        return store.branch(sid, row['id']) if row else None

    @staticmethod
    def _natural_ending(node, package):
        """A completed scene closes the route without a second manual action."""
        if node.get('nextDirections'):
            return False
        if node.get('naturalEnding') is True:
            return True
        ending_ids = set((package.get('story', {}).get('narrativeGraph', {}).get('endingBeatIds') or {}).values())
        return node.get('sourceNodeRef') in ending_ids

    @staticmethod
    def _save_natural_ending(store, session_id, branch_id):
        from .route_lifecycle import records, save_record
        nodes = store.lineage(session_id, branch_id)
        record = records(store, session_id, nodes).get(branch_id, {})
        record['receipt'] = {
            'ending_type': 'natural', 'branch_id': branch_id, 'closed_at': now(),
            'ending_written': True, 'source': 'scene_completion',
        }
        save_record(store, session_id, branch_id, record)
        saved = preferences(store, session_id)
        saved['ended'][branch_id] = 'completed'
        save_preferences(store, session_id, ended=saved['ended'])

    def _commit_turn(self, job, artifact, payload, request_id):
        binding = job['binding']
        sid, bid = binding['session_id'], binding['parent_branch_id']
        with self._lock:
            store = SessionStore(str(self.database_path))
            try:
                with store.connection:
                    store.connection.execute('BEGIN IMMEDIATE')
                    def receipt(result):
                        store.connection.execute('INSERT INTO turn_requests VALUES (?,?,?,?,?)',
                            (sid, request_id, bid, json.dumps(payload, ensure_ascii=False), json.dumps(result, ensure_ascii=False)))
                        return result
                    existing = self._existing_turn(store, sid, bid, payload, request_id)
                    if existing:
                        return existing
                    session = store.get_session(sid)
                    path, package = self.read.load_package(session['storyPackageId'], session['storyPackageVersion'])
                    store.assert_session_package(sid, package)
                    actual = digest([store.branch(sid, bid), store.contract(sid), store.derived(sid), session.get('storyPackageModuleIndexSha256')])
                    if actual != binding['parent_digest'] or binding['rules_version'] != RULES_VERSION:
                        raise ReadError(409, 'draft_expired', '这段故事已变化，请重新选择方向。')
                    self._check_closing_binding(store, binding)
                    if journey(store, sid, bid, package)['status'] != 'active':
                        raise ReadError(409, 'route_ended', '这条路线已收尾。')
                    # Different click IDs for the same prepared result also
                    # converge to one saved branch (double clicks/multiple tabs).
                    saved = self._prepared_branch(store, sid, job['key'])
                    if saved:
                        return receipt(dict(status='written', request_id=request_id, deduplicated=True, branch=saved))
                    if artifact is None:
                        return None
                    for audit, parent_id in artifact['audits']:
                        store.save_audit(sid, audit, parent_id)
                    evaluation = artifact['evaluation']
                    if evaluation and store.find_direction_request(sid, request_id) is None:
                        store.save_direction_evaluation(sid, bid, evaluation['text'], evaluation['evaluation'], request_id)
                    outcome = artifact['outcome']
                    if outcome['kind'] != 'accepted':
                        return receipt(dict(status='rejected', request_id=request_id, deduplicated=False, branch=None,
                                    kind=outcome['kind'], reason=outcome.get('message') or outcome.get('rationale')))
                    node = dict(artifact['node'], requestId=request_id, turnInput=payload,
                                preparedTurnKey=job['key'], preparedAttemptId=job['attempt_id'])
                    from .route_outline import build_outline
                    node['parentId'] = bid
                    node['routeOutline'] = build_outline(package, store.contract(sid), [*store.lineage(sid, bid), node])
                    stored = store.append_branch(sid, bid, node)
                    if artifact['derived']:
                        store.update_derived(artifact['derived'])
                    if self._natural_ending(stored, package):
                        self._save_natural_ending(store, sid, stored['id'])
                    return receipt(dict(status='written', request_id=request_id, deduplicated=False, branch=stored,
                                reason=outcome.get('rationale')))
            except ValueError as error:
                raise ReadError(409, 'play_rejected', str(error)) from error
            finally:
                store.close()

    def continue_turn(self, session_id, parent_branch_id, direction_id=None, text=None,
                      request_id=None, stream=None, stream_reset=None, choice_id=None, subscriber_id=None, draft_id=None, history_id=None):
        selected_at = time.monotonic()
        self._validate_history_id(history_id)
        self._require_planner()
        if sum(v is not None for v in (direction_id, text, choice_id)) != 1 or (text is not None and not text.strip()):
            raise ReadError(422, 'invalid_request', '请选择一个方向或填写自由行动')
        request_id = request_id or 'http-' + uuid.uuid4().hex
        action_payload = {'choice_id': choice_id} if choice_id is not None else ({'text': text.strip()} if text is not None else {'direction_id': direction_id})
        payload = dict(action_payload, history_id=history_id) if history_id is not None else action_payload
        if not self.database_path.is_file():
            raise ReadError(404, 'session_not_found', '会话不存在')
        with self.read.store() as store:
            try:
                store.get_session(session_id)
            except ValueError as error:
                raise ReadError(404, 'session_not_found', '会话不存在') from error
            existing = self._existing_turn(store, session_id, parent_branch_id, payload, request_id)
            if existing:
                return existing
        binding, snapshot = self._turn_snapshot(session_id, parent_branch_id, history_id)
        if choice_id is not None:
            choice = next((c for c in visible_choices(snapshot.history[-1], snapshot.package, snapshot.story_contract, snapshot.history) if c['id'] == choice_id), None)
            if choice is None:
                raise ReadError(409, 'choice_unavailable', '这个方向已不在当前可选列表中')
            generation_payload = choice['payload']
            binding['choice_id'] = choice_id
        else:
            if direction_id is not None:
                from .reader_consequences import filter_directions
                parent = snapshot.history[-1]
                if not any(d['id'] == direction_id for d in parent['nextDirections']):
                    raise ReadError(409, 'play_rejected', '当前分支不存在可选方向: ' + direction_id)
                if not any(d['id'] == direction_id for d in filter_directions(parent['nextDirections'], snapshot.package, parent['branchState'])):
                    raise ReadError(409, 'choice_unavailable', '这个方向已不符合当前分支状态')
            generation_payload = action_payload
            binding.update(request_id=request_id, input_digest=digest(payload))
        if draft_id is not None and (choice_id is None or digest(binding) != draft_id):
            raise ReadError(409, "draft_expired", "这个方向的前情已变化，请刷新后重新选择。")
        if choice_id is not None:
            with self.read.store() as store:
                saved = self._prepared_branch(store, session_id, digest(binding))
            if saved:
                result = self._commit_turn(dict(binding=binding, key=digest(binding)), None, payload, request_id)
                if result is not None:
                    _stream_text(result.get('branch', {}).get('narrativeText', ''), stream)
                    if subscriber_id:
                        self.drafts.release(session_id, parent_branch_id, subscriber_id)
                    return result
        job = self._ensure_turn(binding, generation_payload, snapshot, foreground=True, retry=True)
        try:
            if subscriber_id:
                self.drafts.release(session_id, parent_branch_id, subscriber_id)
            artifact = self.drafts.wait(job, stream, stream_reset)
            started = time.monotonic()
            result = self._commit_turn(job, artifact, payload, request_id)
            with self.drafts.condition:
                job['metrics']['commit_ms'] = round((time.monotonic() - started) * 1000)
                written = result['status'] == 'written'
                owns_result = written and (
                    result['branch'].get('preparedAttemptId') == job.get('attempt_id') and bool(job.get('attempt_id'))
                    or job['metrics'].get('committed_branch_id') == result['branch']['id'])
                job['metrics']['committed'] = owns_result
                job['metrics']['result_reused'] = written and not owns_result
                if owns_result:
                    job['metrics']['committed_branch_id'] = result['branch']['id']
                job['metrics']['selection_to_complete_ms'] = round((time.monotonic() - selected_at) * 1000)
            return result
        finally:
            self.drafts.release_selection(job)
