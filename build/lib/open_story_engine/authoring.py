"""Compile reviewed source-analysis candidates into StoryPackage entry data."""

from __future__ import annotations

import copy
from typing import Any, Dict, List

from .content import NEW_CHARACTER_PROFILE_FIELD_IDS, validate_story_package


DEFAULT_NEW_CHARACTER_PROFILE_FIELDS = [
    {"id": "name", "label": "姓名", "type": "text", "minLength": 2, "maxLength": 12},
    {"id": "gender", "label": "性别", "type": "text", "minLength": 1, "maxLength": 12},
    {"id": "age", "label": "年龄", "type": "integer", "minimum": 1, "maximum": 150},
    {"id": "occupation", "label": "职业", "type": "text", "minLength": 2, "maxLength": 40},
    {"id": "sourceRelationship", "label": "与原著角色或势力的关系", "type": "text", "minLength": 2, "maxLength": 120},
    {"id": "background", "label": "个人背景", "type": "text", "minLength": 8, "maxLength": 300},
]


class StoryAuthoringError(ValueError):
    """Reviewed authoring input cannot safely become executable package data."""


def compile_entry_model(package: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    """Copy reviewed, source-cited entry choices into a new package value.

    The compiler never reads narrative prose or guesses significance. Extraction
    tooling must produce the candidate file, and a reviewer must explicitly
    mark it approved before it can affect a runnable StoryPackage.
    """
    if review.get("schemaVersion") != "entry-model-review/0.1":
        raise StoryAuthoringError("入口审核文件 schemaVersion 必须为 entry-model-review/0.1")
    if review.get("status") != "approved":
        raise StoryAuthoringError("入口候选尚未审核通过，不能写入 StoryPackage")
    source = review.get("source")
    if not isinstance(source, dict) or source.get("packageId") != package.get("id") or source.get("packageVersion") != package.get("version"):
        raise StoryAuthoringError("入口审核文件必须引用同一 StoryPackage 版本")
    model = review.get("entryModel")
    if not isinstance(model, dict):
        raise StoryAuthoringError("入口审核文件缺少 entryModel")
    source_character_ids = model.get("sourceCharacterIds")
    entries = model.get("entryPoints")
    if not isinstance(source_character_ids, list) or not source_character_ids or not isinstance(entries, list) or not entries:
        raise StoryAuthoringError("入口审核文件必须声明原著角色和进入节点")
    allowed = {character["id"] for character in package.get("characters", [])}
    if any(not isinstance(character_id, str) or character_id not in allowed for character_id in source_character_ids):
        raise StoryAuthoringError("入口审核文件引用了不存在的原著角色")
    if len(source_character_ids) != len(set(source_character_ids)):
        raise StoryAuthoringError("入口审核文件包含重复原著角色")
    profile_fields = copy.deepcopy(model.get("newCharacterProfileFields", DEFAULT_NEW_CHARACTER_PROFILE_FIELDS))
    field_ids = {field.get("id") for field in profile_fields if isinstance(field, dict)}
    if field_ids != NEW_CHARACTER_PROFILE_FIELD_IDS or len(profile_fields) != len(field_ids):
        raise StoryAuthoringError("新角色档案必须且只能包含 name、gender、age、occupation、sourceRelationship、background")
    default_entry_point_id = model.get("defaultEntryPointId")
    entry_ids = {entry.get("id") for entry in entries if isinstance(entry, dict)}
    if not isinstance(default_entry_point_id, str) or default_entry_point_id not in entry_ids:
        raise StoryAuthoringError("入口审核文件的默认进入节点不存在")

    compiled = copy.deepcopy(package)
    compiled_model = {
        "sourceCharacterIds": copy.deepcopy(source_character_ids),
        "newCharacter": {"enabled": bool(model.get("newCharacterEnabled", True)), "profileFields": profile_fields},
        "defaultEntryPointId": default_entry_point_id,
        "entryPoints": copy.deepcopy(entries),
    }
    compiled["story"]["entryModel"] = compiled_model
    try:
        validate_story_package(compiled)
    except ValueError as error:
        raise StoryAuthoringError("入口审核内容未通过 StoryPackage 校验: " + str(error)) from error
    return compiled
