import argparse
import unittest
import os
import io
import json
import sqlite3
import socket
import time
from contextlib import redirect_stdout
from threading import Event
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from open_story_engine import cli, content as content_module
from open_story_engine.authoring import StoryAuthoringError, compile_entry_model
from open_story_engine.cocreation import CoCreationService, DirectionEvaluator, LlmDirectionEvaluator, LlmPlanner, MockPlanner, NarrativeFieldStream, NarrativeReviewer, apply_branch_patch, chapter_title_for_direction, create_contract, entry_initial_state, entry_points_for_selection, entry_source_characters, guard_narrative, guard_source_character_names, initial_branch_state, normalize_optional_branch_items, parse_narrative_continuation, persona_profile_text, source_continuity_context, validate_branch_additions
from open_story_engine.content import load_runtime_story_package, load_story_package, package_path_from_root, reader_path_from_package_path, validate_story_package
from open_story_engine.environment import load_env_file
from open_story_engine.llm import Completion, LlmError, OpenAICompatibleGateway, parse_json_content
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.package_builder import StoryPackageBuildError, analyze_standard_novel, audit_story_package, audit_story_package_modules, build_source_reader, build_story_package, build_story_package_modules, write_story_package_modules
from open_story_engine.play import PlayerTurnService
from open_story_engine.source import SourceNovelError, draft_entry_review, inspect_standard_novel
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
        fixture_root = Path(__file__).resolve().parent / "fixtures"
        self.package = load_story_package(package_path_from_root(fixture_root))
        cli_packages = patch(
            "open_story_engine.cli.package_path_from_root",
            side_effect=lambda root=None, version=None: package_path_from_root(root or fixture_root, version),
        )
        cli_packages.start()
        self.addCleanup(cli_packages.stop)

    def _phase_root(self, service, session_id, root):
        arc_id = root["nextDirections"][0].get("arcId")
        if not arc_id:
            return root
        return service.continue_direction(session_id, root["id"], "arc:" + arc_id, "选择大方向")

    def _approved_entry_review(self):
        return {
            "schemaVersion": "entry-model-review/0.1",
            "status": "approved",
            "source": {"packageId": self.package["id"], "packageVersion": self.package["version"]},
            "entryModel": {
                "sourceCharacterIds": ["character_xu_chuan", "character_tang_qi", "character_chen_yan", "character_jiang_xu"],
                "defaultEntryPointId": "entry_rainy_arrival",
                "entryPoints": [{
                    "id": "entry_rainy_arrival", "title": "暴雨封锁临潮站", "summary": "雨夜的失联与放行压力同时出现。",
                    "chapterTitle": "第一章 二十三点十分", "nodeId": "node_arrival", "beatId": "beat_arrival",
                    "timelineRefs": ["timeline_tang_message", "timeline_train_held"],
                    "sourceCharacterIds": ["character_xu_chuan", "character_tang_qi", "character_chen_yan", "character_jiang_xu"],
                    "sourceCharacterNarratives": {
                        "character_tang_qi": "唐栖被困在信号室，仍试图确认那份维修记录能否留在暴雨之外。",
                        "character_chen_yan": "陈砚站在候车厅里，必须在列车放行与不断扩大的事故风险之间作出选择。",
                        "character_jiang_xu": "姜序攥着通行证，知道维修隧道的积水已经不能再被当作小事。"
                    },
                    "availableToNewCharacter": True,
                    "newCharacterNarrative": "暴雨封住临潮站，新来者被迫留在候车厅，也被卷入唐栖失联的消息。"
                }]
            }
        }

    def test_package_path_uses_package_id_from_environment(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STORY_PACKAGE_ID": "cultivation-journey",
            "STORY_PACKAGE_VERSION": "1.2.3",
        }, clear=False):
            path = package_path_from_root(Path(directory))
        self.assertEqual(path, Path(directory) / "content" / "packages" / "cultivation-journey" / "1.2.3" / "package.json")
        self.assertEqual(reader_path_from_package_path(path), path.with_name("reader.json"))

    def test_llm_audit_persists_module_prompt_context_and_migrates_legacy_database(self):
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "legacy.sqlite"
            legacy_connection = sqlite3.connect(database_path)
            legacy_connection.execute("""
                CREATE TABLE llm_audits (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                  operation TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
                  request_summary TEXT NOT NULL, raw_response TEXT, error TEXT,
                  call_observations_json TEXT, created_at TEXT NOT NULL
                )
            """)
            legacy_connection.commit()
            legacy_connection.close()

            store = SessionStore(str(database_path))
            columns = {
                row["name"]
                for row in store.connection.execute("PRAGMA table_info(llm_audits)")
            }
            self.assertIn("prompt_context_json", columns)

            session = store.create_session(self.package)
            prompt_context = {
                "mode": "modules",
                "currentChapterId": "chapter-001",
                "modulePaths": ["modules/chapters/chapter-001.json"],
            }
            store.save_audit(session["id"], {
                "operation": "branch_planner",
                "model": "test-model",
                "promptVersion": "test",
                "requestSummary": "test audit",
                "promptContext": prompt_context,
            })
            self.assertEqual(store.llm_audits(session["id"])[0]["promptContext"], prompt_context)
            store.close()

    def test_source_inspection_creates_reviewable_chapter_ranges_without_a_story_package(self):
        source = Path("content/source/rainy-waiting-room.v0.1.txt")
        manifest = inspect_standard_novel(source)

        self.assertEqual(manifest["status"], "needs_review")
        self.assertEqual(manifest["source"]["title"]["value"], "雨夜候车室")
        self.assertEqual([chapter["title"] for chapter in manifest["chapters"][:3]], ["二十三点十分", "十七号柜", "水位线以下"])
        self.assertGreaterEqual(len(manifest["chapters"]), 3)
        self.assertEqual(manifest["chapters"][0]["characterRange"]["end"], manifest["chapters"][1]["characterRange"]["start"])
        self.assertTrue(manifest["chapters"][0]["paragraphs"])
        self.assertEqual(manifest["candidates"]["characters"], [])
        self.assertNotIn("initialState", manifest)

    def test_source_inspection_rejects_non_utf8_and_non_text_inputs(self):
        with TemporaryDirectory() as directory:
            invalid = Path(directory) / "broken.txt"
            invalid.write_bytes(b"\xff\xfe")
            with self.assertRaisesRegex(SourceNovelError, "UTF-8"):
                inspect_standard_novel(invalid)
            with self.assertRaisesRegex(SourceNovelError, ".txt"):
                inspect_standard_novel(Path(directory) / "novel.md")

    def test_inspect_source_cli_writes_a_review_manifest(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            with patch("sys.stdout", new_callable=io.StringIO):
                result = cli.main(["inspect-source", "content/source/rainy-waiting-room.v0.1.txt", "--output", str(output)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "needs_review")

    def test_entry_review_draft_uses_source_mentions_as_reviewable_evidence(self):
        draft = draft_entry_review(Path("content/source/rainy-waiting-room.v0.1.txt"), self.package)
        characters = {item["characterId"]: item for item in draft["candidates"]["sourceCharacters"]}

        self.assertEqual(draft["schemaVersion"], "entry-model-review-draft/0.1")
        self.assertEqual(draft["status"], "needs_review")
        self.assertTrue(characters["character_xu_chuan"]["recommendedImportant"])
        self.assertGreater(characters["character_tang_qi"]["mentions"], 1)
        self.assertTrue(characters["character_tang_qi"]["evidence"])
        self.assertTrue(draft["candidates"]["entryPoints"])
        self.assertIn("sourceCharacterNarratives", draft["candidates"]["entryPoints"][0]["requiredReviewFields"])

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

    def test_story_package_entry_contract_uses_only_the_selected_timeline_digest(self):
        selection = {
            "kind": "source_character",
            "sourceCharacterId": "character_xu_chuan",
            "entryPointId": "entry_rainy_arrival",
        }
        contract = create_contract(self.package, "entry-session", selection)
        state = entry_initial_state(self.package, selection)

        self.assertEqual(contract["persona"], {
            "kind": "source_character", "sourceCharacterId": "character_xu_chuan", "name": "许川",
        })
        self.assertEqual(contract["entryBeatId"], "beat_arrival")
        self.assertEqual(contract["canonicalTimelineRefs"], ["timeline_tang_message", "timeline_train_held"])
        self.assertEqual(state["playerLocationId"], "location_waiting_hall")
        context = source_continuity_context(self.package, [{
            "canonicalRelation": "on_line",
            "summary": "许川抵达临潮站。",
            "narrativeText": "不应将整段母本原文发给模型。",
        }], state, contract["canonicalTimelineRefs"])
        self.assertNotIn("许川抵达临潮站", context)
        self.assertIn("分支状态账本", context)
        self.assertIn("唐栖向许川发送求助语音", context)
        self.assertNotIn("陈砚挪用维修款", context)
        self.assertNotIn("不应将整段母本原文", context)

    def test_new_character_entry_is_session_scoped_and_uses_package_declared_node(self):
        selection = {"kind": "new_character", "name": "林舟", "entryPointId": "entry_rainy_arrival"}
        self.assertEqual([item["id"] for item in entry_source_characters(self.package)], ["character_xu_chuan"])
        self.assertEqual([item["id"] for item in entry_points_for_selection(self.package, selection)], ["entry_rainy_arrival"])
        store = SessionStore(":memory:")
        session = store.create_session(self.package, initial_state=entry_initial_state(self.package, selection))
        contract, root = CoCreationService(self.package, store, MockPlanner()).start(session["id"], selection)

        self.assertEqual(contract["persona"], {"kind": "new_character", "name": "林舟"})
        self.assertEqual(root["sourceNodeRef"], "node_arrival")
        self.assertEqual(root["canonicalRelation"], "diverged")
        self.assertIn("暴雨封住了临潮站", root["narrativeText"])
        self.assertEqual(store.get_session(session["id"])["currentState"], root["branchState"])
        store.close()

    def test_player_flow_acceptance_reads_the_entry_chapter_and_confirms_an_edited_draft(self):
        package_path = (
            Path(__file__).resolve().parents[1]
            / "tests_py" / "fixtures" / "content" / "packages" / "rainy-waiting-room-source" / "0.1.16" / "package.json"
        )
        package = load_runtime_story_package(package_path, lazy=True)
        self.assertEqual(
            [(item["name"], item["menuDescription"]) for item in entry_source_characters(package)],
            [
                ("许川", "赶到临潮站、追查失联线索的人。"),
                ("唐栖", "追查异常记录的人。"),
                ("陈砚", "临潮站的值班主管。"),
                ("姜序", "夜班维修。"),
            ],
        )
        entry_model = package["story"]["entryModel"]
        self.assertEqual(
            [entry["id"] for entry in entry_points_for_selection(
                package,
                {"kind": "source_character", "sourceCharacterId": entry_model["sourceCharacterIds"][0]},
            )],
            [entry["id"] for entry in entry_model["entryPoints"]
             if entry_model["sourceCharacterIds"][0] in entry["sourceCharacterIds"]],
        )
        source_selection = {
            "kind": "source_character",
            "sourceCharacterId": entry_model["sourceCharacterIds"][0],
            "entryPointId": entry_model["defaultEntryPointId"],
        }
        reader = cli.load_source_reader(package_path, package)
        self.assertIsNotNone(reader)
        store = SessionStore(":memory:")
        source_session = store.create_session(
            package, initial_state=entry_initial_state(package, source_selection),
        )
        service = CoCreationService(package, store, MockPlanner())
        source_contract, root = service.start(source_session["id"], source_selection)

        chapter = next(item for item in reader["chapters"] if item["id"] == source_contract["entrySourceChapterId"])
        rendered_chapter = io.StringIO()
        with patch("sys.stdout", rendered_chapter):
            cli.print_entry_chapter(reader, source_contract)
        self.assertIn(chapter["title"], rendered_chapter.getvalue())
        self.assertIn(chapter["text"].strip(), rendered_chapter.getvalue())
        self.assertEqual(root["entryChapter"]["title"], chapter["title"])

        macro = service.continue_direction(
            source_session["id"], root["id"], root["nextDirections"][0]["id"], "选择大方向：救援唐栖",
        )
        self.assertEqual(macro["nextDirections"][0]["directionLevel"], "phase")
        first_direction = macro["nextDirections"][0]
        first_chapter = service.continue_direction(
            source_session["id"], macro["id"], first_direction["id"], "选择方向：" + first_direction["title"],
        )
        stored_before_confirmation = len(store.branches(source_session["id"]))
        editor_calls = []
        second_direction = first_chapter["nextDirections"][0]

        def edit_and_confirm(draft):
            editor_calls.append(draft["narrativeText"])
            self.assertEqual(len(store.branches(source_session["id"])), stored_before_confirmation)
            return draft["narrativeText"] + "\n\n许川没有增加新的事实，只把已经确认的状态重新核对了一次。"

        confirmed = service.continue_direction(
            source_session["id"], first_chapter["id"], second_direction["id"], "选择方向：" + second_direction["title"],
            draft_editor=edit_and_confirm,
        )
        persisted = store.branch(source_session["id"], confirmed["id"])

        self.assertEqual(len(editor_calls), 1)
        self.assertEqual(len(store.branches(source_session["id"])), stored_before_confirmation + 1)
        self.assertTrue(confirmed["draftConfirmation"]["edited"])
        self.assertEqual(confirmed["draftConfirmation"]["status"], "confirmed")
        self.assertEqual(persisted["narrativeText"], confirmed["narrativeText"])
        self.assertGreaterEqual(len("".join(confirmed["narrativeText"].split())), 2000)
        self.assertEqual(chapter_title_for_direction(package, first_direction), "二、" + first_direction["title"])
        self.assertEqual(first_chapter["storyArc"]["chapter"], {"title": "二、" + first_direction["title"], "status": "complete"})
        self.assertEqual(confirmed["storyArc"]["chapter"], {"title": "三、" + second_direction["title"], "status": "complete"})
        self.assertNotEqual(first_chapter["storyArc"]["chapter"]["title"], confirmed["storyArc"]["chapter"]["title"])
        self.assertNotEqual(first_chapter["branchState"]["sourceProgress"], confirmed["branchState"]["sourceProgress"])
        self.assertEqual(confirmed["branchState"]["sourceProgress"], second_direction["statePatch"]["sourceProgress"])
        cancellation_parent = confirmed
        cancellation_direction = cancellation_parent["nextDirections"][0]
        if cancellation_direction.get("directionLevel") == "arc":
            cancellation_parent = service.continue_direction(
                source_session["id"], cancellation_parent["id"], cancellation_direction["id"],
                "选择大方向：" + cancellation_direction["title"],
            )
            cancellation_direction = cancellation_parent["nextDirections"][0]
        stored_before_cancellation = len(store.branches(source_session["id"]))
        with self.assertRaisesRegex(ValueError, "草稿未确认"):
            service.continue_direction(
                source_session["id"], cancellation_parent["id"], cancellation_direction["id"], "取消本章草稿",
                draft_editor=lambda _: "",
            )
        self.assertEqual(len(store.branches(source_session["id"])), stored_before_cancellation)

        new_selection = {
            "kind": "new_character",
            "profile": {
                "name": "林舟", "gender": "女", "age": 26, "occupation": "记者",
                "sourceRelationship": "与唐栖曾在旧档案馆共事",
                "background": "调查城市公共工程事故的自由记者，因收到唐栖的求助来到临潮站。",
            },
            "entryPointId": entry_model["defaultEntryPointId"],
        }
        new_session = store.create_session(package, initial_state=entry_initial_state(package, new_selection))
        new_contract, _ = service.start(new_session["id"], new_selection)
        self.assertEqual(new_contract["persona"]["name"], "林舟")
        self.assertEqual(new_contract["entrySourceChapterId"], source_contract["entrySourceChapterId"])
        store.close()

    def test_script_generated_package_blocks_unreachable_character_message_and_arrival(self):
        package_path = (
            Path(__file__).resolve().parents[1]
            / "tests_py" / "fixtures" / "content" / "packages" / "rainy-waiting-room-source" / "0.1.16" / "package.json"
        )
        package = load_runtime_story_package(package_path, lazy=True)
        state = initial_branch_state(package["story"]["narrativeGraph"]["beats"][0])

        communication_assertion = next(
            assertion for assertion in package["stateModel"]["narrativeAssertions"]
            if assertion["message"] == "剧情正文与已确认的通信状态矛盾。"
        )
        self.assertEqual(
            communication_assertion["communicationTimestamp"]["knownMessageTimes"],
            ["二十二点五十五分"],
        )
        self.assertEqual(
            communication_assertion["communicationTimestamp"]["knownMessageAnchors"],
            ["别让陈砚拿到储物柜里的录音", "隧道里还有人"],
        )
        self.assertEqual(
            communication_assertion["communicationTimestamp"]["knownMessageTexts"],
            ["别让陈砚拿到储物柜里的录音。隧道里还有人。"],
        )
        with self.assertRaisesRegex(ValueError, "通信状态"):
            guard_narrative(
                "唐栖最后一条消息说她已经到站，让许川来接。",
                state, [], package=package,
            )
        with self.assertRaisesRegex(ValueError, "通信状态"):
            guard_narrative(
                "唐栖最后一条是未接通，时间停在二十三点零七分。",
                state, [], package=package,
            )
        with self.assertRaisesRegex(ValueError, "通信状态"):
            guard_narrative(
                "许川想起唐栖发来的语音，她说临潮站出事了，让他别过来。",
                state, [], package=package,
            )
        with self.assertRaisesRegex(ValueError, "通信状态"):
            guard_narrative(
                "唐栖的声音从语音里传来：\"临潮站，末班车，别让陈砚拿到储物柜里的录音。隧道里还有人。\"",
                state, [], package=package,
            )
        with self.assertRaisesRegex(ValueError, "未登记的通信事实"):
            guard_narrative(
                "许川的手机亮起，一条新消息的发件人未知。",
                state, [], package=package,
            )
        guard_narrative(
            "许川确认唐栖最后一条语音的发送时间是二十二点五十五分：别让陈砚拿到储物柜里的录音。",
            state, [], package=package,
        )

    def test_cli_draft_confirmation_supports_append_and_cancel(self):
        draft = {"narrativeText": "许川停在候车厅，先确认当前风险。"}
        with patch("builtins.input", side_effect=["append", "他把已经确认的信息记下。", "confirm"]):
            confirmed = cli.confirm_chapter_draft(draft, narrative_already_shown=True)
        self.assertEqual(confirmed, "许川停在候车厅，先确认当前风险。\n\n他把已经确认的信息记下。")
        with patch("builtins.input", side_effect=["cancel"]):
            with self.assertRaisesRegex(ValueError, "草稿未确认"):
                cli.confirm_chapter_draft(draft, narrative_already_shown=True)

    def test_cli_entry_menu_shows_compact_role_intro_and_required_profile_fields(self):
        package = compile_entry_model(self.package, self._approved_entry_review())
        package["characters"][0]["menuDescription"] = "抵达临潮站寻找失联唐栖。"
        output = io.StringIO()
        with patch("builtins.input", side_effect=["1", "1"]), redirect_stdout(output):
            selection = cli.choose_co_creation_entry(package)

        self.assertEqual(selection["sourceCharacterId"], "character_xu_chuan")
        self.assertIn("许川：抵达临潮站寻找失联唐栖。", output.getvalue())
        self.assertIn("新建角色（需填写：姓名、性别、年龄、职业、与原著角色或势力的关系、个人背景）", output.getvalue())

    def test_cli_draft_commands_are_reserved_outside_draft_confirmation(self):
        self.assertTrue(cli.is_draft_confirmation_command("append"))
        self.assertTrue(cli.is_draft_confirmation_command(" CONFIRM "))
        self.assertFalse(cli.is_draft_confirmation_command("append 一段正文"))

    def test_reviewed_entry_compiler_populates_role_list_and_new_character_profile(self):
        compiled = compile_entry_model(self.package, self._approved_entry_review())
        selection = {
            "kind": "new_character",
            "profile": {
                "name": "林舟", "gender": "女", "age": "26", "occupation": "记者",
                "sourceRelationship": "与唐栖曾在旧档案馆共事", "background": "调查城市公共工程事故的自由记者，因收到唐栖的求助而来到临潮站。",
            },
            "entryPointId": "entry_rainy_arrival",
        }
        contract = create_contract(compiled, "profile-session", selection)

        self.assertEqual([item["id"] for item in entry_source_characters(compiled)], [
            "character_xu_chuan", "character_tang_qi", "character_chen_yan", "character_jiang_xu",
        ])
        self.assertEqual(contract["persona"]["name"], "林舟")
        self.assertIn({"id": "age", "label": "年龄", "value": 26}, contract["persona"]["profile"])
        self.assertIn("与原著角色或势力的关系：与唐栖曾在旧档案馆共事", persona_profile_text(contract["persona"]))
        tang_selection = {"kind": "source_character", "sourceCharacterId": "character_tang_qi", "entryPointId": "entry_rainy_arrival"}
        store = SessionStore(":memory:")
        session = store.create_session(compiled, initial_state=entry_initial_state(compiled, tang_selection))
        _, root = CoCreationService(compiled, store, MockPlanner()).start(session["id"], tang_selection)
        self.assertEqual(root["canonicalRelation"], "diverged")
        self.assertIn("唐栖被困在信号室", root["narrativeText"])
        store.close()

    def test_entry_compiler_rejects_unapproved_candidates(self):
        review = self._approved_entry_review()
        review["status"] = "needs_review"
        with self.assertRaisesRegex(StoryAuthoringError, "尚未审核通过"):
            compile_entry_model(self.package, review)

    def test_cli_uses_compiled_profile_fields_instead_of_a_fixed_new_character_form(self):
        compiled = compile_entry_model(self.package, self._approved_entry_review())
        with patch("builtins.input", side_effect=[
            "5", "林舟", "女", "26", "记者", "与唐栖曾在旧档案馆共事",
            "调查城市公共工程事故的自由记者，因收到唐栖的求助而来到临潮站。", "1",
        ]):
            selection = cli.choose_co_creation_entry(compiled)
        self.assertEqual(selection["profile"]["age"], 26)
        self.assertEqual(selection["entryPointId"], "entry_rainy_arrival")

    def test_entry_model_rejects_a_new_character_entry_without_a_declared_intro(self):
        package = json.loads(json.dumps(self.package))
        package["story"]["entryModel"]["entryPoints"][0].pop("newCharacterNarrative")
        with self.assertRaisesRegex(ValueError, "newCharacterNarrative"):
            validate_story_package(package)

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
        self.assertEqual(derived["branchLedger"], entry["branchState"]["branchLedger"])
        self.assertEqual(derived["branchLedger"]["entries"][-1]["source"]["kind"], "player")
        self.assertEqual(self.package["id"], "rainy-waiting-room")
        started = service.continue_direction(session["id"], entry["id"], "direction_derivative_start", "选择方向：开始衍生篇")
        self.assertEqual(started["branchState"]["playerLocationId"], "location_station_office")
        self.assertEqual(started["branchState"]["tangLocationId"], "location_station_office")
        lead = service.continue_direction(session["id"], started["id"], "direction_derivative_follow_lead", "选择方向：暂离车站并跟进线索")
        named = service.continue_direction(session["id"], lead["id"], "direction_derivative_ask_identity", "选择方向：核验联系人身份")
        self.assertEqual(named["branchState"]["derivedCharacterReveals"], [{"characterId": "character_unidentified_contact", "name": "罗峥", "summary": "自称掌握相似项目编号线索的人；背景与动机仍待核验。"}])
        persisted = store.derived(session["id"])
        self.assertEqual(persisted["branchLedger"], named["branchState"]["branchLedger"])
        self.assertEqual(len(persisted["revisions"]), 3)
        self.assertTrue(all(revision["ledgerEntryIds"] for revision in persisted["revisions"]))
        tampered = json.loads(json.dumps(persisted, ensure_ascii=False))
        tampered["branchLedger"]["entries"][0]["summary"] = "被改写的历史"
        tampered["revisions"].append({"branchId": "branch_tampered", "ledgerEntryIds": ["ledger_fake"], "addedAt": "2026-01-01T00:00:00Z"})
        with self.assertRaisesRegex(ValueError, "只能追加修订"):
            store.update_derived(tampered)
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

    def test_narrative_guard_blocks_unregistered_source_outcomes_and_item_transfers(self):
        package = json.loads(json.dumps(self.package))
        package["metadata"]["authoringSource"] = "source_text_script"
        package["items"].append({"id": "item_key", "name": "钥匙", "portable": True})
        package["world"]["immutableFacts"].append({"id": "fact_tang_unreachable", "text": "唐栖的电话已经无法接通。", "sourceProgress": "chapter_001"})
        package["world"]["narrativeGuidelines"]["characterLocationStateFields"] = {
            "character_xu_chuan": "playerLocationId",
        }
        state = {"storyScope": "source", "playerLocationId": "location_waiting_hall", "inventory": []}
        with self.assertRaisesRegex(ValueError, "唐栖 的去向或结果尚未由状态登记"):
            guard_narrative("唐栖不在信号室，已经进入更深处的电缆井。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "唐栖 的去向或结果尚未由状态登记"):
            guard_narrative("唐栖隔着门回应了许川的呼喊。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "唐栖 的去向或结果尚未由状态登记"):
            guard_narrative("陈砚说唐栖刚才来过站务室，她走之前问过列车的事。", state, [], package=package)
        guard_narrative("陈砚站在候车厅门口，对讲机贴在耳边。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "钥匙 的交接尚未由状态登记"):
            guard_narrative("姜序递来的钥匙在许川掌心发冷。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未确认取得.*钥匙"):
            guard_narrative("陈砚从抽屉里拿出一把钥匙，试着打开候车厅的侧门。", state, [], package=package)
        owned_state = {**state, "itemOwnerCharacterIds": {"item_key": "character_chen"}}
        guard_narrative("陈砚从腰间取下钥匙，又挂回了原处。", owned_state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可持续物品: 手电筒"):
            guard_narrative("值班员递来一支手电筒，示意许川继续往里走。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可取证痕迹"):
            guard_narrative("门前出现一串明显的脚印，指向墙后的暗格。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可进入地点"):
            guard_narrative("许川发现一条员工通道，尽头通往地下。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可进入地点"):
            guard_narrative("许川在走廊尽头发现废弃配电间，推门走了进去。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可进入地点"):
            guard_narrative("陈砚快步走进值班室，接起了响个不停的电话。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可进入地点"):
            guard_narrative("候车厅另一头的值班室门开了，陈砚转身走回值班室门口。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可取证痕迹"):
            guard_narrative("许川循着门前新鲜的脚印，找到了一道暗门。", state, [], package=package)
        with self.assertRaisesRegex(ValueError, "未登记的可持续物品: 门禁卡"):
            guard_narrative("许川从积水里捡起一张门禁卡，试着打开走廊尽头的门。", state, [], package=package)
        guard_narrative("雨水把地上模糊的脚印冲散，候车厅只剩广播的杂音。", state, [], package=package)
        guard_narrative("走廊深处传来配电间断续的低鸣，像旧线路在潮气里喘息。", state, [], package=package)

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

    def test_modular_live_evaluation_uses_the_generated_package_scenarios(self):
        class ModuleContextMockPlanner(MockPlanner):
            def plan(self, context, selected, resolved_state, stream=None, stream_reset=None, repair=None):
                result, _ = super().plan(context, selected, resolved_state, stream, stream_reset, repair)
                resolver = self.context_resolver
                module_context = resolver.resolve(context, selected, resolved_state)
                return result, {
                    "operation": "branch_planner", "model": "test-model", "promptVersion": "test",
                    "requestSummary": selected["title"], "rawResponse": None, "callObservations": [],
                    "promptContext": {
                        "mode": "modules", "currentChapterId": module_context["currentChapter"]["id"],
                        "currentBeatId": module_context["currentBeat"]["id"],
                        "modulePaths": list(resolver.last_resolved_paths),
                    },
                }

        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STORY_LIVE_EVALUATION": "1",
            "STORY_PLANNER": "openai",
            "STORY_PACKAGE_ID": "rainy-waiting-room-source",
            "STORY_PACKAGE_VERSION": "0.1.16",
            "STORY_LLM_BASE_URL": "https://example.invalid/v1",
            "STORY_LLM_API_KEY": "test-key",
            "STORY_LLM_MODEL": "test-model",
        }, clear=False), patch(
            "open_story_engine.cli.create_live_evaluation_runtime",
            return_value=(ModuleContextMockPlanner(), DirectionEvaluator(), NarrativeReviewer(), "test modular JSON runtime"),
        ):
            output = Path(directory) / "modular-live-evaluation.json"
            with patch("sys.stdout", new=io.StringIO()):
                result = cli.run_evaluate_live(argparse.Namespace(scenario=None, output=str(output)))
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(report["package"], "rainy-waiting-room-source@0.1.16")
        self.assertEqual([item["id"] for item in report["results"]], [
            item.identifier for item in cli.MODULAR_LIVE_EVALUATION_SCENARIOS
        ])
        self.assertTrue(all(item["status"] == "passed" for item in report["results"]))

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

    def test_cocreation_runtime_can_disable_transport_fallback(self):
        with patch.dict(os.environ, {
            "STORY_PLANNER": "openai",
            "STORY_LLM_BASE_URL": "https://example.invalid/v1",
            "STORY_LLM_API_KEY": "test-key",
            "STORY_LLM_MODEL": "test-model",
            "STORY_LLM_STREAM": "false",
            "STORY_LLM_TRANSPORT_FALLBACK": "false",
            "STORY_LLM_TIMEOUT_SECONDS": "90",
            "STORY_LLM_REASONING_EFFORT": "none",
        }, clear=False):
            planner, evaluator, _reviewer, _label = cli.create_cocreation_runtime()

        self.assertFalse(planner.gateway.stream)
        self.assertFalse(planner.gateway.allow_transport_fallback)
        self.assertEqual(planner.gateway.timeout_seconds, 90)
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
        self.assertIn("已登记地点（涉及这些地点时只能直接使用登记名称", prompt)
        self.assertIn("本章角色白名单：许川、唐栖、陈砚、姜序", prompt)
        self.assertIn("地点名称必须逐字使用上方“已登记地点”中的名称", prompt)
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

    def test_branch_ledger_records_all_entity_kinds_and_change_provenance(self):
        state = initial_branch_state(self.package["story"]["narrativeGraph"]["beats"][0])
        resolved = apply_branch_patch(self.package, state, {
            "hasLockerToken": True,
            "derivedAdditions": {
                "locations": [{"id": "location_hidden_archive", "name": "隐蔽档案间", "summary": "用于核验旧档案的分支私有地点。"}],
                "characters": [{"id": "character_archive_keeper", "name": "档案保管人", "summary": "只确认负责保管旧档案，动机待核验。"}],
                "items": [{"id": "item_archive_seal", "name": "档案封签", "summary": "标记被重新封存的档案袋。"}],
                "relationships": [{
                    "id": "relationship_xu_keeper", "fromCharacterId": "character_xu_chuan",
                    "toCharacterId": "character_archive_keeper", "summary": "许川已向档案保管人说明核验目的。",
                }],
                "clues": [{"id": "clue_archive_number", "name": "档案编号", "summary": "编号可与维修记录交叉核验。"}],
                "events": [{"id": "event_archive_access", "name": "档案准入", "summary": "保管人允许在现场核验一份旧档案。"}],
                "characterReveals": [{
                    "characterId": "character_archive_keeper", "name": "杜宁", "summary": "保管人的姓名已在本回合确认。",
                }],
                "changes": [{
                    "kind": "character", "entityId": "character_xu_chuan", "summary": "许川已承担档案核验责任。",
                    "attributes": {"responsibility": "archive_verification"},
                }],
            },
        }, "node_records", {"kind": "player_direction", "ref": "direction_verify_archive", "nodeRef": "node_records"})

        self.assertEqual(resolved["derivedRelationships"][0]["id"], "relationship_xu_keeper")
        self.assertEqual(resolved["derivedClues"][0]["id"], "clue_archive_number")
        self.assertEqual(resolved["derivedEvents"][0]["id"], "event_archive_access")
        entries = resolved["branchLedger"]["entries"]
        self.assertEqual([entry["sequence"] for entry in entries], list(range(1, len(entries) + 1)))
        self.assertTrue({"location", "character", "item", "relationship", "clue", "event"}.issubset({entry["kind"] for entry in entries}))
        self.assertTrue(all(entry["source"] == {
            "kind": "player_direction", "ref": "direction_verify_archive", "nodeRef": "node_records",
        } for entry in entries))
        self.assertIn("character_xu_chuan", [entry["entityId"] for entry in entries if entry["operation"] == "changed"])

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

    def test_source_continuity_context_includes_item_ownership_and_parent_prose_only_for_divergence(self):
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
        guard_source_character_names("墙上的钟面停在二十三点十分。", self.package, state)
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

    def test_narrative_guard_blocks_focal_character_leaving_an_unchanged_location(self):
        generated_package = load_story_package(
            Path(__file__).resolve().parents[1]
            / "tests_py" / "fixtures" / "content" / "packages" / "rainy-waiting-room-source" / "0.1.16" / "package.json"
        )
        state = initial_branch_state(generated_package["story"]["narrativeGraph"]["beats"][0])
        with self.assertRaisesRegex(ValueError, "许川 的最终位置应为 候车厅"):
            guard_narrative(
                "许川沿着站台走了一段，停在列车旁听雨。",
                state,
                [],
                package=generated_package,
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
        historical = load_story_package(Path(__file__).resolve().parent / "fixtures/legacy-story-package.json")
        self.assertEqual(historical["version"], "0.1.0")
        self.assertEqual(self.package["version"], "0.1.2")
        self.assertNotIn("stateModel", historical)
        self.assertIn("stateModel", self.package)
        with patch.dict(os.environ, {"STORY_PACKAGE_VERSION": "0.1.0"}):
            self.assertEqual(
                package_path_from_root(),
                Path(__file__).resolve().parents[1] / "content/packages/rainy-waiting-room/0.1.0/package.json",
            )

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

    def test_llm_planner_repairs_one_source_causality_draft(self):
        class SourceGuardRepairGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.messages = []

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.messages.append(messages)
                content = (
                    "许川从积水里捡起一支手电筒，照向候车厅尽头。"
                    if self.calls == 1
                    else "许川站在候车厅中央，听见雨声压过广播的杂音。"
                )
                return Completion(
                    content=test_plain_narrative(content), raw_response=f"response-{self.calls}",
                    observations=[{"attempt": self.calls, "outcome": "completed", "generationStage": "initial"}],
                )

        package = json.loads(json.dumps(self.package))
        package["metadata"]["authoringSource"] = "source_text_script"
        state = {
            "storyScope": "source", "playerLocationId": "location_waiting_hall", "inventory": [],
            "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured",
        }
        selected = package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
        gateway = SourceGuardRepairGateway()
        resets = []

        result, audit = LlmPlanner(gateway).plan(
            {"package": package, "parent": {"id": "branch_test", "branchState": state, "summary": "雨夜的候车厅。"}, "characterDetails": []},
            selected, state, stream_reset=resets.append,
        )

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(resets, ["semantic_repair"])
        self.assertEqual(result["narrativeText"], "许川站在候车厅中央，听见雨声压过广播的杂音。")
        self.assertIn("上次草稿的问题：剧情正文引入了未登记的可持续物品: 手电筒", gateway.messages[1][1]["content"])
        self.assertEqual(audit["rawResponse"], "response-1\n\nresponse-2")
        self.assertTrue(any(item.get("retryReason") == "source_causality_guard" for item in audit["callObservations"]))

    def test_llm_planner_repairs_a_pending_vehicle_causality_draft(self):
        class VehicleGuardRepairGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.messages = []

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.messages.append(messages)
                content = (
                    "末班列车已经离站，旧码头一下安静下来。"
                    if self.calls == 1
                    else "末班列车仍停在旧码头，林舟先确认了车站广播里的等待信息。"
                )
                return Completion(
                    content=test_plain_narrative(content), raw_response=f"response-{self.calls}",
                    observations=[{"attempt": self.calls, "outcome": "completed", "generationStage": "initial"}],
                )

        package = json.loads(json.dumps(self.package))
        package["metadata"]["authoringSource"] = "source_text_script"
        package["stateModel"]["narrativeAssertions"] = [{
            "id": "assertion_vehicle_pending",
            "when": {"storyScope": {"equals": "source"}},
            "instruction": "末班列车仍在等待放行。",
            "message": "剧情正文与已确认的交通状态矛盾。",
            "forbiddenPatterns": [r"列车[^\u3002\uff01\uff1f\\n]{0,20}(?:离开|离站)"],
        }]
        state = {
            "storyScope": "source", "playerLocationId": "location_waiting_hall", "inventory": [],
            "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured",
        }
        selected = package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
        gateway = VehicleGuardRepairGateway()
        resets = []

        result, audit = LlmPlanner(gateway).plan(
            {"package": package, "parent": {"id": "branch_test", "branchState": state, "summary": "雨夜的候车厅。"}, "characterDetails": []},
            selected, state, stream_reset=resets.append,
        )

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(resets, ["semantic_repair"])
        self.assertEqual(result["narrativeText"], "末班列车仍停在旧码头，林舟先确认了车站广播里的等待信息。")
        self.assertIn("上次草稿的问题：剧情正文与已确认的交通状态矛盾。", gateway.messages[1][1]["content"])
        self.assertTrue(any(item.get("retryReason") == "source_causality_guard" for item in audit["callObservations"]))

    def test_llm_planner_repairs_an_unreachable_character_message_claim(self):
        class CommunicationGuardRepairGateway:
            model = "test-model"

            def __init__(self):
                self.calls = 0
                self.messages = []

            def complete_text(self, messages, _on_delta=None, _on_reset=None):
                self.calls += 1
                self.messages.append(messages)
                content = (
                    "唐栖最后一条消息说她已经到站，让许川来接。"
                    if self.calls == 1
                    else "许川站在候车厅中央，只能继续拨打唐栖无法接通的电话。"
                )
                return Completion(
                    content=test_plain_narrative(content), raw_response=f"response-{self.calls}",
                    observations=[{"attempt": self.calls, "outcome": "completed", "generationStage": "initial"}],
                )

        package = json.loads(json.dumps(self.package))
        package["metadata"]["authoringSource"] = "source_text_script"
        package["stateModel"]["narrativeAssertions"] = [{
            "id": "assertion_tang_unreachable",
            "when": {"storyScope": {"equals": "source"}},
            "instruction": "唐栖的电话已经无法接通，不能补写她的新消息或到站。",
            "message": "剧情正文与已确认的通信状态矛盾。",
            "forbiddenPatterns": [r"唐栖[^。！？\n]{0,80}最后一条[^。！？\n]{0,24}消息"],
        }]
        state = {
            "storyScope": "source", "playerLocationId": "location_waiting_hall", "inventory": [],
            "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured",
        }
        selected = package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
        gateway = CommunicationGuardRepairGateway()

        result, audit = LlmPlanner(gateway).plan(
            {"package": package, "parent": {"id": "branch_test", "branchState": state, "summary": "雨夜的候车厅。"}, "characterDetails": []},
            selected, state,
        )

        self.assertEqual(gateway.calls, 2)
        self.assertEqual(result["narrativeText"], "许川站在候车厅中央，只能继续拨打唐栖无法接通的电话。")
        self.assertIn("上次草稿的问题：剧情正文与已确认的通信状态矛盾。", gateway.messages[1][1]["content"])
        self.assertTrue(any(item.get("retryReason") == "source_causality_guard" for item in audit["callObservations"]))

    def test_llm_planner_continuation_repeats_source_fact_protection(self):
        package = json.loads(json.dumps(self.package))
        package["metadata"]["authoringSource"] = "source_text_script"
        package["world"]["immutableFacts"].append({
            "id": "fact_tang_unreachable", "text": "唐栖的电话已经无法接通。", "sourceProgress": "chapter_001",
        })
        state = {
            "storyScope": "source", "playerLocationId": "location_waiting_hall", "inventory": [],
            "trainStatus": "pending_release", "signalRoomStatus": "locked", "tangStatus": "located", "evidenceStatus": "unsecured",
        }
        selected = package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
        prompt = LlmPlanner(MockPlanner())._continuation_prompt(
            {"package": package}, selected, state, "许川站在候车厅中央。",
        )

        self.assertIn("唐栖的电话已经无法接通。", prompt)
        self.assertIn("不得通过“他/她/其”等代词间接断言", prompt)


class StoryPackageBuilderTests(unittest.TestCase):
    def _analyzer(self):
        class FixtureAnalyzer:
            def analyze(self, chapter, fragment):
                paragraph_id = fragment["paragraphIds"][0]
                if chapter["id"] == "chapter-001":
                    return {
                        "summary": "林舟在旧码头发现铜钥匙，决定前往灯塔。",
                        "characters": [{"name": "林舟", "role": "protagonist", "description": "寻找姐姐下落的青年。", "importance": "major", "evidenceParagraphIds": [paragraph_id]}],
                        "locations": [{"name": "旧码头", "description": "潮水拍打木桩的码头。", "evidenceParagraphIds": [paragraph_id]}],
                        "items": [{"name": "铜钥匙", "description": "刻有灯塔纹样的钥匙。", "portable": True, "evidenceParagraphIds": [paragraph_id]}],
                        "relations": [], "facts": [{"text": "林舟必须在潮水上涨前离开旧码头。", "evidenceParagraphIds": [paragraph_id]}],
                        "events": [{"title": "发现铜钥匙", "summary": "林舟在旧码头找到通往灯塔的铜钥匙。", "characterNames": ["林舟"], "locationNames": ["旧码头"], "openThreads": ["灯塔里的线索"], "evidenceParagraphIds": [paragraph_id]}],
                    }
                return {
                    "summary": "林舟抵达灯塔，与苏岚核对航海图。",
                    "characters": [{"name": "林舟", "role": "protagonist", "description": "寻找姐姐下落的青年。", "importance": "major", "evidenceParagraphIds": [paragraph_id]}, {"name": "苏岚", "role": "ally", "description": "守灯塔的航海员。", "importance": "major", "evidenceParagraphIds": [paragraph_id]}],
                    "locations": [{"name": "灯塔", "description": "海崖上的旧灯塔。", "evidenceParagraphIds": [paragraph_id]}],
                    "items": [{"name": "航海图", "description": "标出暗礁和航线的图。", "portable": True, "evidenceParagraphIds": [paragraph_id]}],
                    "relations": [{"fromName": "林舟", "toName": "苏岚", "description": "苏岚愿意协助林舟查找航线。", "evidenceParagraphIds": [paragraph_id]}],
                    "facts": [{"text": "航海图标出避开暗礁的旧航线。", "evidenceParagraphIds": [paragraph_id]}],
                    "events": [{"title": "核对航海图", "summary": "林舟在灯塔与苏岚核对航海图，确定下一段航线。", "characterNames": ["林舟", "苏岚"], "locationNames": ["灯塔"], "openThreads": [], "evidenceParagraphIds": [paragraph_id]}],
                }
        return FixtureAnalyzer()

    def test_script_builds_and_audits_a_generic_story_package_from_source_analysis(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。末班列车仍在旧码头等待放行。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            audit = audit_story_package(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            module_audit = audit_story_package_modules(source, analysis, package, reader, modules)

        self.assertEqual(audit["status"], "passed")
        self.assertEqual(module_audit["status"], "passed")
        self.assertIn("package-index.json", modules["files"])
        self.assertIn("main-story-graph.json", modules["files"])
        self.assertIn("chapter-index.json", modules["files"])
        self.assertIn("character-index.json", modules["files"])
        self.assertIn("chapters/chapter-001.json", modules["files"])
        self.assertIn("node-index.json", modules["files"])
        self.assertIn("arc-model.json", modules["files"])
        self.assertIn("arc-index.json", modules["files"])
        self.assertIn("entry-model.json", modules["files"])
        self.assertIn("entry-index.json", modules["files"])
        self.assertIn("beat-index.json", modules["files"])
        self.assertIn("runtime-index.json", modules["files"])
        self.assertIn("nodes/node_source_continuity.json", modules["files"])
        self.assertIn("arcs/arc_source_chapter_001.json", modules["files"])
        self.assertIn("entries/entry_chapter-001-event-001.json", modules["files"])
        self.assertNotIn("arcModel", modules["files"]["main-story-graph.json"]["story"])
        self.assertNotIn("entryModel", modules["files"]["main-story-graph.json"]["story"])
        self.assertIn("beats/chapter-001/beat_chapter_001.json", modules["files"])
        self.assertIn("reader/chapter-001.json", modules["files"])
        self.assertNotIn("sourceExcerpt", modules["files"]["chapters/chapter-001.json"]["beats"][0])
        self.assertEqual(modules["files"]["reader/chapter-001.json"]["chapter"]["text"], "一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。末班列车仍在旧码头等待放行。")
        self.assertEqual(modules["index"]["source"]["sha256"], package["sourceAnalysis"]["sha256"])
        self.assertEqual(modules["files"]["character-index.json"]["characters"][0]["path"].split("/")[0], "characters")
        self.assertTrue(modules["files"]["character-index.json"]["characters"][0]["menuDescription"])
        self.assertEqual(modules["files"]["indexes/beats/chapter-001.json"]["beats"][0]["path"], "beats/chapter-001/beat_chapter_001.json")
        self.assertEqual(modules["files"]["beats/chapter-001/beat_chapter_001.json"]["beat"]["id"], "beat_chapter_001")
        self.assertEqual(modules["files"]["node-index.json"]["nodes"][0]["path"], "nodes/node_source_continuity.json")
        self.assertEqual(modules["files"]["runtime-index.json"]["coreModules"]["beats"], "beat-index.json")
        self.assertEqual(modules["files"]["runtime-index.json"]["coreModules"]["nodes"], "node-index.json")
        self.assertEqual(modules["files"]["runtime-index.json"]["coreModules"]["arcs"], "arc-index.json")
        self.assertEqual(modules["files"]["runtime-index.json"]["coreModules"]["entries"], "entry-index.json")
        self.assertEqual(module_audit["checks"]["beatModuleCount"], 2)
        self.assertEqual(module_audit["checks"]["nodeModuleCount"], 1)
        self.assertEqual(module_audit["checks"]["arcModuleCount"], 1)
        self.assertEqual(module_audit["checks"]["entryModuleCount"], 2)
        self.assertTrue(module_audit["checks"]["runtimeIndexPresent"])
        self.assertGreater(audit["checks"]["topologyExitCount"], 0)
        self.assertGreater(audit["checks"]["sourceLocatedItemCount"], 0)
        self.assertEqual(package["metadata"]["title"], "潮汐灯塔")
        self.assertEqual(package["metadata"]["chapterHeadingStyle"], "chinese_dunhao")
        self.assertEqual(len(package["characters"]), 2)
        self.assertTrue(all(character["name"] in character["description"] for character in package["characters"]))
        entry_character_ids = package["story"]["entryModel"]["sourceCharacterIds"]
        entry_characters = [character for character in package["characters"] if character["id"] in entry_character_ids]
        self.assertTrue(entry_characters)
        self.assertTrue(all(character["sourceImportance"] == "major" for character in entry_characters))
        self.assertEqual(len(package["relationships"]), 1)
        self.assertEqual(len(package["story"]["narrativeGraph"]["beats"]), 2)
        self.assertTrue(any(location["exits"] for location in package["locations"]))
        self.assertIn("availableFromSourceChapterId", package["items"][0])
        self.assertEqual(package["story"]["entryModel"]["newCharacter"]["profileFields"][0]["id"], "name")
        self.assertEqual(reader["chapters"][0]["text"], "一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。末班列车仍在旧码头等待放行。")
        self.assertNotIn("chapters", package)
        self.assertIn("sourceChapterId", package["story"]["entryModel"]["entryPoints"][0])
        self.assertTrue(any("列车" in fact["text"] for fact in package["world"]["immutableFacts"]))
        self.assertTrue(package["stateModel"]["narrativeAssertions"])
        self.assertTrue(modules["files"]["chapters/chapter-001.json"]["facts"])
        validate_story_package(package)
        with self.assertRaisesRegex(ValueError, "交通状态"):
            guard_narrative(
                "末班列车已经离站，旧码头一下安静下来。",
                initial_branch_state(package["story"]["narrativeGraph"]["beats"][0]), [], package=package,
            )
        with self.assertRaisesRegex(ValueError, "交通状态"):
            guard_narrative(
                "末班列车已经发车，旧码头一下安静下来。",
                initial_branch_state(package["story"]["narrativeGraph"]["beats"][0]), [], package=package,
            )
        guard_narrative(
            "末班列车停在旧码头，发车时间仍显示二十三点十分。",
            initial_branch_state(package["story"]["narrativeGraph"]["beats"][0]), [], package=package,
        )

        class CountingPlanner(MockPlanner):
            def __init__(self):
                self.calls = 0

            def plan(self, *args, **kwargs):
                self.calls += 1
                return super().plan(*args, **kwargs)

        store = SessionStore(":memory:")
        session = store.create_session(package)
        planner = CountingPlanner()
        service = CoCreationService(package, store, planner, DirectionEvaluator())
        _, root = service.start(session["id"])
        self.assertEqual(root["nextDirections"][0]["directionLevel"], "arc")
        phases = service.continue_direction(session["id"], root["id"], root["nextDirections"][0]["id"], "选择大方向")
        self.assertEqual(phases["nextDirections"][0]["directionLevel"], "phase")
        advanced = service.continue_direction(session["id"], phases["id"], phases["nextDirections"][0]["id"], "选择方向：沿母本推进")
        self.assertEqual(advanced["branchState"]["sourceProgress"], "chapter_002")
        self.assertEqual(planner.calls, 1)
        store.close()

    def test_module_audit_rejects_a_tampered_generated_file(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            tampered = json.loads(json.dumps(modules))
            character_path = next(path for path in tampered["files"] if path.startswith("characters/"))
            tampered["files"][character_path]["character"]["description"] = "不属于脚本编译的内容。"
            audit = audit_story_package_modules(source, analysis, package, reader, tampered)
            self.assertEqual(audit["status"], "failed")
            self.assertTrue(any(issue["id"] == "module_content" for issue in audit["issues"]))
            tampered_index = json.loads(json.dumps(modules))
            tampered_index["index"]["indexes"]["chapters"] = "被改写的章节索引.json"
            index_audit = audit_story_package_modules(source, analysis, package, reader, tampered_index)
            self.assertEqual(index_audit["status"], "failed")
            self.assertTrue(any(issue["id"] == "module_index_content" for issue in index_audit["issues"]))

    def test_module_context_resolver_uses_only_indexed_planning_modules(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n\n三、塔顶\n林舟登上塔顶，发现远海的求救灯。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            package_path = Path(directory) / "packages" / "tide-lighthouse" / "0.1.0" / "package.json"
            package_path.parent.mkdir(parents=True)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
            write_story_package_modules(package_path.parent / "modules", modules)
            resolver = ModuleContextResolver.for_package(package_path, package)
            parent = {
                "summary": "林舟在旧码头找到铜钥匙。",
                "branchState": initial_branch_state(package["story"]["narrativeGraph"]["beats"][0]),
            }
            selected = package["story"]["narrativeGraph"]["beats"][0]["nextDirections"][0]
            state = {**parent["branchState"], **selected["statePatch"]}
            context = {
                "package": package,
                "parent": parent,
                "lineage": [parent],
                "characterDetails": [],
                "contract": {"entrySourceChapterId": "chapter-001", "canonicalTimelineRefs": []},
            }
            planner = LlmPlanner(object(), context_resolver=resolver)
            prompt = planner._prompt(context, selected, state, None)

        self.assertIsNotNone(resolver)
        self.assertIn("模块化故事上下文范围：当前章节《灯塔》的剧情节点", prompt)
        self.assertIn("分支状态账本（跨回合因果的唯一运行时依据", prompt)
        self.assertIn('"schemaVersion":"branch-state-ledger/0.1"', prompt)
        self.assertIn("当前章节摘要：林舟抵达灯塔", prompt)
        self.assertNotIn("一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。", prompt)
        self.assertNotIn("塔顶", prompt)
        self.assertTrue(resolver.loaded_paths)
        self.assertTrue(all(not path.startswith("reader/") for path in resolver.loaded_paths))
        self.assertTrue(all(not path.startswith("chapters/") for path in resolver.loaded_paths))
        self.assertIn("beats/chapter-002/beat_chapter_002.json", resolver.loaded_paths)
        self.assertNotIn("beats/chapter-003/beat_chapter_003.json", resolver.loaded_paths)
        self.assertEqual(planner.last_prompt_context["mode"], "modules")
        self.assertEqual(planner.last_prompt_context["currentChapterId"], "chapter-002")
        self.assertEqual(planner.last_prompt_context["currentBeatId"], "beat_chapter_002")
        self.assertEqual(planner.last_prompt_context["branchLedgerEntries"], 0)
        self.assertTrue(all(not path.startswith("reader/") for path in planner.last_prompt_context["modulePaths"]))

    def test_runtime_loader_reconstructs_a_valid_package_without_root_source_excerpts(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            package_path = Path(directory) / "packages" / "tide-lighthouse" / "0.1.0" / "package.json"
            package_path.parent.mkdir(parents=True)
            package_path.write_text("not a readable root package", encoding="utf-8")
            write_story_package_modules(package_path.parent / "modules", modules)
            runtime = load_runtime_story_package(package_path)

        self.assertEqual(runtime["id"], "tide-lighthouse")
        self.assertEqual(runtime["moduleIndexSha256"], package["moduleIndexSha256"])
        self.assertEqual(len(runtime["story"]["narrativeGraph"]["beats"]), 2)
        self.assertTrue(all("sourceExcerpt" not in beat for beat in runtime["story"]["narrativeGraph"]["beats"]))
        self.assertEqual(runtime["timeline"], package["timeline"])
        validate_story_package(runtime)

    def test_lazy_runtime_loader_defers_beat_modules_until_they_are_requested(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            package_path = Path(directory) / "packages" / "tide-lighthouse" / "0.1.0" / "package.json"
            package_path.parent.mkdir(parents=True)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
            write_story_package_modules(package_path.parent / "modules", modules)
            runtime = load_runtime_story_package(package_path, lazy=True)
            beats = runtime["story"]["narrativeGraph"]["beats"]
            nodes = runtime["story"]["nodes"]
            arcs = runtime["story"]["arcModel"]["arcs"]
            entries = runtime["story"]["entryModel"]["entryPoints"]

            self.assertEqual(beats.loaded_ids, [])
            self.assertEqual(nodes.loaded_ids, [])
            self.assertEqual(arcs.loaded_ids, [])
            self.assertEqual(entries.loaded_ids, [])
            self.assertEqual(beats[0]["id"], "beat_chapter_001")
            self.assertEqual(beats.loaded_ids, ["beat_chapter_001"])
            self.assertEqual(beats[1]["id"], "beat_chapter_002")
            self.assertEqual(beats.loaded_ids, ["beat_chapter_001", "beat_chapter_002"])
            self.assertEqual(nodes.get_by_id("node_source_continuity")["id"], "node_source_continuity")
            self.assertEqual(nodes.loaded_ids, ["node_source_continuity"])
            self.assertEqual(arcs.get_by_id("arc_source_chapter_001")["id"], "arc_source_chapter_001")
            self.assertEqual(arcs.loaded_ids, ["arc_source_chapter_001"])
            self.assertEqual([entry["id"] for entry in entries.for_new_character()], ["entry_chapter-001-event-001", "entry_chapter-002-event-001"])
            self.assertEqual(entries.loaded_ids, [])
            self.assertEqual(entries.get_by_id("entry_chapter-001-event-001")["id"], "entry_chapter-001-event-001")
            self.assertEqual(entries.loaded_ids, ["entry_chapter-001-event-001"])
            self.assertTrue(all("sourceExcerpt" not in beat for beat in beats))

    def test_large_package_startup_reads_only_segmented_index_catalogs(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "large-harbor.txt"
            chapters = [
                f"第{index}章 港口{index}\n林舟在旧码头找到铜钥匙，确认第{index}条线索，继续寻找灯塔。"
                for index in range(1, 41)
            ]
            source.write_text("长篇港口\n\n" + "\n\n".join(chapters) + "\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "large-harbor", "0.1.0")
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            package_path = Path(directory) / "packages" / "large-harbor" / "0.1.0" / "package.json"
            package_path.parent.mkdir(parents=True)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
            write_story_package_modules(package_path.parent / "modules", modules)
            with patch("open_story_engine.content._read_runtime_json", wraps=content_module._read_runtime_json) as read_json:
                runtime = load_runtime_story_package(package_path, lazy=True)
            startup_paths = [str(call.args[0]) for call in read_json.call_args_list]
            beats = runtime["story"]["narrativeGraph"]["beats"]
            arcs = runtime["story"]["arcModel"]["arcs"]
            entries = runtime["story"]["entryModel"]["entryPoints"]
            role_id = runtime["story"]["entryModel"]["sourceCharacterIds"][0]
            with patch("open_story_engine.content._read_runtime_json", wraps=content_module._read_runtime_json) as read_menu:
                menu = entries.for_source_character(role_id)
            menu_paths = [str(call.args[0]) for call in read_menu.call_args_list]

        self.assertEqual(modules["files"]["chapter-index.json"]["schemaVersion"], "story-package-chapter-index/0.2")
        self.assertEqual(len(modules["files"]["beat-index.json"]["segments"]), 40)
        self.assertEqual(len(modules["files"]["beat-index.json"]["locators"]), 40)
        self.assertEqual(beats.loaded_ids, [])
        self.assertEqual(arcs.loaded_ids, [])
        self.assertEqual(entries.loaded_ids, [])
        self.assertFalse(any("/indexes/" in path for path in startup_paths))
        self.assertTrue(menu)
        self.assertEqual(entries.loaded_ids, [])
        self.assertEqual(
            [path for path in menu_paths if "/indexes/entries/" in path],
            [str(package_path.parent / "modules" / "indexes" / "entries" / ("character_" + role_id + ".json"))],
        )

    def test_session_binds_the_script_generated_module_projection(self):
        package = {
            "id": "module-bound-story", "version": "0.1.0",
            "moduleIndexSha256": "a" * 64, "initialState": {"phase": "opening"},
        }
        store = SessionStore(":memory:")
        session = store.create_session(package)

        self.assertEqual(session["storyPackageModuleIndexSha256"], "a" * 64)
        store.assert_session_package(session["id"], package)
        changed = {**package, "moduleIndexSha256": "b" * 64}
        with self.assertRaisesRegex(ValueError, "模块索引已变化"):
            store.assert_session_package(session["id"], changed)
        store.close()

    def test_script_generated_package_accepts_a_custom_registered_location_action(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙，决定前往灯塔。\n\n二、灯塔\n林舟抵达灯塔，与苏岚核对航海图。\n", encoding="utf-8")
            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
        store = SessionStore(":memory:")
        session = store.create_session(package)
        service = CoCreationService(package, store, MockPlanner(), DirectionEvaluator())
        _, root = service.start(session["id"])
        phases = service.continue_direction(session["id"], root["id"], root["nextDirections"][0]["id"], "选择大方向")
        result = service.continue_free_text(session["id"], phases["id"], "请苏岚带路前往灯塔查看灯光")
        self.assertEqual(result["kind"], "accepted")
        location_id = next(item["id"] for item in package["locations"] if item["name"] == "灯塔")
        self.assertEqual(result["node"]["branchState"]["playerLocationId"], location_id)
        self.assertEqual(result["node"]["branchState"]["freeTextProgress"], 1)
        locations = result["node"]["branchState"]["characterLocationIds"]
        self.assertEqual(locations[next(item["id"] for item in package["characters"] if item["name"] == "林舟")], location_id)
        self.assertEqual(locations[next(item["id"] for item in package["characters"] if item["name"] == "苏岚")], location_id)
        self.assertTrue(result["node"]["nextDirections"][0]["isFreeText"])
        store.close()

    def test_script_rejects_candidate_entities_that_are_not_in_the_cited_source(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙。\n", encoding="utf-8")

            analysis = analyze_standard_novel(source, 12000)
            analysis["chapters"][0]["fragments"][0]["candidate"]["locations"][0]["name"] = "不存在的地点"
            with self.assertRaisesRegex(StoryPackageBuildError, "名称未出现在原文"):
                build_story_package(source, analysis, "tide-lighthouse", "0.1.0")

    def test_script_preserves_multiple_cited_plot_nodes_in_one_chapter(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "harbor.txt"
            source.write_text("潮汐灯塔\n\n一、旧码头\n林舟在旧码头发现铜钥匙。\n\n林舟拿着铜钥匙前往灯塔。\n", encoding="utf-8")

            class TwoEventAnalyzer:
                def analyze(self, _chapter, fragment):
                    first, second = fragment["paragraphIds"]
                    return {
                        "summary": "林舟发现钥匙后离开码头。",
                        "characters": [{"name": "林舟", "role": "protagonist", "description": "寻找线索的青年。", "importance": "major", "evidenceParagraphIds": [first, second]}],
                        "locations": [{"name": "旧码头", "description": "潮水拍打木桩的码头。", "evidenceParagraphIds": [first]}, {"name": "灯塔", "description": "海崖上的灯塔。", "evidenceParagraphIds": [second]}],
                        "items": [{"name": "铜钥匙", "description": "刻有灯塔纹样。", "portable": True, "evidenceParagraphIds": [first, second]}],
                        "relations": [], "facts": [],
                        "events": [
                            {"title": "发现铜钥匙", "summary": "林舟在旧码头发现铜钥匙。", "characterNames": ["林舟"], "locationNames": ["旧码头"], "openThreads": ["前往灯塔"], "evidenceParagraphIds": [first]},
                            {"title": "前往灯塔", "summary": "林舟携带铜钥匙前往灯塔。", "characterNames": ["林舟"], "locationNames": ["灯塔"], "openThreads": [], "evidenceParagraphIds": [second]},
                        ],
                    }

            analysis = analyze_standard_novel(source, 12000)
            package = build_story_package(source, analysis, "tide-lighthouse", "0.1.0")
            audit = audit_story_package(source, analysis, package)
            reader = build_source_reader(source, analysis, package)
            modules = build_story_package_modules(source, analysis, package, reader)
            package_path = Path(directory) / "packages" / "tide-lighthouse" / "0.1.0" / "package.json"
            package_path.parent.mkdir(parents=True)
            package_path.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
            write_story_package_modules(package_path.parent / "modules", modules)
            resolver = ModuleContextResolver.for_package(package_path, package)
            second_beat = package["story"]["narrativeGraph"]["beats"][1]
            resolved_context = resolver.resolve(
                {
                    "contract": {"entrySourceChapterId": "chapter-001", "canonicalTimelineRefs": []},
                    "lineage": [], "parent": {"summary": "林舟发现铜钥匙。"},
                },
                {}, second_beat["branchState"],
            )

        self.assertEqual(audit["status"], "passed")
        self.assertEqual(audit["checks"]["plotNodeCount"], 2)
        self.assertEqual(len(package["story"]["narrativeGraph"]["beats"]), 2)
        self.assertEqual(resolved_context["currentBeat"]["id"], "beat_chapter_002")
        self.assertIn("beats/chapter-001/beat_chapter_002.json", resolved_context["modulePaths"])
        self.assertIn("beats/chapter-001/beat_chapter_001.json", resolved_context["modulePaths"])
        self.assertTrue(all(not path.startswith("chapters/") for path in resolved_context["modulePaths"]))


if __name__ == "__main__":
    unittest.main()
