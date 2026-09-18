"""Read a standard novel source without promoting it to executable story data."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple


class SourceNovelError(ValueError):
    """The input is not a reviewable standard UTF-8 novel source."""


CHAPTER_HEADING = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*[章节回篇].*|"
    r"[0-9一二三四五六七八九十百千万零〇两]+\s*[、.．]\s*\S.*)$"
)

def inspect_standard_novel(path: Path) -> Dict[str, Any]:
    """Create an evidence-indexed manifest for a standard UTF-8 novel text.

    The result is deliberately not a StoryPackage. Semantic candidates remain
    empty until a later extraction and review step can cite these source ranges.
    """
    source_path = Path(path)
    if source_path.suffix.lower() != ".txt":
        raise SourceNovelError("小说母本必须是 .txt 文件")
    try:
        raw = source_path.read_bytes()
    except OSError as error:
        raise SourceNovelError("无法读取小说母本: " + str(error)) from error
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceNovelError("小说母本必须使用 UTF-8 编码") from error

    lines = list(_lines_with_offsets(text))
    title_line = next((line for line in lines if line[2].strip()), None)
    if title_line is None:
        raise SourceNovelError("小说母本不能为空")
    title_index, title_start, raw_title = title_line
    title = raw_title.strip().lstrip("\ufeff").strip()
    if not title:
        raise SourceNovelError("小说标题不能为空")

    body_lines = [line for line in lines if line[0] > title_index]
    if not any(line[2].strip() for line in body_lines):
        raise SourceNovelError("小说母本必须在标题后包含正文")
    chapter_starts = [line for line in body_lines if CHAPTER_HEADING.match(line[2].strip())]
    chapters = _chapters(text, body_lines, chapter_starts)
    warnings: List[str] = []
    if not chapter_starts:
        warnings.append("未识别到章节标题；已将全部正文作为待确认章节。")

    return {
        "schemaVersion": "source-novel-manifest/0.1",
        "status": "needs_review",
        "source": {
            "fileName": source_path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": "utf-8",
            "characters": len(text),
            "lines": len(lines),
            "title": {
                "value": title,
                "line": title_index,
                "characterRange": {"start": title_start, "end": title_start + len(raw_title.rstrip("\r\n"))},
            },
        },
        "chapters": chapters,
        "candidates": {
            "characters": [],
            "locations": [],
            "relations": [],
            "timeline": [],
            "plotNodes": [],
        },
        "review": {
            "required": [
                "确认章节边界和标题。",
                "在章节范围内提取候选角色、地点、关系、时间线与关键剧情节点。",
                "确认候选事实后才能构建可执行 StoryPackage。",
            ],
            "warnings": warnings,
        },
    }


def draft_entry_review(path: Path, package: Dict[str, Any], minimum_mentions: int = 2) -> Dict[str, Any]:
    """Extract source-cited entry candidates without changing a StoryPackage.

    This is deliberately conservative: name appearance is evidence for review,
    not a decision that a character is important or playable. A reviewer must
    complete motivations, point-of-view openings and node availability before
    the entry-model compiler accepts the result.
    """
    if minimum_mentions < 1:
        raise SourceNovelError("minimum_mentions 必须至少为 1")
    manifest = inspect_standard_novel(path)
    text = Path(path).read_text(encoding="utf-8")
    source_characters = []
    player_id = package.get("initialState", {}).get("player", {}).get("characterId")
    for character in package.get("characters", []):
        name = character.get("name")
        if not isinstance(name, str) or len(name.strip()) < 2:
            continue
        evidence = []
        appearances = 0
        for chapter in manifest["chapters"]:
            start = chapter["characterRange"]["start"]
            end = chapter["characterRange"]["end"]
            count = text[start:end].count(name)
            if count:
                appearances += count
                evidence.append({"chapterId": chapter["id"], "lineRange": chapter["lineRange"], "mentions": count})
        source_characters.append({
            "characterId": character["id"], "name": name, "role": character.get("role", "Unknown"),
            "mentions": appearances, "evidence": evidence[:8],
            "recommendedImportant": character["id"] == player_id or appearances >= minimum_mentions,
        })
    important_ids = [item["characterId"] for item in source_characters if item["recommendedImportant"]]
    beat_candidates = []
    for beat in package.get("story", {}).get("narrativeGraph", {}).get("beats", []):
        excerpt = beat.get("sourceExcerpt")
        if not isinstance(excerpt, dict) or not isinstance(excerpt.get("text"), str):
            continue
        related = [item["characterId"] for item in source_characters if item["characterId"] in important_ids and item["name"] in excerpt["text"]]
        beat_candidates.append({
            "beatId": beat["id"], "nodeId": beat.get("nodeId"), "summary": beat.get("summary", ""),
            "sourceLineRange": excerpt.get("lineRange"), "recommendedSourceCharacterIds": related,
            "requiredReviewFields": ["chapterTitle", "title", "timelineRefs", "sourceCharacterNarratives", "newCharacterNarrative"],
        })
    return {
        "schemaVersion": "entry-model-review-draft/0.1", "status": "needs_review",
        "source": {
            "fileName": manifest["source"]["fileName"], "sha256": manifest["source"]["sha256"],
            "packageId": package.get("id"), "packageVersion": package.get("version"),
        },
        "candidates": {
            "sourceCharacters": source_characters, "recommendedSourceCharacterIds": important_ids,
            "entryPoints": beat_candidates,
        },
        "review": {
            "required": [
                "确认 recommendedImportant 角色，而不是按出现次数自动发布。",
                "为每位开放的原著角色确认可进入节点及其不违背母本的开场锚点。",
                "确认节点前时间线摘要，并补全新角色开场锚点。",
                "将审核结果转换为 entry-model-review/0.1 且 status=approved 后，才能运行 compile-entry-model。",
            ]
        },
    }


def _lines_with_offsets(text: str) -> List[Tuple[int, int, str]]:
    lines: List[Tuple[int, int, str]] = []
    offset = 0
    for index, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        lines.append((index, offset, raw_line))
        offset += len(raw_line)
    if not lines and text:
        lines.append((1, 0, text))
    return lines


def _chapters(
    text: str,
    body_lines: List[Tuple[int, int, str]],
    chapter_starts: List[Tuple[int, int, str]],
) -> List[Dict[str, Any]]:
    starts = chapter_starts or [next(line for line in body_lines if line[2].strip())]
    chapters: List[Dict[str, Any]] = []
    for index, (line_number, start, raw_heading) in enumerate(starts, start=1):
        next_start = starts[index][1] if index < len(starts) else len(text)
        lines = [line for line in body_lines if start <= line[1] < next_start]
        paragraphs = _paragraphs(lines[1:] if chapter_starts else lines)
        heading = raw_heading.strip()
        title = _chapter_title(heading, index, bool(chapter_starts))
        chapters.append({
            "id": f"chapter-{index:03d}",
            "title": title,
            "heading": heading if chapter_starts else None,
            "lineRange": {"start": line_number, "end": lines[-1][0] if lines else line_number},
            "characterRange": {"start": start, "end": next_start},
            "paragraphs": paragraphs,
        })
    return chapters


def _chapter_title(heading: str, index: int, has_heading: bool) -> str:
    if not has_heading:
        return "待确认章节"
    title = re.sub(r"^(?:第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*[章节回篇]|[0-9一二三四五六七八九十百千万零〇两]+\s*[、.．])\s*", "", heading)
    # Novel headings commonly use a Chinese colon after the chapter number.
    # It is part of the heading syntax, not part of the user-facing title.
    title = re.sub(r"^[：:]\s*", "", title)
    return title or f"第 {index} 章"


def _paragraphs(lines: List[Tuple[int, int, str]]) -> List[Dict[str, Any]]:
    paragraphs: List[Dict[str, Any]] = []
    active: List[Tuple[int, int, str]] = []
    for line in lines:
        if line[2].strip():
            active.append(line)
            continue
        if active:
            paragraphs.append(_paragraph_record(active, len(paragraphs) + 1))
            active = []
    if active:
        paragraphs.append(_paragraph_record(active, len(paragraphs) + 1))
    return paragraphs


def _paragraph_record(lines: List[Tuple[int, int, str]], index: int) -> Dict[str, Any]:
    first = lines[0]
    last = lines[-1]
    return {
        "id": f"paragraph-{index:03d}",
        "lineRange": {"start": first[0], "end": last[0]},
        "characterRange": {"start": first[1], "end": last[1] + len(last[2])},
    }
