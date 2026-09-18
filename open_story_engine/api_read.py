"""Read-only application services shared by HTTP routes, without CLI execution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .api_journey import journey, preferences
from .branch_ledger import BRANCH_LEDGER_KEY, empty_branch_ledger
from .cocreation import create_contract, entry_node
from .content import load_runtime_story_package, validate_story_package
from .module_context import ModuleContextError, ModuleContextResolver
from .storage import SessionStore
from .scene_library import SceneLibrary


class ReadError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ReadOnlySessionStore(SessionStore):
    """Reuse existing read methods without invoking initialization or migrations."""

    def __init__(self, database_path: Path) -> None:
        self.connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")


class ReadService:
    def __init__(self, package_root: Path, database_path: Path) -> None:
        self.package_root = package_root.resolve()
        self.database_path = database_path

    def package_path(self, package_id: str, version: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", package_id) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ReadError(422, "invalid_package_ref", "故事包 ID 或版本格式无效")
        path = (self.package_root / package_id / version / "package.json").resolve()
        if self.package_root not in path.parents or not path.is_file():
            raise ReadError(404, "package_not_found", "故事包不存在")
        return path

    def load_package(self, package_id: str, version: str) -> tuple[Path, dict[str, Any]]:
        path = self.package_path(package_id, version)
        try:
            package = load_runtime_story_package(path, lazy=True)
            if (package["id"], package["version"]) != (package_id, version):
                raise ValueError("目录与包身份不一致")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise ReadError(409, "invalid_package", "故事包结构或模块完整性校验失败") from error
        return path, package

    @staticmethod
    def preview_capability(path: Path | None, package: dict[str, Any]) -> dict[str, Any]:
        if path is None:
            return {"available": False, "code": "package_not_registered", "message": "仅校验上传的 JSON；未注册故事包或验证本地模块"}
        try:
            resolver = ModuleContextResolver.for_package(path, package)
            if resolver is None:
                return {"available": False, "code": "modular_context_required", "message": "上下文预览需要模块目录"}
        except (ModuleContextError, OSError, KeyError, TypeError, AttributeError):
            return {"available": False, "code": "context_modules_unavailable", "message": "上下文模块索引不兼容或校验失败"}
        # Only inspect indexes here. Selected module bodies are checked on demand.
        return {"available": True, "code": None, "message": None}

    def summary(self, package: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        return {
            "package_id": package["id"], "version": package["version"],
            "title": package["metadata"]["title"], "summary": package["metadata"].get("summary", ""),
            "visual_prompt": package["metadata"].get("visualPrompt", ""),
            "modular": bool(package.get("moduleIndexSha256")),
            "module_index_sha256": package.get("moduleIndexSha256"),
            "beat_count": len(package["story"]["narrativeGraph"]["beats"]),
            "character_count": len(package["characters"]),
            "context_preview": self.preview_capability(path, package),
        }

    def packages(self) -> dict[str, Any]:
        packages, issues = [], []
        for path in sorted(self.package_root.glob("*/*/package.json")):
            package_id, version = path.parent.parent.name, path.parent.name
            try:
                package_path, package = self.load_package(package_id, version)
                if package['story'].get('entryModel', {}).get('policy') != 'official_unknown_reader/1':
                    continue
                packages.append(self.summary(package, package_path))
            except ReadError as error:
                issues.append({"package_id": package_id, "version": version, "code": error.code, "message": error.message})
        packages.sort(key=lambda item: (item["package_id"], tuple(int(x) for x in item["version"].split("."))), reverse=True)
        return {"packages": packages, "issues": issues}

    def catalog(self, package: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
        entry_model = package["story"].get("entryModel", {})
        entries = entry_model.get("entryPoints", [])
        new_character = entry_model.get("newCharacter")
        playable_ids = set(entry_model.get("sourceCharacterIds", []))
        return {
            "package": self.summary(package, path),
            "entries": [{
                "id": item["id"], "title": item["title"], "chapter_title": item["chapterTitle"],
                "chapter_id": item.get("sourceChapterId"), "beat_id": item["beatId"],
                "node_id": item["nodeId"], "summary": item["summary"],
                "source_character_ids": item["sourceCharacterIds"],
                "available_to_new_character": bool(item.get("availableToNewCharacter")),
                "opening_image": SceneLibrary().opening(package, item),
            } for item in entries],
            **({
                "new_character": {
                    "enabled": bool(new_character.get("enabled")),
                    "profile_fields": new_character.get("profileFields", []) if isinstance(new_character, dict) else [],
                },
            } if isinstance(new_character, dict) and new_character.get("enabled") else {}),
            "beats": [{"id": beat["id"], "node_id": beat["nodeId"], "summary": beat["summary"]}
                      for beat in package["story"]["narrativeGraph"]["beats"]],
            # Keep the reviewed opening list separate from the full public
            # roster. The desktop identity picker merges both lists, while
            # read-only consumers can still distinguish reviewed openings.
            "characters": [character for character in package["characters"] if character.get("id") in playable_ids],
            "supporting_characters": [
                {key: character[key] for key in (
                    "id", "name", "description", "menuDescription", "role", "tags",
                    "roleGroup", "identitySummary", "motivation", "openingHook",
                    "defaultEntryPointId", "portraitAsset", "rosterVisible",
                ) if key in character}
                for character in package["characters"]
                if character.get("rosterVisible") and character.get("id") not in playable_ids
            ],
            # Keep catalog cards usable for UI location labels while omitting
            # source descriptions and other future-state details.
            "locations": [{key: entity[key] for key in ("id", "name") if key in entity}
                          for entity in package["locations"]],
            "items": [{key: entity[key] for key in ("id", "name") if key in entity}
                      for entity in package["items"]],
        }

    def parse(self, package: dict[str, Any]) -> dict[str, Any]:
        try:
            validate_story_package(package)
            return self.catalog(package)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise ReadError(422, "invalid_package", "请提供符合 StoryPackage 1.0 契约的完整 JSON 对象") from error

    @contextmanager
    def store(self):
        if not self.database_path.is_file():
            raise ReadError(404, "database_not_found", "尚无可读取的会话数据库")
        store = None
        try:
            store = ReadOnlySessionStore(self.database_path)
            # Keep the session, branch and lineage reads on one SQLite snapshot.
            store.connection.execute("BEGIN")
            yield store
        except (sqlite3.Error, IndexError) as error:
            raise ReadError(503, "storage_unavailable", "会话数据库不可读或结构不兼容；API 不会自动迁移数据库") from error
        finally:
            if store is not None:
                store.close()

    def chapter_reader(self, package_id: str, version: str) -> dict[str, Any]:
        """Load the hash-bound chapter reader for human reading; never feeds the planner."""
        path, package = self.load_package(package_id, version)
        reader_path = path.with_name("reader.json")
        if not reader_path.is_file():
            raise ReadError(404, "no_reader", "该故事包没有本地章节阅读器")
        try:
            reader = json.loads(reader_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ReadError(409, "invalid_reader", "本地章节阅读器无法读取") from error
        if (
            reader.get("schemaVersion") != "source-reader/0.1"
            or reader.get("package") != {"id": package["id"], "version": package["version"]}
            or reader.get("source", {}).get("sha256") != package.get("sourceAnalysis", {}).get("sha256")
            or not isinstance(reader.get("chapters"), list)
        ):
            raise ReadError(409, "invalid_reader", "本地章节阅读器与当前故事包不匹配")
        return reader

    def _progress_locator(self, package_id: str, version: str) -> dict[str, str]:
        path = self.package_path(package_id, version)
        index_path = path.parent / "modules" / "chapter-index.json"
        if not index_path.is_file():
            return {}
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        locator = index.get("progressLocator", [])
        return {
            item["sourceProgress"]: item["chapterId"]
            for item in locator
            if isinstance(item, dict) and isinstance(item.get("sourceProgress"), str) and isinstance(item.get("chapterId"), str)
        }

    def branch_source_chapter(self, session_id: str, branch_id: str) -> dict[str, Any]:
        """Map a branch to its original-source chapter via branchState.sourceProgress."""
        with self.store() as store:
            try:
                branch = store.branch(session_id, branch_id)
            except ValueError as error:
                raise ReadError(404, "branch_not_found", "分支不存在或跨会话") from error
            state = branch.get("branchState") or {}
            progress = state.get("sourceProgress")
            if not isinstance(progress, str) or not progress:
                raise ReadError(404, "no_source_chapter", "该页没有对应的原著章节")
            session_row = store.get_session(session_id)
        locator = self._progress_locator(session_row["storyPackageId"], session_row["storyPackageVersion"])
        chapter_id = locator.get(progress)
        if chapter_id is None:
            raise ReadError(404, "no_source_chapter", "该页没有对应的原著章节")
        reader = self.chapter_reader(session_row["storyPackageId"], session_row["storyPackageVersion"])
        chapter = next((item for item in reader["chapters"] if item.get("id") == chapter_id), None)
        if chapter is None or not isinstance(chapter.get("text"), str):
            raise ReadError(404, "chapter_not_found", "原著章节缺失")
        return {
            "session_id": session_id, "branch_id": branch_id,
            "chapter_id": chapter["id"], "title": chapter.get("title", ""),
            "text": chapter["text"], "source_progress": progress,
        }

    def sessions(self) -> dict[str, Any]:
        if not self.database_path.is_file():
            return {"available": False, "sessions": []}
        with self.store() as store:
            rows = store.connection.execute(
                "SELECT s.*, json_extract(c.contract_json, '$.persona.name') AS role_name, "
                "(SELECT json_extract(b.node_json, '$.summary') FROM branch_nodes b WHERE b.session_id=s.id ORDER BY b.sequence DESC LIMIT 1) AS recent_progress, "
                "(SELECT b.created_at FROM branch_nodes b WHERE b.session_id=s.id ORDER BY b.sequence DESC LIMIT 1) AS branch_updated_at "
                "FROM game_sessions s LEFT JOIN session_story_contracts c ON c.session_id=s.id "
                "ORDER BY COALESCE(branch_updated_at,s.updated_at) DESC, s.id"
            ).fetchall()
            return {"available": True, "sessions": [{
                "id": row["id"], "package_id": row["story_package_id"], "version": row["story_package_version"],
                "status": row["status"], "state_version": row["state_version"], "created_at": row["created_at"],
                "updated_at": row['branch_updated_at'] or row['updated_at'],
                "role_name": row['role_name'], "recent_progress": row['recent_progress'],
                "title": preferences(store, row['id'])['title'],
            } for row in rows]}

    def journey(self, session_id, branch_id):
        with self.store() as store:
            session = self.session(store, session_id)
            self.branch_state(self.branch(store, session_id, branch_id))
            _, package = self.load_package(session['storyPackageId'], session['storyPackageVersion'])
            return journey(store, session_id, branch_id, package)

    def route_monitor(self, session_id, branch_id):
        from .route_monitor import monitor
        from .api_journey import route_status
        with self.store() as store:
            session = self.session(store, session_id)
            self.branch_state(self.branch(store, session_id, branch_id))
            _, package = self.load_package(session['storyPackageId'], session['storyPackageVersion'])
            try:
                store.assert_session_package(session_id, package)
                nodes = store.lineage(session_id, branch_id)
            except ValueError as error:
                raise ReadError(409, 'route_history_unavailable', '路线历史或故事包绑定不一致，无法生成监测记录') from error
            try:
                return monitor(nodes, package, store.contract(session_id),
                               route_status(store, session_id, nodes, package))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '路线记录不完整或格式无效，无法生成监测记录') from error

    def route_outline(self, session_id, branch_id):
        from .route_outline import outline_view
        from .api_journey import route_status
        with self.store() as store:
            session = self.session(store, session_id)
            self.branch_state(self.branch(store, session_id, branch_id))
            _, package = self.load_package(session['storyPackageId'], session['storyPackageVersion'])
            try:
                store.assert_session_package(session_id, package)
                nodes = store.lineage(session_id, branch_id)
                return outline_view(package, store.contract(session_id), nodes,
                                    route_status(store, session_id, nodes, package))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '路线记录或故事包绑定无效，无法读取局部大纲') from error

    def route_closure(self, session_id, branch_id):
        from .route_closure import preparation
        from .route_lifecycle import view
        from .api_journey import route_status
        with self.store() as store:
            session = self.session(store, session_id)
            self.branch_state(self.branch(store, session_id, branch_id))
            _, package = self.load_package(session['storyPackageId'], session['storyPackageVersion'])
            try:
                store.assert_session_package(session_id, package)
                nodes = store.lineage(session_id, branch_id)
                result = preparation(package, store.contract(session_id), nodes,
                                     route_status(store, session_id, nodes, package))
                result['lifecycle'] = view(store, session_id, nodes, result)
                result['ending_written'] = result['lifecycle']['ending_written']
                return result
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '路线记录或故事包绑定无效，无法生成收束准备清单') from error

    def ending_proposals(self, session_id, branch_id):
        from .route_endings import local_record, context, eligible
        with self.store() as store:
            self.session(store, session_id)
            self.branch(store, session_id, branch_id)
            try:
                proposals = list(local_record(store, session_id, branch_id).get('endingProposals', {}).values())
                if not proposals:
                    return []
                binding = None
                try:
                    ctx = context(self, store, session_id, branch_id)
                    eligible(ctx)
                    binding = ctx['binding']
                except ReadError:
                    pass
                # Recent proposals only; compute the branch binding once per read.
                return [dict(p, can_cancel=p['status'] == 'pending', status='stale' if p['status'] in ('pending', 'approved') and
                             p['binding_digest'] != binding else p['status']) for p in reversed(proposals[-10:])]
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '终局提案记录无效') from error

    def ending_proposal(self, session_id, branch_id, proposal_id):
        from .route_endings import find_proposal, proposal_view
        with self.store() as store:
            self.session(store, session_id)
            self.branch(store, session_id, branch_id)
            try:
                proposal = find_proposal(store, session_id, branch_id, proposal_id)
                return proposal_view(self, store, session_id, branch_id, proposal)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ReadError(409, 'route_history_unavailable', '终局提案记录无效') from error

    @staticmethod
    def session(store: SessionStore, session_id: str) -> dict[str, Any]:
        try:
            return store.get_session(session_id)
        except ValueError as error:
            raise ReadError(404, "session_not_found", "会话不存在") from error

    @staticmethod
    def branch(store: SessionStore, session_id: str, branch_id: str) -> dict[str, Any]:
        try:
            return store.branch(session_id, branch_id)
        except ValueError as error:
            raise ReadError(404, "branch_not_found", "该会话中不存在指定分支") from error

    @staticmethod
    def branch_state(branch: dict[str, Any]) -> dict[str, Any]:
        state = branch.get("branchState")
        if not isinstance(state, dict):
            raise ReadError(409, "branch_state_unavailable", "历史分支未保存有效状态快照；正文仍可读取，不能代用其他状态")
        return state

    def session_preview_capability(self, store: SessionStore, session: dict[str, Any]) -> dict[str, Any]:
        try:
            path, package = self.load_package(session["storyPackageId"], session["storyPackageVersion"])
        except ReadError as error:
            return {"available": False, "binding_status": "unavailable", "code": error.code, "message": error.message}
        try:
            store.assert_session_package(session["id"], package)
        except ValueError:
            return {"available": False, "binding_status": "changed", "code": "package_binding_changed",
                    "message": "会话绑定的故事包已变化；历史正文和状态仍可读取"}
        binding = "matched" if session.get("storyPackageModuleIndexSha256") is not None else "legacy_unverified"
        capability = {**self.preview_capability(path, package), "binding_status": binding}
        if not capability["available"]:
            return capability
        if not store.has_branches(session["id"]):
            return {**capability, "available": False, "code": "no_confirmed_branches", "message": "会话尚无可预览的共创分支"}
        try:
            store.contract(session["id"])
        except ValueError:
            return {**capability, "available": False, "code": "invalid_session", "message": "会话缺少有效的共创契约"}
        return capability

    def session_view(self, session_id: str, include_branches: bool = True) -> dict[str, Any]:
        with self.store() as store:
            session = self.session(store, session_id)
            return {"session": session, "branches": store.branches(session_id) if include_branches else [],
                    "branches_included": include_branches,
                    "context_preview": self.session_preview_capability(store, session)}

    def branch_page(self, session_id: str, after_sequence: int, limit: int, include_actions: bool = False) -> dict[str, Any]:
        with self.store() as store:
            self.session(store, session_id)
            action_column = (
                ", COALESCE(NULLIF(json_extract(node_json,'$.playerDirection'),''), "
                "NULLIF(json_extract(node_json,'$.selectedDirection.title'),''), "
                "NULLIF(json_extract(node_json,'$.selectedDirection.summary'),''), "
                "CASE WHEN parent_id IS NULL THEN '故事开篇' ELSE json_extract(node_json,'$.summary') END) AS action"
            ) if include_actions else ""
            rows = store.connection.execute(
                "SELECT id,session_id,parent_id,sequence,created_at" + action_column + " FROM branch_nodes "
                "WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (session_id, after_sequence, limit + 1),
            ).fetchall()
            return {"session_id": session_id, "branches": [dict(row) for row in rows[:limit]],
                    "next_after_sequence": rows[limit - 1]["sequence"] if len(rows) > limit else None}

    def branch_view(self, session_id: str, branch_id: str) -> dict[str, Any]:
        with self.store() as store:
            self.session(store, session_id)
            branch = self.branch(store, session_id, branch_id)
            if not branch.get('branchState') or branch.get('canonicalRelation') != 'diverged':
                return branch
            from . import api_routes
            session = self.session(store, session_id)
            try:
                _, package = self.load_package(session['storyPackageId'], session['storyPackageVersion'])
            except ReadError:
                return branch
            if api_routes.role_name(package, branch['branchState']):
                branch = {**branch, 'nextDirections': api_routes.directions(package, branch['branchState'])}
            return branch

    def state(self, session_id: str, branch_id: str | None) -> dict[str, Any]:
        with self.store() as store:
            session = self.session(store, session_id)
            if branch_id:
                branch = self.branch(store, session_id, branch_id)
                state, version = self.branch_state(branch), branch["sequence"]
            else:
                if store.has_branches(session_id):
                    raise ReadError(422, "branch_required", "共创会话须明确指定 branch_id，不能以最后写入的分支推定当前分支")
                state, version = session["currentState"], session["stateVersion"]
            return {
                "session_id": session_id, "branch_id": branch_id, "state_version": version,
                "state": state, "ledger": state.get(BRANCH_LEDGER_KEY, empty_branch_ledger()),
            }

    def context(self, request: Any) -> dict[str, Any]:
        if request.session_id is not None:
            with self.store() as store:
                session = self.session(store, request.session_id)
                path, package = self.load_package(session["storyPackageId"], session["storyPackageVersion"])
                try:
                    store.assert_session_package(request.session_id, package)
                except ValueError as error:
                    raise ReadError(409, "package_binding_changed", "会话绑定的故事包版本或模块索引已变化") from error
                parent = self.branch(store, request.session_id, request.parent_branch_id)
                self.branch_state(parent)
                try:
                    contract = store.contract(request.session_id)
                    lineage = store.lineage(request.session_id, request.parent_branch_id)
                except ValueError as error:
                    raise ReadError(409, "invalid_session", "会话缺少有效的共创契约或分支历史") from error
            mode = "branch_preview"
        else:
            path, package = self.load_package(request.package.package_id, request.package.version)
            selection = {"kind": "source_character", "sourceCharacterId": request.source_character_id,
                         "entryPointId": request.entry_point_id}
            try:
                contract = create_contract(package, "preview", selection)
                parent = entry_node(package, contract)
            except ValueError as error:
                raise ReadError(422, "invalid_entry", "入口或原著角色不可用，不能建立该预览") from error
            lineage, mode = [parent], "entry_preview"
        resolver = ModuleContextResolver.for_package(path, package)
        if resolver is None:
            raise ReadError(409, "modular_context_required", "本批上下文预览仅支持具备模块目录的故事包")
        state = copy.deepcopy(parent["branchState"])
        # Preview only the confirmed parent/entry state, never a proposed next action.
        context = resolver.resolve({"contract": contract, "parent": parent, "lineage": lineage}, {"statePatch": {}}, state)
        payload = {"package": self.summary(package, path), "state": state, "context": context}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {**payload, "mode": mode, "session_id": request.session_id,
                "parent_branch_id": request.parent_branch_id, "context_sha256": digest}
