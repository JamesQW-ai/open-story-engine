"""Build an auditable StoryPackage from a standard novel TXT source.

This module is deliberately separate from the runtime planner. The runtime
model writes prose only; this local authoring pipeline derives conservative,
source-cited candidates and lets Python own all executable JSON, identifiers,
state transitions, and validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .authoring import DEFAULT_NEW_CHARACTER_PROFILE_FIELDS
from .content import validate_story_package
from .source import inspect_standard_novel


class StoryPackageBuildError(ValueError):
    """Source analysis cannot safely be promoted to an executable package."""


def analyze_standard_novel(path: Path, maximum_fragment_characters: int = 12000) -> Dict[str, Any]:
    """Analyse bounded source fragments and retain every citation boundary.

    The extractor never calls an LLM or sends source prose across a process
    boundary. It derives only conservative candidates from local text patterns;
    ``build_story_package`` and ``audit_story_package`` verify their ranges.
    """
    if maximum_fragment_characters < 1000:
        raise StoryPackageBuildError("maximum_fragment_characters 必须至少为 1000")
    manifest = inspect_standard_novel(path)
    text = Path(path).read_text(encoding="utf-8")
    known_names = _recognized_source_names(text)
    chapters: List[Dict[str, Any]] = []
    for chapter in manifest["chapters"]:
        fragments = _chapter_fragments(chapter, text, maximum_fragment_characters)
        analyses = []
        for fragment in fragments:
            candidate = _deterministic_fragment_candidate(chapter, fragment, known_names)
            analyses.append({"fragment": _fragment_reference(fragment), "candidate": candidate})
        chapters.append({
            "chapterId": chapter["id"], "chapterTitle": chapter["title"],
            "lineRange": copy.deepcopy(chapter["lineRange"]),
            "characterRange": copy.deepcopy(chapter["characterRange"]),
            "fragments": analyses,
        })
    _mark_character_importance(chapters)
    return {
        "schemaVersion": "source-semantic-analysis/0.1",
        "status": "candidate",
        "source": copy.deepcopy(manifest["source"]),
        "chapters": chapters,
    }


SURNAME_CHARACTERS = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏窦章云苏潘葛范彭郎鲁韦昌马苗方俞任袁柳鲍史唐费薛雷贺倪汤殷罗毕郝安常乐于傅齐康伍余元顾孟平黄和穆萧尹姚邵汪祁毛禹狄明臧成戴宋庞熊纪舒屈项祝董梁杜阮蓝闵席季贾路江童颜郭梅盛林钟徐邱骆高夏蔡田樊胡凌霍虞万柯管卢莫房裘解应宗丁宣邓洪包左石崔吉程邢裴陆荣翁荀惠曲封段富巫乌焦巴牧山谷车侯全班秋仲伊宫宁仇栾甘厉戎武刘景詹束龙叶幸司黎白怀蒲艾容向古易戈廖温庄晏柴瞿阎连简饶曾关游权盖公"
LOCATION_SUFFIXES = ("信号维修隧道", "维修隧道", "火车站", "候车厅", "站务室", "值班室", "信号室", "档案室", "配电间", "配电室", "旧码头", "消防通道", "维修通道", "码头", "灯塔", "车站", "站台", "走廊", "庭院", "客栈", "酒馆", "书院", "宗门", "山谷", "山庄", "通道", "城", "村", "镇", "港", "岛", "寺")
ITEM_SUFFIXES = ("录音笔", "图纸", "铜牌", "通行证", "门卡", "钥匙", "日志", "扳手", "文件袋", "对讲机", "时刻表", "手机", "档案", "账本", "信件", "令牌", "玉佩", "长剑", "药瓶", "残印", "无相镜", "灯")
LOCATION_PREFIXES = ("消防", "维修", "地下", "中央", "旧", "新", "东", "西", "南", "北", "内", "外")
PLOT_ACTION_MARKERS = ("发现", "决定", "前往", "抵达", "赶到", "进入", "离开", "推开", "刷开", "撬开", "断开", "弹开", "拿起", "抓起", "拍下", "交给", "递给", "救出", "承认", "拒绝", "找到", "启动", "切断", "放行", "救援", "上报", "写下", "完成")
RELATION_MARKERS = (("冲突", ("争执", "吵", "拦", "抢", "威胁", "隐瞒", "指责", "对峙", "敢")), ("协作", ("协助", "帮助", "递给", "核对")))
STATE_FACT_MARKERS = (
    "无法接通", "临时管制", "等待放行", "停驶", "停在", "紧闭", "锁闭", "被锁",
    "不对外开放", "电量不足", "电池只剩", "水位上涨", "倒灌", "断开", "故障", "发送时间",
)
PENDING_VEHICLE_MARKERS = ("临时管制", "等待放行", "停驶", "停在", "紧闭")
VEHICLE_TERMS = ("列车", "火车", "班车", "客车", "货车", "船只", "轮船")


def _deterministic_fragment_candidate(chapter: Dict[str, Any], fragment: Dict[str, Any], known_names: Sequence[str]) -> Dict[str, Any]:
    """Extract only local, text-present candidates from a source fragment."""
    characters: List[Dict[str, Any]] = []
    locations: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []
    relations: List[Dict[str, Any]] = []
    facts: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    summaries = []
    for index, paragraph in enumerate(fragment.get("paragraphs", []), start=1):
        paragraph_id = paragraph["id"]
        paragraph_text = paragraph["text"].strip()
        if not paragraph_text:
            continue
        names = _source_names(paragraph_text, known_names)
        place_names = _source_entities(paragraph_text, LOCATION_SUFFIXES)
        item_names = _source_entities(paragraph_text, ITEM_SUFFIXES)
        for name in names:
            characters.append({"name": name, "role": "source_character", "description": _source_sentence(paragraph_text, name), "importance": "minor", "evidenceParagraphIds": [paragraph_id]})
        for name in place_names:
            locations.append({"name": name, "description": _source_sentence(paragraph_text, name), "evidenceParagraphIds": [paragraph_id]})
        for name in item_names:
            items.append({"name": name, "description": _source_sentence(paragraph_text, name), "portable": _portable_item(name), "evidenceParagraphIds": [paragraph_id]})
        relations.extend(_source_relations(paragraph_text, names, paragraph_id))
        facts.extend(_source_facts(paragraph_text, paragraph_id))
        summary = _source_sentence(paragraph_text, names[0] if names else place_names[0] if place_names else "")
        summaries.append(summary)
        event_text = _plot_sentence(paragraph_text, names)
        if event_text:
            event_names = _source_names(event_text, known_names)
            event_places = _source_entities(event_text, LOCATION_SUFFIXES)
            event_title = _event_title(event_names, event_text)
            events.append({
                "title": event_title, "summary": event_text, "characterNames": event_names, "locationNames": event_places,
                "openThreads": [], "evidenceParagraphIds": [paragraph_id],
            })
    facts.extend(_source_communication_records(fragment.get("paragraphs", []), known_names))
    return {
        "summary": "；".join(summaries[:2]) or chapter["title"], "characters": _dedupe_entries("characters", characters),
        "locations": _dedupe_entries("locations", locations), "items": _dedupe_entries("items", items),
        "relations": _dedupe_entries("relations", relations), "facts": _dedupe_entries("facts", facts), "events": _dedupe_entries("events", events),
    }


def _mark_character_importance(chapters: List[Dict[str, Any]]) -> None:
    counts: Dict[str, int] = {}
    for chapter in chapters:
        for fragment in chapter["fragments"]:
            for character in fragment["candidate"].get("characters", []):
                counts[character["name"]] = counts.get(character["name"], 0) + 1
    highest_count = max(counts.values(), default=0)
    major_names = {name for name, count in counts.items() if count >= 2}
    if not major_names and highest_count:
        major_names = {name for name, count in counts.items() if count == highest_count}
    for chapter in chapters:
        for fragment in chapter["fragments"]:
            for character in fragment["candidate"].get("characters", []):
                character["importance"] = "major" if character["name"] in major_names else "minor"


def _recognized_source_names(text: str) -> List[str]:
    """Return repeated or explicitly introduced two-character Chinese names.

    A surname alone is not enough evidence: everyday prose contains many
    surname-shaped words. Repetition or an explicit name label is required so
    this remains conservative for arbitrary novels.
    """
    # Support common two- and three-character names. The former pattern
    # truncated names such as “陆照临” to “陆照”, which then poisoned every
    # downstream entity reference. Keep the witness checks below so ordinary
    # three-character prose is still rejected.
    patterns = (
        re.compile("([" + SURNAME_CHARACTERS + r"][\u4e00-\u9fff]{2})(?=(?:说|问|答|道|看|想|站|走|来|去|把|将|拿|抬|回|听|冲|赶|塞|按|握|在|没有|正在|终于|也|仍|，|。|！|？))"),
        re.compile("([" + SURNAME_CHARACTERS + r"][\u4e00-\u9fff])"),
    )
    candidates = [match.group(1) for pattern in patterns for match in pattern.finditer(text)]
    counts = {value: candidates.count(value) for value in set(candidates)}
    labels = {match.group(1) for match in re.finditer(r"(?:名叫|叫做|写着[：:]|姓名[：:])([" + SURNAME_CHARACTERS + r"][\u4e00-\u9fff]{1,2})", text)}
    values = sorted((
        value for value, count in counts.items()
        if (count >= 2 or value in labels or _has_name_introduction_witness(text, value))
        and (value in labels or _has_name_action_witness(text, value) or _has_name_introduction_witness(text, value))
        and not _looks_like_nonperson(value)
    ), key=lambda value: text.find(value))
    # When a valid three-character name is present, discard the truncated
    # two-character prefix (for example “陆照” beside “陆照临”).
    return [value for value in values if not any(
        len(other) > len(value) and other.startswith(value) for other in values
    )]


def _source_names(text: str, known_names: Sequence[str]) -> List[str]:
    return [name for name in known_names if name in text]


def _source_relations(text: str, names: Sequence[str], paragraph_id: str) -> List[Dict[str, Any]]:
    relations: List[Dict[str, Any]] = []
    for relation_type, markers in RELATION_MARKERS:
        sentence = next((
            value for value in re.split(r"(?<=[。！？])", text)
            if any(marker in value for marker in markers) and sum(name in value for name in names) >= 2
        ), None)
        if not sentence:
            continue
        related = [name for name in names if name in sentence]
        for index, from_name in enumerate(related):
            for to_name in related[index + 1:]:
                relations.append({
                    "fromName": from_name,
                    "toName": to_name,
                    "description": f"原文明确记录二人的{relation_type}互动：{sentence.strip()[:140]}",
                    "evidenceParagraphIds": [paragraph_id],
                })
        break
    return relations


def _source_facts(text: str, paragraph_id: str) -> List[Dict[str, Any]]:
    """Keep short, source-cited status facts without interpreting an outcome.

    These are deliberately sentences that state an observable constraint, not
    inferred character motivations.  They remain usable for any ordinary TXT
    source and never require a story-specific field in the runtime schema.
    """
    facts = []
    for sentence in (part.strip() for part in re.split(r"(?<=[。！？])", text)):
        if (
            not sentence
            or sentence.startswith(("“", "\""))
            or sentence.count("“") != sentence.count("”")
            or not any(marker in sentence for marker in STATE_FACT_MARKERS)
        ):
            continue
        facts.append({"text": sentence[:180], "evidenceParagraphIds": [paragraph_id]})
        if len(facts) == 2:
            break
    return facts


def _source_communication_records(paragraphs: Sequence[Dict[str, Any]], known_names: Sequence[str]) -> List[Dict[str, Any]]:
    """Extract a quoted source message with its sender, without paraphrasing it."""
    joined = "\n".join(
        paragraph.get("text", "") for paragraph in paragraphs
        if isinstance(paragraph, dict) and isinstance(paragraph.get("text"), str)
    )
    records: List[Dict[str, Any]] = []
    for name in known_names:
        match = re.search(
            re.escape(name) + r"发来的(语音|短信|消息)[\s\S]{0,600}?[“\"]([^”\"]{4,160})[”\"]",
            joined,
        )
        if match is None:
            continue
        content = match.group(2).strip()
        evidence = next((
            paragraph.get("id") for paragraph in paragraphs
            if isinstance(paragraph, dict)
            and isinstance(paragraph.get("id"), str)
            and content in str(paragraph.get("text", ""))
        ), None)
        if evidence is None:
            continue
        records.append({
            "text": content,
            "communication": {"speakerName": name, "kind": match.group(1)},
            "evidenceParagraphIds": [evidence],
        })
    return records


def _source_entities(text: str, suffixes: Sequence[str]) -> List[str]:
    values: List[str] = []
    for suffix in sorted(suffixes, key=len, reverse=True):
        for match in re.finditer(re.escape(suffix), text):
            if suffix != "通道":
                values.append(suffix)
            if suffixes == ITEM_SUFFIXES:
                continue
            prefix = text[max(0, match.start() - 5):match.start()]
            named = re.search(r"(?:在|到|从|向|通往|进入|离开|走进|回到|前往|抵达|赶到)([\u4e00-\u9fff]{1,4})$", prefix)
            if named and not any(marker in named.group(1) for marker in ("在", "从", "进", "出", "的", "和", "向")):
                values.append(named.group(1) + suffix)
            introduced = re.search(r"(?:^|[\n。！？])((?:" + "|".join(LOCATION_PREFIXES) + r")[\u4e00-\u9fff]{0,2}" + re.escape(suffix) + r")", text)
            if introduced and not any(marker in introduced.group(1) for marker in ("在", "从", "进", "出", "的", "和", "向")):
                values.append(introduced.group(1))
    return list(dict.fromkeys(value for value in values if len(value) >= 2))


def _source_sentence(text: str, entity: str) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？])", text) if part.strip()]
    sentence = next((part for part in sentences if entity and entity in part), sentences[0] if sentences else text)
    return sentence[:180]


def _looks_like_location(value: str) -> bool:
    return any(value.endswith(suffix) for suffix in LOCATION_SUFFIXES)


def _looks_like_nonperson(value: str) -> bool:
    # These suffixes are frequent nouns/titles in prose and are not personal
    # names even when they begin with a surname character (顾家、沈执事、金丹).
    noun_suffixes = ("氏", "家", "执事", "掌柜", "先生", "姑娘", "公子", "少爷", "小姐", "金丹", "筑基", "令", "牌", "书", "纸", "火", "骨", "印", "镜", "权限", "解决")
    return _looks_like_location(value) or value in {"司机", "主管", "医生", "老师", "警察", "师傅", "老板", "父亲", "母亲", "程序", "安全", "路边", "路面", "马蹄", "洪流", "陆无", "石棺"} or value.endswith(("在", "了", "的", "着", "过", "灯", "门", "车", "厅", "站", "室", "线", "图", "表", "声", "气", "水", "路", "钟", "月", "天", "年")) or value.endswith(noun_suffixes)


def _has_name_action_witness(text: str, value: str) -> bool:
    return bool(re.search(r"(?:^|[\n。！？“”])" + re.escape(value) + r"(?=(?:说|问|答|道|看|想|站|走|来|去|把|将|拿|抬|回|听|冲|赶|塞|按|握|在|没有|正在|终于|也|仍))", text))


def _has_name_introduction_witness(text: str, value: str) -> bool:
    return bool(re.search(r"(?:与|和)" + re.escape(value) + r"(?=(?:一起|一同|核对|交谈|会面|同行|站|走|说|问|，|。))", text))


def _plot_sentence(text: str, names: Sequence[str]) -> str | None:
    sentences = []
    pending = ""
    for part in re.findall(r"[^。！？]+[。！？][”\"]?|[^。！？]+$", text):
        pending += part
        if pending.count("“") != pending.count("”") or pending.count('"') % 2:
            continue
        if pending.strip():
            sentences.append(pending.strip())
        pending = ""
    for sentence in sentences:
        marker = next((value for value in PLOT_ACTION_MARKERS if value in sentence), None)
        if marker and not re.search(r"想[^。！？]{0,8}" + re.escape(marker), sentence) and (any(name in sentence for name in names) or not names):
            return sentence
    return None


def _event_title(names: Sequence[str], text: str) -> str:
    marker = next((value for value in PLOT_ACTION_MARKERS if value in text), "行动")
    subject = next((name for name in names if re.match(r"^[“”\"']*" + re.escape(name) + r"(?!的)", text)), None)
    if marker == "放行" and re.search(r"(?:说|喊|要求|命令)[：:，,]?[“\"]?[^。！？]*(?:必须|要)[^。！？]*放行", text):
        marker = "要求放行"
    return subject + marker if subject else "关键推进：" + marker


def _portable_item(name: str) -> bool:
    return not name.endswith(("灯", "档案", "账本"))


def _event_action_contract(event: Dict[str, Any]) -> Dict[str, Any]:
    summary = event["summary"]
    requested = re.search(r"(?:说|喊|要求|命令)[：:，,]?[“\"]?[^。！？]*(?:必须|要)[^。！？]*放行", summary)
    if requested:
        return {
            "kind": "request", "action": "放行", "completed": False,
            "instruction": "本章只发生提出放行要求及在场人物的反应。提出要求不等于批准或执行；本回合没有登记的设备、人、物及交通状态均不得改变。主角不必赞同说话人的要求。",
            "forbiddenPatterns": [
                r"(?:线路占用|占用状态)[^。！？]{0,6}(?:解除|清除)",
                r"(?:闸门|放行灯|指示灯|信号灯)[^。！？]{0,12}(?:已经开|已开|打开了|转绿|变绿|由红转绿)",
                r"(?:已经|已|完成|执行了)(?:恢复)?放行",
            ],
        }
    return {"kind": "action", "instruction": "只完成当前节点明确描述的动作及登记的状态变化，不扩展到下一节点。"}


def build_story_package(path: Path, analysis: Dict[str, Any], package_id: str, version: str) -> Dict[str, Any]:
    """Compile a verified analysis into generic, executable co-creation data."""
    _validate_package_identity(package_id, version)
    manifest = inspect_standard_novel(path)
    _validate_analysis_identity(manifest, analysis)
    text = Path(path).read_text(encoding="utf-8")
    chapter_models = _normalize_analysis(manifest, analysis, text)
    _assert_semantic_completeness(chapter_models)
    return _compile_package(manifest, chapter_models, package_id, version, text)


def build_source_reader(path: Path, analysis: Dict[str, Any], package: Dict[str, Any]) -> Dict[str, Any]:
    """Create a local-only chapter reader sidecar for a generated package.

    Reader text is deliberately outside StoryPackage and is never included in
    planner context. It exists only for the person choosing an entry chapter.
    """
    manifest = inspect_standard_novel(path)
    _validate_analysis_identity(manifest, analysis)
    source = package.get("sourceAnalysis", {})
    if source.get("sha256") != manifest["source"]["sha256"]:
        raise StoryPackageBuildError("章节阅读器必须绑定当前 StoryPackage 的同一 TXT。")
    text = Path(path).read_text(encoding="utf-8")
    return {
        "schemaVersion": "source-reader/0.1",
        "package": {"id": package.get("id"), "version": package.get("version")},
        "source": copy.deepcopy(manifest["source"]),
        "chapters": [
            {
                "id": chapter["id"], "title": chapter["title"],
                "lineRange": copy.deepcopy(chapter["lineRange"]),
                # Keep the chapter itself intact while removing separator
                # blank lines that belong to the surrounding TXT layout.
                "text": _source_range_text(text, chapter["characterRange"]).strip(),
            }
            for chapter in manifest["chapters"]
        ],
    }


MODULE_INDEX_SCHEMA = "story-package-module-index/0.1"


def build_story_package_modules(
    path: Path, analysis: Dict[str, Any], package: Dict[str, Any], reader: Dict[str, Any],
) -> Dict[str, Any]:
    """Build deterministic, local StoryPackage modules from the same source.

    The directory layout is a generated projection of the fixed StoryPackage,
    never an authoring surface. Its chapter modules contain bounded planning
    data and references only; original chapter text is isolated under
    ``reader/`` for the local reading UI.
    """
    manifest = inspect_standard_novel(path)
    _validate_analysis_identity(manifest, analysis)
    if package.get("sourceAnalysis", {}).get("sha256") != manifest["source"]["sha256"]:
        raise StoryPackageBuildError("模块文件必须绑定当前 StoryPackage 的同一 TXT。")
    if (
        reader.get("schemaVersion") != "source-reader/0.1"
        or reader.get("package") != {"id": package.get("id"), "version": package.get("version")}
        or reader.get("source", {}).get("sha256") != manifest["source"]["sha256"]
    ):
        raise StoryPackageBuildError("模块文件必须绑定同一版本的本地章节阅读器。")

    files: Dict[str, Dict[str, Any]] = {}
    files["world.json"] = {
        "schemaVersion": "story-package-world-module/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "metadata": copy.deepcopy(package["metadata"]),
        "world": copy.deepcopy(package["world"]),
        "rules": copy.deepcopy(package["rules"]),
    }
    files["state-schema.json"] = {
        "schemaVersion": "story-package-state-module/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "initialState": copy.deepcopy(package["initialState"]),
        "stateModel": copy.deepcopy(package["stateModel"]),
    }
    files["main-story-graph.json"] = {
        "schemaVersion": "story-package-graph-module/0.2",
        "package": {"id": package["id"], "version": package["version"]},
        "story": {
            key: copy.deepcopy(package["story"][key])
            for key in ("mode", "premise", "longTermGoal", "startNodeId", "endings")
        },
        "narrativeGraph": {
            key: copy.deepcopy(package["story"]["narrativeGraph"][key])
            for key in ("startBeatId", "edges", "endingBeatIds")
        },
        "directions": copy.deepcopy(package["directions"]),
        "defaultDirectionId": package["defaultDirectionId"],
    }
    files["node-index.json"] = {
        "schemaVersion": "story-package-node-index/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "nodes": [
            {"id": node["id"], "path": "nodes/" + node["id"] + ".json", "locationId": node.get("locationId")}
            for node in package["story"]["nodes"]
        ],
    }
    for node in package["story"]["nodes"]:
        files["nodes/" + node["id"] + ".json"] = {
            "schemaVersion": "story-package-node-module/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "node": copy.deepcopy(node),
        }
    arc_model = package["story"].get("arcModel")
    if not isinstance(arc_model, dict):
        raise StoryPackageBuildError("模块化运行时要求 story.arcModel 为对象。")
    arcs = arc_model.get("arcs")
    if not isinstance(arcs, list):
        raise StoryPackageBuildError("模块化运行时要求 story.arcModel.arcs 为数组。")
    files["arc-model.json"] = {
        "schemaVersion": "story-package-arc-model/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "arcModel": {key: copy.deepcopy(value) for key, value in arc_model.items() if key != "arcs"},
    }
    for arc in arcs:
        files["arcs/" + arc["id"] + ".json"] = {
            "schemaVersion": "story-package-arc-module/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "arc": copy.deepcopy(arc),
        }
    entry_model = package["story"].get("entryModel")
    if not isinstance(entry_model, dict):
        raise StoryPackageBuildError("模块化运行时要求 story.entryModel 为对象。")
    entry_points = entry_model.get("entryPoints")
    if not isinstance(entry_points, list):
        raise StoryPackageBuildError("模块化运行时要求 story.entryModel.entryPoints 为数组。")
    files["entry-model.json"] = {
        "schemaVersion": "story-package-entry-model/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "entryModel": {key: copy.deepcopy(value) for key, value in entry_model.items() if key != "entryPoints"},
    }
    for entry in entry_points:
        files["entries/" + entry["id"] + ".json"] = {
            "schemaVersion": "story-package-entry-module/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "entryPoint": copy.deepcopy(entry),
        }

    entity_specs = (
        ("characters", "character", package["characters"]),
        ("locations", "location", package["locations"]),
        ("items", "item", package["items"]),
        ("relationships", "relationship", package["relationships"]),
    )
    entity_indexes: Dict[str, List[Dict[str, Any]]] = {}
    for folder, kind, entities in entity_specs:
        entity_indexes[folder] = []
        for entity in entities:
            files[f"{folder}/{entity['id']}.json"] = {
                "schemaVersion": f"story-package-{kind}-module/0.1",
                "package": {"id": package["id"], "version": package["version"]},
                kind: copy.deepcopy(entity),
            }
            entity_indexes[folder].append(_module_entity_index_entry(folder, entity))
    for folder, entries in entity_indexes.items():
        files[f"{folder[:-1]}-index.json"] = {
            "schemaVersion": f"story-package-{folder[:-1]}-index/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            folder: entries,
        }

    text = Path(path).read_text(encoding="utf-8")
    chapter_models = _normalize_analysis(manifest, analysis, text)
    plot_nodes = _plot_nodes(chapter_models, text)
    beats = package["story"]["narrativeGraph"]["beats"]
    if len(plot_nodes) != len(beats) or len(package["timeline"]) != len(beats):
        raise StoryPackageBuildError("模块化章节必须与剧情节点、时间线和叙事锚点一一对应。")
    character_ids = {entity["name"]: entity["id"] for entity in package["characters"]}
    location_ids = {entity["name"]: entity["id"] for entity in package["locations"]}
    reader_chapters = {chapter["id"]: chapter for chapter in reader.get("chapters", [])}
    chapter_entries = {chapter["id"]: [] for chapter in manifest["chapters"]}
    chapter_beats = {chapter["id"]: [] for chapter in manifest["chapters"]}
    chapter_timelines = {chapter["id"]: [] for chapter in manifest["chapters"]}
    beat_timelines: Dict[str, List[Dict[str, Any]]] = {}
    for plot_node, beat, timeline in zip(plot_nodes, beats, package["timeline"]):
        chapter_id = plot_node["chapterId"]
        scene_text = "\n".join(cue["text"] for cue in plot_node["narrativeBrief"])
        event_item_ids = [
            item_id for name, item_id in {
                item["name"]: item["id"] for item in package["items"]
            }.items()
            if name in plot_node["sourceExcerpt"]
        ]
        beat_module = {
            "id": beat["id"], "nodeId": beat["nodeId"], "summary": beat["summary"],
            "narrativeBrief": copy.deepcopy(plot_node["narrativeBrief"]),
            "actionContract": _event_action_contract(plot_node["event"]),
            "sourceEvidence": {"lineRange": copy.deepcopy(beat["sourceExcerpt"]["lineRange"])},
            "narrativeAnchor": beat["narrativeAnchor"], "openThreads": copy.deepcopy(beat["openThreads"]),
            "nextDirections": copy.deepcopy(beat["nextDirections"]), "branchState": copy.deepcopy(beat["branchState"]),
            "contextRefs": {
                "characterIds": [identifier for name, identifier in character_ids.items()
                                 if name in plot_node["event"].get("characterNames", []) or name in scene_text],
                "locationIds": [location_ids[name] for name in plot_node["event"].get("locationNames", []) if name in location_ids],
                "itemIds": event_item_ids,
            },
        }
        chapter_beats[chapter_id].append(beat_module)
        chapter_timelines[chapter_id].append(copy.deepcopy(timeline))
        beat_timelines[beat["id"]] = [copy.deepcopy(timeline)]
    for entry in package["story"]["entryModel"]["entryPoints"]:
        chapter_id = entry.get("sourceChapterId")
        if chapter_id in chapter_entries:
            chapter_entries[chapter_id].append(copy.deepcopy(entry))
    items_by_chapter: Dict[str, List[str]] = {chapter["id"]: [] for chapter in manifest["chapters"]}
    facts_by_chapter: Dict[str, List[Dict[str, Any]]] = {chapter["id"]: [] for chapter in manifest["chapters"]}
    for item in package["items"]:
        chapter_id = item.get("availableFromSourceChapterId")
        if chapter_id in items_by_chapter:
            items_by_chapter[chapter_id].append(item["id"])
    for fact in package["world"].get("immutableFacts", []):
        chapter_id = fact.get("sourceChapterId") if isinstance(fact, dict) else None
        if chapter_id in facts_by_chapter:
            facts_by_chapter[chapter_id].append(copy.deepcopy(fact))
    facts_by_beat: Dict[str, List[Dict[str, Any]]] = {
        beat["id"]: []
        for beats_for_chapter in chapter_beats.values()
        for beat in beats_for_chapter
    }
    for chapter_id, chapter_facts in facts_by_chapter.items():
        eligible_beats = [beat for group in chapter_beats.values() for beat in group]
        for fact in chapter_facts:
            line_range = fact.get("lineRange", {}) if isinstance(fact, dict) else {}
            fact_end = line_range.get("end") if isinstance(line_range, dict) else None
            target = next(
                (
                    beat for beat in eligible_beats
                    if isinstance(fact_end, int)
                    and fact_end <= beat["sourceEvidence"]["lineRange"]["end"]
                ),
                eligible_beats[-1] if eligible_beats else None,
            )
            if target is not None:
                facts_by_beat[target["id"]].append(copy.deepcopy(fact))
    arcs = package["story"].get("arcModel", {}).get("arcs", [])
    ordered_beats = [beat for beats_for_chapter in chapter_beats.values() for beat in beats_for_chapter]
    beat_positions = {beat["id"]: index for index, beat in enumerate(ordered_beats)}
    for index, chapter in enumerate(manifest["chapters"]):
        chapter_id = chapter["id"]
        current_beats = chapter_beats[chapter_id]
        direction_ids = {direction["id"] for beat in current_beats for direction in beat["nextDirections"]}
        related_arcs = [copy.deepcopy(arc) for arc in arcs if direction_ids & set(arc.get("phaseDirectionIds", []))]
        plot_character_ids = sorted({
            character_ids[name]
            for plot_node in plot_nodes if plot_node["chapterId"] == chapter_id
            for name in plot_node["event"].get("characterNames", []) if name in character_ids
        })
        plot_location_ids = sorted({
            location_ids[name]
            for plot_node in plot_nodes if plot_node["chapterId"] == chapter_id
            for name in plot_node["event"].get("locationNames", []) if name in location_ids
        })
        files[f"chapters/{chapter_id}.json"] = {
            "schemaVersion": "story-package-chapter-module/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "chapter": {"id": chapter_id, "title": chapter["title"], "lineRange": copy.deepcopy(chapter["lineRange"])},
            "neighborChapterIds": [
                candidate["id"] for candidate in manifest["chapters"][max(0, index - 1):index + 2]
                if candidate["id"] != chapter_id
            ],
            "contextRefs": {
                "characterIds": plot_character_ids, "locationIds": plot_location_ids,
                "itemIds": items_by_chapter[chapter_id],
            },
            "facts": facts_by_chapter[chapter_id], "timeline": chapter_timelines[chapter_id], "beats": current_beats,
            "entryPoints": chapter_entries[chapter_id], "arcs": related_arcs,
        }
        for beat in current_beats:
            position = beat_positions[beat["id"]]
            previous_beat_ids = [item["id"] for item in ordered_beats[max(0, position - 2):position]]
            files[f"beats/{chapter_id}/{beat['id']}.json"] = {
                "schemaVersion": "story-package-beat-module/0.1",
                "package": {"id": package["id"], "version": package["version"]},
                "chapter": {"id": chapter_id, "title": chapter["title"]},
                "beat": copy.deepcopy(beat),
                "previousBeatIds": previous_beat_ids,
                "facts": facts_by_beat[beat["id"]],
                "timeline": beat_timelines[beat["id"]],
            }
        reader_chapter = reader_chapters.get(chapter_id)
        if reader_chapter is None:
            raise StoryPackageBuildError("章节阅读器缺少模块化章节: " + chapter_id)
        files[f"reader/{chapter_id}.json"] = {
            "schemaVersion": "story-package-reader-chapter/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "source": {"sha256": manifest["source"]["sha256"]},
            "chapter": copy.deepcopy(reader_chapter),
        }

    chapter_index = [
        {
            "id": chapter["id"], "title": chapter["title"], "path": f"chapters/{chapter['id']}.json",
            "readerPath": f"reader/{chapter['id']}.json",
            "beatIds": [beat["id"] for beat in chapter_beats[chapter["id"]]],
            "sourceProgressValues": sorted({
                beat["branchState"].get("sourceProgress")
                for beat in chapter_beats[chapter["id"]]
                if isinstance(beat["branchState"].get("sourceProgress"), str)
            }),
        }
        for chapter in manifest["chapters"]
    ]
    chapter_segments = []
    progress_locator = []
    for chapter in chapter_index:
        segment_path = "indexes/chapters/" + chapter["id"] + ".json"
        files[segment_path] = {
            "schemaVersion": "story-package-chapter-index-segment/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "chapter": copy.deepcopy(chapter),
        }
        chapter_segments.append({"id": chapter["id"], "title": chapter["title"], "path": segment_path})
        progress_locator.extend(
            {"sourceProgress": progress, "chapterId": chapter["id"]}
            for progress in chapter["sourceProgressValues"]
        )
    files["chapter-index.json"] = {
        "schemaVersion": "story-package-chapter-index/0.2",
        "package": {"id": package["id"], "version": package["version"]},
        "chapters": chapter_segments,
        "progressLocator": progress_locator,
    }
    chapter_id_by_beat = {
        beat["id"]: chapter_id
        for chapter_id, beats_for_chapter in chapter_beats.items()
        for beat in beats_for_chapter
    }
    beat_entries_by_chapter: Dict[str, List[Dict[str, Any]]] = {chapter["id"]: [] for chapter in manifest["chapters"]}
    beat_locator = []
    for beat in ordered_beats:
        chapter_id = chapter_id_by_beat[beat["id"]]
        entry = {
            "id": beat["id"], "nodeId": beat["nodeId"], "chapterId": chapter_id,
            "sourceProgress": beat["branchState"].get("sourceProgress"),
            "path": "beats/" + chapter_id + "/" + beat["id"] + ".json",
            "previousBeatIds": [item["id"] for item in ordered_beats[max(0, beat_positions[beat["id"]] - 2):beat_positions[beat["id"]]]],
            "timeline": copy.deepcopy(beat_timelines[beat["id"]]),
        }
        beat_entries_by_chapter[chapter_id].append(entry)
        beat_locator.append({key: entry[key] for key in ("id", "chapterId", "sourceProgress")})
    beat_segments = []
    for chapter_id, entries in beat_entries_by_chapter.items():
        segment_path = "indexes/beats/" + chapter_id + ".json"
        files[segment_path] = {
            "schemaVersion": "story-package-beat-index-segment/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "chapterId": chapter_id, "beats": entries,
        }
        beat_segments.append({"chapterId": chapter_id, "path": segment_path})
    files["beat-index.json"] = {
        "schemaVersion": "story-package-beat-index/0.2",
        "package": {"id": package["id"], "version": package["version"]},
        "segments": beat_segments, "locators": beat_locator,
    }
    direction_node_ids = {
        direction["id"]: beat["nodeId"]
        for beat in ordered_beats for direction in beat["nextDirections"]
        if isinstance(direction.get("id"), str)
    }
    arcs_by_node: Dict[str, List[Dict[str, Any]]] = {}
    arc_locator = []
    for arc in arcs:
        node_ids = sorted({direction_node_ids[direction_id] for direction_id in arc.get("phaseDirectionIds", []) if direction_id in direction_node_ids})
        if not node_ids:
            raise StoryPackageBuildError("大方向没有可定位的剧情节点: " + str(arc.get("id")))
        entry = {"id": arc["id"], "title": arc["title"], "summary": arc["summary"], "path": "arcs/" + arc["id"] + ".json"}
        for node_id in node_ids:
            arcs_by_node.setdefault(node_id, []).append(copy.deepcopy(entry))
        arc_locator.append({"id": arc["id"], "nodeId": node_ids[0]})
    arc_segments = []
    for node_id, entries in sorted(arcs_by_node.items()):
        segment_path = "indexes/arcs/" + node_id + ".json"
        files[segment_path] = {
            "schemaVersion": "story-package-arc-index-segment/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "nodeId": node_id, "arcs": entries,
        }
        arc_segments.append({"nodeId": node_id, "path": segment_path})
    files["arc-index.json"] = {
        "schemaVersion": "story-package-arc-index/0.2",
        "package": {"id": package["id"], "version": package["version"]},
        "segments": arc_segments, "locators": arc_locator,
    }
    entry_cards = [
        {
            key: copy.deepcopy(entry[key])
            for key in ("id", "title", "summary", "chapterTitle", "sourceChapterId", "nodeId", "beatId", "sourceCharacterIds", "availableToNewCharacter")
        } | {"path": "entries/" + entry["id"] + ".json"}
        for entry in entry_points
    ]
    entry_selectors = {"sourceCharacters": [], "newCharacter": None}
    entry_locator = []
    for character_id in entry_model.get("sourceCharacterIds", []):
        entries = [copy.deepcopy(entry) for entry in entry_cards if character_id in entry["sourceCharacterIds"]]
        segment_path = "indexes/entries/character_" + character_id + ".json"
        files[segment_path] = {
            "schemaVersion": "story-package-entry-index-segment/0.1",
            "package": {"id": package["id"], "version": package["version"]},
            "selector": {"kind": "source_character", "sourceCharacterId": character_id}, "entryPoints": entries,
        }
        entry_selectors["sourceCharacters"].append({"id": character_id, "path": segment_path})
        for entry in entries:
            if not any(locator["id"] == entry["id"] for locator in entry_locator):
                entry_locator.append({"id": entry["id"], "path": segment_path})
    new_character_entries = [copy.deepcopy(entry) for entry in entry_cards if entry["availableToNewCharacter"]]
    new_character_path = "indexes/entries/new-character.json"
    files[new_character_path] = {
        "schemaVersion": "story-package-entry-index-segment/0.1",
        "package": {"id": package["id"], "version": package["version"]},
        "selector": {"kind": "new_character"}, "entryPoints": new_character_entries,
    }
    entry_selectors["newCharacter"] = {"path": new_character_path}
    for entry in new_character_entries:
        if not any(locator["id"] == entry["id"] for locator in entry_locator):
            entry_locator.append({"id": entry["id"], "path": new_character_path})
    files["entry-index.json"] = {
        "schemaVersion": "story-package-entry-index/0.2",
        "package": {"id": package["id"], "version": package["version"]},
        "selectors": entry_selectors, "locators": entry_locator,
    }
    files["runtime-index.json"] = {
        "schemaVersion": "story-package-runtime-index/0.5",
        "package": {"id": package["id"], "version": package["version"]},
        "coreModules": {
            "world": "world.json",
            "state": "state-schema.json",
            "story": "main-story-graph.json",
            "nodes": "node-index.json",
            "arcModel": "arc-model.json",
            "arcs": "arc-index.json",
            "entryModel": "entry-model.json",
            "entries": "entry-index.json",
            "beats": "beat-index.json",
            "chapters": "chapter-index.json",
        },
        "entityIndexes": {
            "characters": "character-index.json",
            "locations": "location-index.json",
            "items": "item-index.json",
            "relationships": "relationship-index.json",
        },
    }
    descriptors = [
        {"path": relative_path, "sha256": _module_sha256(content)}
        for relative_path, content in sorted(files.items())
    ]
    index = {
        "schemaVersion": MODULE_INDEX_SCHEMA,
        "package": {"id": package["id"], "version": package["version"]},
        "source": copy.deepcopy(manifest["source"]),
        "sourceAnalysis": copy.deepcopy(package["sourceAnalysis"]),
        "modules": descriptors,
        "indexes": {
            "chapters": "chapter-index.json", "characters": "character-index.json",
            "locations": "location-index.json", "items": "item-index.json",
            "relationships": "relationship-index.json", "nodes": "node-index.json", "arcs": "arc-index.json",
            "entries": "entry-index.json", "beats": "beat-index.json",
            "runtime": "runtime-index.json",
        },
    }
    package["moduleIndexSha256"] = _module_sha256(index)
    files["package-index.json"] = index
    return {"index": index, "files": files}


def write_story_package_modules(output_directory: Path, modules: Dict[str, Any]) -> List[Path]:
    """Write only generated module paths; unrelated files are never removed."""
    written: List[Path] = []
    for relative_path, content in modules["files"].items():
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise StoryPackageBuildError("模块输出路径不安全: " + relative_path)
        target = output_directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return written


def read_story_package_modules(output_directory: Path) -> Dict[str, Any]:
    """Read a generated module tree for offline integrity auditing."""
    index_path = output_directory / "package-index.json"
    if not index_path.is_file():
        raise StoryPackageBuildError("模块目录缺少 package-index.json")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoryPackageBuildError("无法读取模块索引：" + str(error)) from error
    descriptors = index.get("modules") if isinstance(index, dict) else None
    if not isinstance(descriptors, list):
        raise StoryPackageBuildError("模块索引缺少 modules 列表")
    files: Dict[str, Dict[str, Any]] = {}
    for descriptor in descriptors:
        relative_path = descriptor.get("path") if isinstance(descriptor, dict) else None
        if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            raise StoryPackageBuildError("模块索引包含不安全的路径")
        path = output_directory / relative_path
        if not path.is_file():
            raise StoryPackageBuildError("模块目录缺少索引声明的文件: " + relative_path)
        try:
            files[relative_path] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoryPackageBuildError("无法读取模块文件 " + relative_path + "：" + str(error)) from error
    files["package-index.json"] = index
    return {"index": index, "files": files}


def audit_story_package_modules(
    path: Path, analysis: Dict[str, Any], package: Dict[str, Any], reader: Dict[str, Any], modules: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate that a module tree is complete and byte-stable for one package."""
    issues: List[Dict[str, str]] = []
    try:
        expected = build_story_package_modules(path, analysis, package, reader)
    except StoryPackageBuildError as error:
        return {"schemaVersion": "story-package-module-audit/0.1", "status": "failed", "issues": [{"id": "module_input", "message": str(error)}]}
    files = modules.get("files") if isinstance(modules, dict) else None
    index = modules.get("index") if isinstance(modules, dict) else None
    if not isinstance(files, dict) or not isinstance(index, dict):
        issues.append({"id": "module_shape", "message": "模块审计输入必须包含 index 和 files。"})
        files, index = {}, {}
    if index.get("schemaVersion") != MODULE_INDEX_SCHEMA:
        issues.append({"id": "module_index_schema", "message": "模块索引 schemaVersion 不匹配。"})
    if index.get("package") != {"id": package["id"], "version": package["version"]}:
        issues.append({"id": "module_package_ref", "message": "模块索引未绑定当前 StoryPackage 版本。"})
    if index.get("source", {}).get("sha256") != package["sourceAnalysis"]["sha256"]:
        issues.append({"id": "module_source_hash", "message": "模块索引未绑定当前母本 SHA-256。"})
    if package.get("moduleIndexSha256") != _module_sha256(index):
        issues.append({"id": "module_index_hash", "message": "StoryPackage 未绑定当前模块索引哈希。"})
    if index != expected["index"]:
        issues.append({"id": "module_index_content", "message": "模块索引与当前脚本编译结果不一致。"})
    expected_paths = set(expected["files"])
    actual_paths = set(files)
    if actual_paths != expected_paths:
        issues.append({"id": "module_path_coverage", "message": "模块文件路径集合与脚本生成结果不一致。"})
    for relative_path in sorted(expected_paths & actual_paths):
        if relative_path == "package-index.json":
            continue
        if files[relative_path] != expected["files"][relative_path]:
            issues.append({"id": "module_content", "message": "模块内容与当前脚本编译结果不一致: " + relative_path})
            continue
        descriptor = next((item for item in index.get("modules", []) if item.get("path") == relative_path), None)
        if not isinstance(descriptor, dict) or descriptor.get("sha256") != _module_sha256(files[relative_path]):
            issues.append({"id": "module_hash", "message": "模块索引哈希不匹配: " + relative_path})
    return {
        "schemaVersion": "story-package-module-audit/0.1",
        "status": "passed" if not issues else "failed",
        "package": {"id": package["id"], "version": package["version"]},
        "checks": {
            "moduleCount": len(expected_paths), "chapterModuleCount": len(expected["files"]["chapter-index.json"]["chapters"]),
            "beatModuleCount": len(expected["files"]["beat-index.json"]["locators"]),
            "nodeModuleCount": len(expected["files"]["node-index.json"]["nodes"]),
            "arcModuleCount": len(expected["files"]["arc-index.json"]["locators"]),
            "entryModuleCount": len(expected["files"]["entry-index.json"]["locators"]),
            "runtimeIndexPresent": "runtime-index.json" in expected["files"],
            "characterModuleCount": len(package["characters"]), "locationModuleCount": len(package["locations"]),
            "itemModuleCount": len(package["items"]), "relationshipModuleCount": len(package["relationships"]),
        },
        "issues": issues,
    }


def _module_sha256(content: Dict[str, Any]) -> str:
    payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _module_entity_index_entry(folder: str, entity: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact lookup card without copying module-level detail."""
    path = f"{folder}/{entity['id']}.json"
    if folder == "characters":
        return {
            "id": entity["id"], "name": entity["name"], "role": entity["role"],
            "menuDescription": entity.get("menuDescription") or _character_menu_description(
                entity["name"], entity.get("sourceDescriptions", [entity.get("description", "")]),
            ),
            "sourceImportance": entity.get("sourceImportance"), "tags": copy.deepcopy(entity.get("tags", [])), "path": path,
            **{key: copy.deepcopy(entity[key]) for key in (
                "defaultEntryPointId", "roleGroup", "identitySummary", "openingHook", "portraitAsset", "playable", "rosterVisible",
            ) if key in entity},
        }
    if folder == "locations":
        return {
            "id": entity["id"], "name": entity["name"], "tags": copy.deepcopy(entity.get("tags", [])),
            "exits": copy.deepcopy(entity.get("exits", [])), "path": path,
        }
    if folder == "items":
        return {
            "id": entity["id"], "name": entity["name"], "portable": entity.get("portable", False),
            "availableFromSourceChapterId": entity.get("availableFromSourceChapterId"),
            "initialHolderId": entity.get("initialHolderId"),
            "initialOwnerCharacterId": entity.get("initialOwnerCharacterId"), "path": path,
        }
    return {
        "id": entity["id"], "fromCharacterId": entity["fromCharacterId"],
        "toCharacterId": entity["toCharacterId"], "sourceChapterId": entity.get("sourceChapterId"), "path": path,
    }


def audit_story_package(path: Path, analysis: Dict[str, Any], package: Dict[str, Any]) -> Dict[str, Any]:
    """Return a machine-readable release gate for a generated StoryPackage."""
    manifest = inspect_standard_novel(path)
    issues: List[Dict[str, str]] = []
    try:
        _validate_analysis_identity(manifest, analysis)
        chapter_models = _normalize_analysis(manifest, analysis, Path(path).read_text(encoding="utf-8"))
        _assert_semantic_completeness(chapter_models)
    except StoryPackageBuildError as error:
        issues.append({"id": "source_analysis", "message": str(error)})
        chapter_models = []
    try:
        validate_story_package(package)
    except ValueError as error:
        issues.append({"id": "story_package_schema", "message": str(error)})
    source_ref = package.get("sourceAnalysis", {})
    if source_ref.get("sha256") != manifest["source"]["sha256"]:
        issues.append({"id": "source_hash", "message": "StoryPackage 未绑定当前 TXT 的 SHA-256。"})
    beats = package.get("story", {}).get("narrativeGraph", {}).get("beats", [])
    source_text = Path(path).read_text(encoding="utf-8")
    plot_nodes = _plot_nodes(chapter_models, source_text) if chapter_models else []
    if len(beats) != len(plot_nodes):
        issues.append({"id": "plot_node_coverage", "message": "每个带证据的关键剧情节点必须生成一个叙事锚点。"})
    for index, (plot_node, beat) in enumerate(zip(plot_nodes, beats), start=1):
        excerpt = beat.get("sourceExcerpt", {}) if isinstance(beat, dict) else {}
        if excerpt.get("text") != plot_node["sourceExcerpt"]:
            issues.append({"id": "source_excerpt_" + str(index), "message": f"{plot_node['id']} 的原文锚点不完整或不匹配。"})
        if beat.get("branchState", {}).get("sourceProgress") != _progress_value(index):
            issues.append({"id": "progress_state_" + str(index), "message": f"{plot_node['id']} 缺少连续进度状态。"})
    character_names = [item.get("name") for item in package.get("characters", []) if isinstance(item, dict)]
    major_names = {item["name"] for chapter in chapter_models for item in chapter["characters"] if item["importance"] == "major"}
    if not major_names.issubset(set(character_names)):
        issues.append({"id": "major_character_coverage", "message": "重要角色未完整写入 StoryPackage。"})
    entry_model = package.get("story", {}).get("entryModel", {})
    entry_character_ids = set(entry_model.get("sourceCharacterIds", [])) if isinstance(entry_model, dict) else set()
    compiled_characters = package.get("characters", [])
    compiled_by_id = {item.get("id"): item for item in compiled_characters if isinstance(item, dict)}
    official = entry_model.get("policy") == "official_unknown_reader/1"
    if (not official and len(entry_character_ids) != len(major_names)) or any(
        character_id not in compiled_by_id or compiled_by_id[character_id].get("sourceImportance") != "major"
        for character_id in entry_character_ids
    ):
        issues.append({"id": "entry_character_coverage", "message": "入口可选原著角色必须与脚本识别的重要角色一致。"})
    if official:
        from .official_openings import validate_official_openings
        try:
            validate_official_openings(package)
        except (ValueError, KeyError, TypeError) as error:
            issues.append({"id": "official_openings", "message": str(error)})
    for character in compiled_characters:
        if not isinstance(character, dict):
            continue
        name = character.get("name")
        description = character.get("description")
        if not isinstance(name, str) or not isinstance(description, str) or name not in description:
            issues.append({"id": "character_card_" + str(character.get("id", "unknown")), "message": "角色卡必须保留角色本人名称的原文证据。"})
    expected_relationships = {
        (relation["fromName"], relation["toName"], relation["description"])
        for chapter in chapter_models for relation in chapter["relations"]
    }
    actual_relationships = package.get("relationships", [])
    if len(actual_relationships) != len(expected_relationships):
        issues.append({"id": "relationship_coverage", "message": "已提取关系未完整写入 StoryPackage。"})
    location_ids = {location.get("id") for location in package.get("locations", []) if isinstance(location, dict)}
    chapter_ids = {chapter["id"] for chapter in chapter_models}
    located_items = 0
    for item in package.get("items", []):
        if not isinstance(item, dict):
            continue
        source_location_id = item.get("sourceFirstSeenLocationId")
        if source_location_id is not None:
            if source_location_id not in location_ids:
                issues.append({"id": "item_source_location", "message": f"物品 {item.get('name')} 引用了不存在的首次可见地点。"})
            else:
                located_items += 1
        if item.get("availableFromSourceChapterId") not in chapter_ids:
            issues.append({"id": "item_source_chapter", "message": f"物品 {item.get('name')} 缺少有效的首次可见章节。"})
    topology_exit_count = sum(len(location.get("exits", [])) for location in package.get("locations", []) if isinstance(location, dict))
    return {
        "schemaVersion": "story-package-audit/0.1",
        "status": "passed" if not issues else "failed",
        "source": {"fileName": manifest["source"]["fileName"], "sha256": manifest["source"]["sha256"]},
        "package": {"id": package.get("id"), "version": package.get("version")},
        "checks": {
            "chapterCount": len(manifest["chapters"]), "plotNodeCount": len(plot_nodes),
            "beatCount": len(beats),
            "importantCharacterCount": len(major_names),
            "locationCount": len(package.get("locations", [])),
            "itemCount": len(package.get("items", [])),
            "sourceLocatedItemCount": located_items,
            "topologyExitCount": topology_exit_count,
            "relationshipCount": len(package.get("relationships", [])),
        },
        "issues": issues,
    }


def _chapter_fragments(chapter: Dict[str, Any], text: str, maximum: int) -> List[Dict[str, Any]]:
    paragraphs = chapter.get("paragraphs") or []
    if not paragraphs:
        return [{
            "id": chapter["id"] + "-fragment-001", "paragraphIds": [],
            "text": _source_range_text(text, chapter["characterRange"]),
        }]
    fragments: List[Dict[str, Any]] = []
    active: List[Dict[str, Any]] = []
    size = 0
    for paragraph in paragraphs:
        paragraph_text = _source_range_text(text, paragraph["characterRange"])
        if active and size + len(paragraph_text) > maximum:
            fragments.append(_fragment_from_paragraphs(chapter, active, text, len(fragments) + 1))
            active, size = [], 0
        active.append(paragraph)
        size += len(paragraph_text)
    if active:
        fragments.append(_fragment_from_paragraphs(chapter, active, text, len(fragments) + 1))
    return fragments


def _fragment_from_paragraphs(chapter: Dict[str, Any], paragraphs: Sequence[Dict[str, Any]], text: str, index: int) -> Dict[str, Any]:
    start = paragraphs[0]["characterRange"]["start"]
    end = paragraphs[-1]["characterRange"]["end"]
    return {
        "id": f"{chapter['id']}-fragment-{index:03d}",
        "paragraphIds": [paragraph["id"] for paragraph in paragraphs],
        "paragraphs": [
            {"id": paragraph["id"], "text": _source_range_text(text, paragraph["characterRange"])}
            for paragraph in paragraphs
        ],
        "text": text[start:end],
    }


def _fragment_reference(fragment: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": fragment["id"], "paragraphIds": list(fragment["paragraphIds"])}


def _validate_package_identity(package_id: str, version: str) -> None:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", package_id):
        raise StoryPackageBuildError("package_id 必须是小写 kebab-case")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise StoryPackageBuildError("version 必须是 x.y.z")


def _validate_analysis_identity(manifest: Dict[str, Any], analysis: Dict[str, Any]) -> None:
    if analysis.get("schemaVersion") != "source-semantic-analysis/0.1":
        raise StoryPackageBuildError("语义分析文件 schemaVersion 必须为 source-semantic-analysis/0.1")
    source = analysis.get("source", {})
    if source.get("sha256") != manifest["source"]["sha256"]:
        raise StoryPackageBuildError("语义分析文件不属于当前 TXT 母本")
    chapters = analysis.get("chapters")
    expected = [chapter["id"] for chapter in manifest["chapters"]]
    actual = [chapter.get("chapterId") for chapter in chapters] if isinstance(chapters, list) else []
    if actual != expected:
        raise StoryPackageBuildError("语义分析必须按顺序覆盖 TXT 的每一个章节")


def _normalize_analysis(manifest: Dict[str, Any], analysis: Dict[str, Any], text: str) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for chapter, raw in zip(manifest["chapters"], analysis["chapters"]):
        allowed_paragraph_ids = {paragraph["id"] for paragraph in chapter.get("paragraphs", [])}
        chapter_text = _source_range_text(text, chapter["characterRange"])
        values: Dict[str, List[Dict[str, Any]]] = {key: [] for key in ("characters", "locations", "items", "relations", "facts", "events")}
        summaries: List[str] = []
        fragments = raw.get("fragments")
        if not isinstance(fragments, list) or not fragments:
            raise StoryPackageBuildError(chapter["id"] + " 缺少片段语义分析")
        for fragment in fragments:
            reference = fragment.get("fragment", {})
            allowed = set(reference.get("paragraphIds", []))
            if not allowed.issubset(allowed_paragraph_ids):
                raise StoryPackageBuildError(chapter["id"] + " 引用了其他章节的段落")
            candidate = fragment.get("candidate")
            if not isinstance(candidate, dict):
                raise StoryPackageBuildError(chapter["id"] + " 的片段候选必须为对象")
            summary = candidate.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary.strip())
            for key in values:
                raw_entries = candidate.get(key, [])
                if not isinstance(raw_entries, list):
                    raise StoryPackageBuildError(chapter["id"] + " 的 " + key + " 必须为数组")
                for entry in raw_entries:
                    values[key].append(_normalize_candidate_entry(key, entry, allowed, chapter_text, chapter["id"]))
        normalized.append({
            "id": chapter["id"], "title": chapter["title"], "lineRange": copy.deepcopy(chapter["lineRange"]),
            "characterRange": copy.deepcopy(chapter["characterRange"]), "text": chapter_text,
            "paragraphs": copy.deepcopy(chapter.get("paragraphs", [])),
            "paragraphTexts": {
                paragraph["id"]: _source_range_text(text, paragraph["characterRange"])
                for paragraph in chapter.get("paragraphs", [])
            },
            "summary": _deduplicated_summary(summaries, chapter["title"]), **{key: _dedupe_entries(key, entries) for key, entries in values.items()},
        })
    return normalized


def _normalize_candidate_entry(kind: str, entry: Any, allowed: set[str], chapter_text: str, chapter_id: str) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise StoryPackageBuildError(chapter_id + " 的 " + kind + " 候选必须是对象")
    evidence = entry.get("evidenceParagraphIds")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(value, str) or value not in allowed for value in evidence):
        raise StoryPackageBuildError(chapter_id + " 的 " + kind + " 候选必须引用本片段段落")
    value = copy.deepcopy(entry)
    value["evidenceParagraphIds"] = list(dict.fromkeys(evidence))
    text_fields = {
        "characters": ("name", "role", "description"), "locations": ("name", "description"),
        "items": ("name", "description"), "relations": ("fromName", "toName", "description"),
        "facts": ("text",), "events": ("title", "summary"),
    }[kind]
    for field in text_fields:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise StoryPackageBuildError(chapter_id + " 的 " + kind + " 缺少 " + field)
    if kind in ("characters", "locations", "items") and value["name"] not in chapter_text:
        raise StoryPackageBuildError(chapter_id + " 的 " + kind + " 名称未出现在原文: " + value["name"])
    if kind == "characters":
        if value.get("importance") not in ("major", "minor"):
            raise StoryPackageBuildError(chapter_id + " 的角色 importance 必须为 major 或 minor")
    if kind == "items" and not isinstance(value.get("portable"), bool):
        raise StoryPackageBuildError(chapter_id + " 的物品 portable 必须是布尔值")
    if kind == "facts" and "communication" in value:
        communication = value["communication"]
        if (
            not isinstance(communication, dict)
            or not isinstance(communication.get("speakerName"), str)
            or not isinstance(communication.get("kind"), str)
        ):
            raise StoryPackageBuildError(chapter_id + " 的通信事实声明无效")
    for list_field in ("characterNames", "locationNames", "openThreads"):
        if kind == "events":
            raw = value.get(list_field, [])
            if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
                raise StoryPackageBuildError(chapter_id + " 的事件 " + list_field + " 必须为字符串数组")
            value[list_field] = list(dict.fromkeys(raw))
    return value


def _dedupe_entries(kind: str, entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key_fields = {"characters": ("name",), "locations": ("name",), "items": ("name",), "relations": ("fromName", "toName", "description"), "facts": ("text",), "events": ("title", "summary")}[kind]
    merged: Dict[tuple[str, ...], Dict[str, Any]] = {}
    for entry in entries:
        key = tuple(entry[field] for field in key_fields)
        if key not in merged:
            merged[key] = copy.deepcopy(entry)
            continue
        existing = merged[key]
        existing["evidenceParagraphIds"] = list(dict.fromkeys(existing["evidenceParagraphIds"] + entry["evidenceParagraphIds"]))
        existing.setdefault("sourceDescriptions", [existing.get("description", "")])
        if entry.get("description") and entry["description"] not in existing["sourceDescriptions"]:
            existing["sourceDescriptions"].append(entry["description"])
        if kind == "characters" and entry.get("importance") == "major":
            existing["importance"] = "major"
    return list(merged.values())


def _deduplicated_summary(summaries: List[str], fallback: str) -> str:
    unique = list(dict.fromkeys(summary for summary in summaries if summary))
    return "；".join(unique[:2]) or fallback


def _assert_semantic_completeness(chapters: List[Dict[str, Any]]) -> None:
    if not chapters:
        raise StoryPackageBuildError("小说必须包含至少一个章节")
    if not any(character["importance"] == "major" for chapter in chapters for character in chapter["characters"]):
        raise StoryPackageBuildError("语义分析没有识别到任何重要角色")
    if not any(chapter["locations"] for chapter in chapters):
        raise StoryPackageBuildError("语义分析没有识别到任何地点")
    if not any(chapter["items"] for chapter in chapters):
        raise StoryPackageBuildError("语义分析没有识别到任何关键物品")
    character_names = {character["name"] for chapter in chapters for character in chapter["characters"]}
    location_names = {location["name"] for chapter in chapters for location in chapter["locations"]}
    for chapter in chapters:
        for relation in chapter["relations"]:
            if relation["fromName"] not in character_names or relation["toName"] not in character_names:
                raise StoryPackageBuildError(chapter["id"] + " 的关系引用了未登记角色")
        for event in chapter["events"]:
            if any(name not in character_names for name in event["characterNames"]):
                raise StoryPackageBuildError(chapter["id"] + " 的事件引用了未登记角色")
            if any(name not in location_names for name in event["locationNames"]):
                raise StoryPackageBuildError(chapter["id"] + " 的事件引用了未登记地点")
        if not chapter["events"]:
            raise StoryPackageBuildError(chapter["id"] + " 缺少关键剧情节点，不能生成连续剧情")


def _compile_package(manifest: Dict[str, Any], chapters: List[Dict[str, Any]], package_id: str, version: str, text: str) -> Dict[str, Any]:
    characters = _compile_characters(chapters, text)
    locations = _compile_locations(chapters)
    character_ids = {item["name"]: item["id"] for item in characters}
    location_ids = {item["name"]: item["id"] for item in locations}
    items = _compile_items(chapters, location_ids, character_ids)
    for location in locations:
        location["discoverableItemIds"] = [
            item["id"] for item in items if item.get("sourceFirstSeenLocationId") == location["id"]
        ]
    canonical_player = next(item for item in characters if item["importance"] == "major")
    plot_nodes = _plot_nodes(chapters, text)
    facts = _compile_facts(chapters, plot_nodes)
    timeline = [
        {"id": "timeline_" + _stable_id(plot_node["id"]), "order": index, "knownAtStart": index == 1, "description": plot_node["event"]["summary"]}
        for index, plot_node in enumerate(plot_nodes, start=1)
    ]
    beats = _compile_beats(plot_nodes, location_ids, locations[0]["id"])
    directions = [direction for beat in beats for direction in beat["nextDirections"]]
    nodes = [{
        "id": "node_source_continuity", "objective": "在不改写母本事实的前提下推进当前章节后的剧情。",
        "sceneSetup": ["章节、角色、地点、时间线均来自已审核母本。"], "requiredProgress": [], "transitions": [],
        "contextRefs": [canonical_player["id"], beats[0]["branchState"]["playerLocationId"]],
    }]
    important = [item for item in characters if item["importance"] == "major"]
    package: Dict[str, Any] = {
        "schemaVersion": "1.0", "id": package_id, "version": version,
        "metadata": {
            "title": manifest["source"]["title"]["value"], "summary": chapters[0]["summary"],
            "authoringSource": "source_text_script", "contentRating": "unspecified", "language": "zh-CN",
            "chapterHeadingStyle": _chapter_heading_style(manifest),
        },
        "sourceAnalysis": {"fileName": manifest["source"]["fileName"], "sha256": manifest["source"]["sha256"], "schemaVersion": "source-semantic-analysis/0.1"},
        "world": {
            "premise": chapters[0]["summary"],
            "immutableFacts": facts,
            "narrativeGuidelines": {
                "perspective": "third_person_limited", "focalCharacterId": canonical_player["id"], "tense": "present", "language": "zh-CN",
                "turnLengthCharacters": {"min": 2000, "max": 2500},
                "characterLocationStateFields": {canonical_player["id"]: "playerLocationId"},
                "prohibitions": ["不得改写已确认的母本事实", "不得替玩家声明未输入的行动"],
            },
            "globalConstraints": ["运行时模型只能承接 StoryPackage 的压缩上下文，不读取母本全文。"],
        },
        "characters": [{key: value for key, value in item.items() if key != "importance"} for item in characters],
        "locations": locations, "items": items, "relationships": _compile_relationships(chapters, character_ids), "timeline": timeline,
        "story": {
            "mode": "co_creation", "premise": chapters[0]["summary"], "longTermGoal": chapters[-1]["summary"],
            "startNodeId": "node_source_continuity", "nodes": nodes,
            "endings": [{"id": "ending_source_terminal", "title": "母本当前结局", "summary": chapters[-1]["summary"]}],
            "narrativeGraph": {"startBeatId": beats[0]["id"], "beats": beats, "edges": [], "endingBeatIds": {"ending_source_terminal": beats[-1]["id"]}},
            "arcModel": _compile_arc_model(plot_nodes, directions),
            "entryModel": _compile_entry_model(plot_nodes, beats, important, timeline),
        },
        "directions": [{"id": "direction_default", "title": "沿当前章节继续", "summary": "基于当前已确认状态推进剧情。"}], "defaultDirectionId": "direction_default",
        "rules": {"actionTypes": ["investigate", "negotiate", "risk"], "check": {"randomRange": {"min": 1, "max": 6}, "successAt": 5, "partialSuccessAt": 4}, "attributes": ["insight", "empathy", "nerve"], "resolutions": [], "guards": [], "terminalRules": []},
        "initialState": {
            "player": {"characterId": canonical_player["id"], "attributes": {"insight": 2, "empathy": 2, "nerve": 2}},
            "inventory": [], "relationships": {}, "flags": {}, "counters": {}, "knownFacts": [timeline[0]["id"]], "freeTextProgress": 0,
            "currentNodeId": "node_source_continuity", "currentLocationId": beats[0]["branchState"]["playerLocationId"], "directionId": "direction_default",
        },
        "stateModel": {
            "runtimeStateFields": ["storyScope", "derivativeStage", "derivedTurn", "freeTextProgress", "characterLocationIds", "itemOwnerCharacterIds"], "locationReferenceFields": ["playerLocationId"],
            "immutableFields": ["storyScope", "itemOwnerCharacterIds"], "monotonicEnums": {"sourceProgress": [_progress_value(index) for index in range(1, len(plot_nodes) + 1)], "derivativeStage": ["inactive", "setup", "active"]},
            "transitionRules": [], "invariants": [],
            "narrativeAssertions": _compile_narrative_assertions(facts, len(plot_nodes)), "mockFollowups": [],
        },
    }
    validate_story_package(package)
    return package


def _compile_characters(chapters: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for chapter in chapters:
        paragraphs = {paragraph["id"]: paragraph for paragraph in chapter.get("paragraphs", [])}
        for candidate in chapter["characters"]:
            existing = merged.get(candidate["name"])
            if existing is None:
                merged[candidate["name"]] = {
                    "id": "character_" + _stable_id(candidate["name"]), **copy.deepcopy(candidate),
                    "initialLocationId": None, "tags": [candidate["role"]], "sourceDescriptionEvidence": [],
                }
                existing = merged[candidate["name"]]
            elif candidate["importance"] == "major":
                existing["importance"] = "major"
            if existing is not None:
                descriptions = existing.setdefault("sourceDescriptions", [existing["description"]])
                for description in candidate.get("sourceDescriptions", [candidate["description"]]):
                    if description and description not in descriptions:
                        descriptions.append(description)
                    evidence_ids = candidate.get("evidenceParagraphIds", [])
                    evidence_paragraphs = [paragraphs[item] for item in evidence_ids if item in paragraphs]
                    matching_paragraphs = [
                        paragraph for paragraph in evidence_paragraphs
                        if description in text[paragraph["characterRange"]["start"]:paragraph["characterRange"]["end"]]
                    ]
                    if matching_paragraphs:
                        evidence_paragraphs = matching_paragraphs
                    if description and evidence_paragraphs:
                        evidence = {
                            "text": description,
                            "chapterId": chapter["id"],
                            "lineRange": {
                                "start": min(item["lineRange"]["start"] for item in evidence_paragraphs),
                                "end": max(item["lineRange"]["end"] for item in evidence_paragraphs),
                            },
                        }
                        if evidence not in existing["sourceDescriptionEvidence"]:
                            existing["sourceDescriptionEvidence"].append(evidence)
    for character in merged.values():
        descriptions = character.get("sourceDescriptions", [character["description"]])
        character["description"] = _character_card_description(character["name"], descriptions)
        character["menuDescription"] = _character_menu_description(character["name"], descriptions)
        character["sourceImportance"] = character["importance"]
    return list(merged.values())


def _character_card_description(name: str, descriptions: Sequence[str]) -> str:
    """Select a cited role-card line without inventing a biography."""
    def score(description: str) -> tuple[int, int]:
        value = description.strip()
        points = 0
        quoted = value.startswith(("“", "”", "\""))
        if not quoted and re.search(r"(?:那是|写着[：:]|姓名[：:]|名叫|叫做)" + re.escape(name), value):
            points += 80
        if re.match(re.escape(name), value):
            points += 45
        if re.search(re.escape(name) + r"[^。！？]{0,12}(?:是|为|担任|任职|负责|被困|赶到|进入|发现|整理|夜班|值班)", value):
            points += 30
        if re.match(re.escape(name) + r"[^。！？]{0,8}(?:想|跌|扶|按|走|站|说|问|看|拿|抱|整理|检查)", value):
            points += 15
        if quoted:
            points -= 50
        if value.find(name) > 12:
            points -= 15
        return points, -len(value)

    candidates = [value.strip() for value in descriptions if isinstance(value, str) and name in value and value.strip()]
    return max(candidates, key=score, default=name + "在母本中被明确提及。")


def _character_menu_description(name: str, descriptions: Sequence[str]) -> str:
    """Compile a concise source-grounded initial role card for the entry menu."""
    role_pattern = r"(?:主管|夜班维修|维修工|维修员|站务员|记者|司机|医生|警察|教师|学生)"
    candidates = [value.strip() for value in descriptions if isinstance(value, str) and name in value and value.strip()]
    for value in candidates:
        explicit_role = re.search(
            r"(?:那是|写着[：:]|姓名[：:]|名叫|叫做)?" + re.escape(name)
            + r"[，,:：]?([^。！？]{0,24}" + role_pattern + r")",
            value,
        )
        if explicit_role is not None:
            return explicit_role.group(1).strip(" ，,:：") + "。"
    for value in candidates:
        if re.search(re.escape(name) + r"[^。！？]{0,24}赶到", value):
            destination = re.search(r"赶到([\u4e00-\u9fff]{2,8})", value)
            place = destination.group(1) if destination is not None else "现场"
            return "赶到" + place + "、追查失联线索的人。"
    for value in candidates:
        if re.search(re.escape(name) + r"[^。！？]{0,24}(?:记录|档案|拍下来|调查)", value):
            return "追查异常记录的人。"
    for value in candidates:
        if re.search(re.escape(name) + r"[^。！？]{0,24}被困", value):
            return "被困在事件现场的关键当事人。"
    return "母本开场已出现的关键角色。"


def _chapter_heading_style(manifest: Dict[str, Any]) -> str | None:
    """Record the source heading convention so generated chapters can retain it."""
    headings = [chapter.get("heading") for chapter in manifest.get("chapters", []) if isinstance(chapter, dict)]
    for heading in headings:
        if not isinstance(heading, str):
            continue
        value = heading.strip()
        if re.match(r"^[一二三四五六七八九十百千万零〇两]+[、.．]", value):
            return "chinese_dunhao"
        if re.match(r"^\d+[、.．]", value):
            return "arabic_dunhao"
        if re.match(r"^第\s*[一二三四五六七八九十百千万零〇两]+\s*[章节回篇]", value):
            return "chinese_chapter"
        if re.match(r"^第\s*\d+\s*[章节回篇]", value):
            return "arabic_chapter"
    return None


def _compile_locations(chapters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for chapter in chapters:
        for candidate in chapter["locations"]:
            location = merged.setdefault(candidate["name"], {"id": "location_" + _stable_id(candidate["name"]), "name": candidate["name"], "description": candidate["description"], "exits": [], "discoverableItemIds": [], "tags": ["source"]})
            descriptions = location.setdefault("sourceDescriptions", [location["description"]])
            for description in candidate.get("sourceDescriptions", [candidate["description"]]):
                if description and description not in descriptions:
                    descriptions.append(description)
    for from_name, to_name in _source_location_links(chapters, set(merged)):
        from_location = merged[from_name]
        to_location = merged[to_name]
        if to_location["id"] not in from_location["exits"]:
            from_location["exits"].append(to_location["id"])
        if from_location["id"] not in to_location["exits"]:
            to_location["exits"].append(from_location["id"])
    return list(merged.values())


def _compile_items(
    chapters: List[Dict[str, Any]], location_ids: Dict[str, str], character_ids: Dict[str, str],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for chapter in chapters:
        scene_locations = _chapter_scene_locations(chapter, set(location_ids))
        for candidate in chapter["items"]:
            source_location_id = _first_seen_item_location(candidate, scene_locations, location_ids)
            item = {
                "id": "item_" + _stable_id(candidate["name"]), "name": candidate["name"],
                "portable": candidate["portable"], "effects": [candidate["description"]],
                "availableFromSourceChapterId": chapter["id"],
            }
            if source_location_id:
                item["sourceFirstSeenLocationId"] = source_location_id
            item = merged.setdefault(candidate["name"], item)
            descriptions = item.setdefault("sourceDescriptions", [item["effects"][0]])
            for description in candidate.get("sourceDescriptions", [candidate["description"]]):
                if description and description not in descriptions:
                    descriptions.append(description)
            if "initialOwnerCharacterId" not in item:
                owner_id = _first_seen_item_owner(item["name"], item.get("sourceDescriptions", []), character_ids)
                if owner_id is not None:
                    item["initialOwnerCharacterId"] = owner_id
    return list(merged.values())


def _first_seen_item_owner(item_name: str, descriptions: Sequence[str], character_ids: Dict[str, str]) -> str | None:
    """Extract an explicit source holder without inferring ownership from proximity."""
    holder_markers = r"(?:腰间挂着|手里(?:拿着|握着)?|握着|拿着|带着|持有|揣着|别着|挂着)"
    for description in descriptions:
        if not isinstance(description, str) or item_name not in description:
            continue
        for name, character_id in character_ids.items():
            pattern = re.escape(name) + r"(?:的)?" + holder_markers + r"[^。！？]{0,20}" + re.escape(item_name)
            if re.search(pattern, description):
                return character_id
    return None


def _source_location_links(chapters: Sequence[Dict[str, Any]], location_names: set[str]) -> set[tuple[str, str]]:
    links: set[tuple[str, str]] = set()
    for chapter in chapters:
        descriptions = list(chapter.get("paragraphTexts", {}).values()) + [
            description for candidate in chapter["locations"]
            for description in candidate.get("sourceDescriptions", [candidate["description"]])
        ]
        descriptions.extend(
            description for candidate in chapter["items"]
            for description in candidate.get("sourceDescriptions", [candidate["description"]])
        )
        for description in descriptions:
            names = _nonoverlapping_entities(description, location_names)
            for index, from_name in enumerate(names):
                for to_name in names[index + 1:]:
                    if _has_direct_location_link(description, from_name, to_name):
                        links.add(tuple(sorted((from_name, to_name))))
    return links


def _has_direct_location_link(text: str, first: str, second: str) -> bool:
    connector = r"(?:通往|前往|抵达|赶到|进入|回到|走进|入口|尽头|后方|里面|门外|之间|在)"
    between = r"[\u4e00-\u9fff，。；：、（）()“”\s]{0,18}"
    return bool(
        re.search(re.escape(first) + between + connector + between + re.escape(second), text)
        or re.search(re.escape(second) + between + connector + between + re.escape(first), text)
    )


def _nonoverlapping_entities(text: str, names: set[str]) -> List[str]:
    matches = []
    occupied: List[tuple[int, int]] = []
    for name in sorted(names, key=len, reverse=True):
        start = text.find(name)
        if start < 0 or any(start < end and start + len(name) > begin for begin, end in occupied):
            continue
        occupied.append((start, start + len(name)))
        matches.append((start, name))
    return [name for _, name in sorted(matches)]


def _chapter_scene_locations(chapter: Dict[str, Any], location_names: set[str]) -> Dict[str, str | None]:
    current: str | None = None
    scenes: Dict[str, str | None] = {}
    for paragraph in chapter["paragraphs"]:
        text = chapter["paragraphTexts"][paragraph["id"]]
        character_names = {candidate["name"] for candidate in chapter["characters"]}
        detected = _scene_location_in_text(text, location_names, character_names, current)
        if detected:
            current = detected
        scenes[paragraph["id"]] = current
    return scenes


def _scene_location_in_text(text: str, location_names: set[str], character_names: set[str], current: str | None) -> str | None:
    names = _nonoverlapping_entities(text, location_names)
    for name in names:
        movement = r"(?:抵达|赶到|进入|回到|走进|前往)" + re.escape(name)
        if re.search(movement, text):
            return name
    for name in names:
        scene_action = any(character in text for character in character_names) and any(marker in text for marker in ("推开", "翻进", "刷开", "走出", "撞开"))
        if scene_action and re.search(r"(?:^|[\n。！？])" + re.escape(name) + r"(?:的|里|中|入口|门|尽头|后方)", text):
            return name
    return names[0] if current is None and names else None


def _first_seen_item_location(item: Dict[str, Any], scene_locations: Dict[str, str | None], location_ids: Dict[str, str]) -> str | None:
    for paragraph_id in item["evidenceParagraphIds"]:
        location_name = scene_locations.get(paragraph_id)
        if location_name in location_ids:
            return location_ids[location_name]
    return None


def _compile_relationships(chapters: List[Dict[str, Any]], character_ids: Dict[str, str]) -> List[Dict[str, Any]]:
    relationships = []
    seen = set()
    for chapter in chapters:
        for relation in chapter["relations"]:
            key = (relation["fromName"], relation["toName"], relation["description"])
            if key in seen:
                continue
            seen.add(key)
            relationships.append({
                "id": "relationship_" + _stable_id("|".join(key)),
                "fromCharacterId": character_ids[relation["fromName"]],
                "toCharacterId": character_ids[relation["toName"]],
                "description": relation["description"],
                "sourceChapterId": chapter["id"],
            })
    return relationships


def _compile_facts(chapters: List[Dict[str, Any]], plot_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach every compressed fact to the first source-progress where it is known."""
    facts: List[Dict[str, Any]] = []
    seen = set()
    for chapter in chapters:
        paragraphs = {paragraph["id"]: paragraph for paragraph in chapter.get("paragraphs", [])}
        for fact in chapter["facts"]:
            if fact["text"] not in seen:
                seen.add(fact["text"])
                evidence = [paragraphs[item] for item in fact.get("evidenceParagraphIds", []) if item in paragraphs]
                if not evidence:
                    continue
                fact_end = max(item["lineRange"]["end"] for item in evidence)
                first_known = next(
                    (index for index, node in enumerate(plot_nodes, start=1) if node["lineRange"]["end"] >= fact_end),
                    len(plot_nodes),
                )
                facts.append({
                    "id": "fact_" + _stable_id(fact["text"]), "text": fact["text"],
                    "sourceChapterId": chapter["id"],
                    "sourceProgress": _progress_value(first_known),
                    "lineRange": {
                        "start": min(item["lineRange"]["start"] for item in evidence),
                        "end": fact_end,
                    },
                })
                if isinstance(fact.get("communication"), dict):
                    facts[-1]["communication"] = copy.deepcopy(fact["communication"])
    return facts or [{"id": "fact_source_continuity", "text": "后续叙事必须保持已确认母本事实的连续性。"}]


def _compile_narrative_assertions(facts: Sequence[Dict[str, Any]], plot_node_count: int) -> List[Dict[str, Any]]:
    """Compile only mechanically evident source constraints into local guards."""
    assertions: List[Dict[str, Any]] = []
    communication_times_by_progress: Dict[str, List[str]] = {}
    communication_contents_by_progress: Dict[str, Dict[str, List[str]]] = {}
    for fact in facts:
        text = fact.get("text")
        progress = fact.get("sourceProgress")
        if not isinstance(text, str) or not isinstance(progress, str):
            continue
        time_match = re.search(r"(?:语音|短信|消息)(?:的)?发送时间(?:是|为)?([零一二三四五六七八九十两]+点[零一二三四五六七八九十两]+分|(?:[01]?\d|2[0-3])[:：][0-5]\d)", text)
        if time_match is not None:
            communication_times_by_progress.setdefault(progress, []).append(time_match.group(1))
        communication = fact.get("communication")
        if isinstance(communication, dict) and isinstance(communication.get("speakerName"), str):
            communication_contents_by_progress.setdefault(progress, {}).setdefault(
                communication["speakerName"], [],
            ).append(text)
    for fact in facts:
        text = fact.get("text")
        progress = fact.get("sourceProgress")
        if not isinstance(text, str) or not isinstance(progress, str):
            continue
        start = next(
            (index for index in range(1, plot_node_count + 1) if _progress_value(index) == progress),
            None,
        )
        if start is None:
            continue
        if any(term in text for term in VEHICLE_TERMS) and any(marker in text for marker in PENDING_VEHICLE_MARKERS):
            assertions.append({
                "id": "assertion_" + _stable_id(fact["id"] + "_vehicle_pending"),
                "when": {
                    "storyScope": {"equals": "source"},
                    "sourceProgress": {"oneOf": [_progress_value(index) for index in range(start, plot_node_count + 1)]},
                },
                "instruction": "已确认事实：" + text.rstrip("。！？") + "。在没有状态变更前，不能写其已经离开、驶离、开走、发车或已离站。",
                "message": "剧情正文与已确认的交通状态矛盾。",
                "forbiddenPatterns": [
                    r"(?:列车|火车|班车|客车|货车|船只|轮船)(?:(?!(?:不|未|没|会|将|能|准备|等待))[^。！？\n]){0,20}(?:离开|驶离|开走|(?:已经|已|正在|开始|随即|终于)发车|已离站|已经离站|刚过|刚刚离站)",
                ],
            })
        communication = re.search(
            r"([\u4e00-\u9fff]{2,3})的(?:电话|手机|通讯|联络)[^。！？]{0,12}(?:无法接通|无法联系|失去联系)",
            text,
        )
        if communication is None:
            continue
        name = communication.group(1)
        known_message_times = list(dict.fromkeys(communication_times_by_progress.get(progress, [])))
        known_message_contents = list(dict.fromkeys(
            communication_contents_by_progress.get(progress, {}).get(name, []),
        ))
        assertions.append({
            "id": "assertion_" + _stable_id(fact["id"] + "_communication_unreachable"),
            "when": {
                "storyScope": {"equals": "source"},
                "sourceProgress": {"oneOf": [_progress_value(index) for index in range(start, plot_node_count + 1)]},
            },
            "instruction": (
                "已确认事实：" + text.rstrip("。！？") + "。"
                f"未经状态变更，不得补写{name}的新消息、来电、到站、抵达或位置。"
                + (
                    "已登记历史通信原句：" + "；".join(known_message_contents)
                    + "若引用，只能使用完整原句或原句中的连续片段，不得插入、追加或替换消息内容。"
                    if known_message_contents else ""
                )
            ),
            "message": "剧情正文与已确认的通信状态矛盾。",
            "communicationTimestamp": {
                "characterName": name,
                "knownMessageTimes": known_message_times,
                "knownMessageTexts": known_message_contents,
                "knownMessageAnchors": [
                    part.strip() for content in known_message_contents
                    for part in re.split(r"[。！？]", content)
                    if len(part.strip()) >= 4
                ],
            },
            "forbiddenPatterns": [
                re.escape(name) + r"[^。！？\n]{0,40}(?:发来|发出|发送|传来)[^。！？\n]{0,8}(?:新的|另一条|第二条)(?:消息|语音|短信)",
                re.escape(name) + r"[^。！？\n]{0,20}(?:再次|又)(?:给[^。！？\n]{0,8})?(?:发来|发出|发送|打来)[^。！？\n]{0,12}(?:消息|语音|短信|电话)",
            ],
        })
    return assertions


def _chapter_location(chapter: Dict[str, Any], location_ids: Dict[str, str], fallback: str) -> str:
    for event in chapter["events"]:
        for name in event.get("locationNames", []):
            if name in location_ids:
                return location_ids[name]
    for location in chapter["locations"]:
        if location["name"] in location_ids:
            return location_ids[location["name"]]
    return fallback


def _plot_nodes(chapters: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    nodes = []
    for chapter_index, chapter in enumerate(chapters):
        paragraphs = {paragraph["id"]: paragraph for paragraph in chapter.get("paragraphs", [])}
        for event_index, event in enumerate(chapter["events"], start=1):
            evidence = [paragraphs[paragraph_id] for paragraph_id in event["evidenceParagraphIds"]]
            start = min(paragraph["characterRange"]["start"] for paragraph in evidence)
            end = max(paragraph["characterRange"]["end"] for paragraph in evidence)
            nodes.append({
                "id": f"{chapter['id']}-event-{event_index:03d}", "chapterId": chapter["id"], "chapterTitle": chapter["title"],
                "event": event, "characters": chapter["characters"], "locations": chapter["locations"],
                "sourceExcerpt": text[start:end], "lineRange": {"start": min(paragraph["lineRange"]["start"] for paragraph in evidence), "end": max(paragraph["lineRange"]["end"] for paragraph in evidence)},
                "narrativeBrief": _source_scene_brief(chapter, end, text, chapters[chapter_index - 1] if chapter_index else None),
            })
    return nodes


def _source_scene_brief(chapter: Dict[str, Any], end: int, text: str, previous_chapter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Compile bounded, cited scene cues up to this beat, never future prose.

    These are source observations/statements, not inferred runtime outcomes.
    Keeping short opening and recent cues gives a writer concrete material
    without sending a reader chapter or a growing history.
    """
    paragraphs = (previous_chapter or {}).get("paragraphs", []) + chapter.get("paragraphs", [])
    eligible = [p for p in paragraphs if p["characterRange"]["end"] <= end]
    communication_cues = []
    for index, paragraph in enumerate(eligible):
        span = paragraph["characterRange"]
        value = text[span["start"]:span["end"]]
        if re.search(r"(?:录音|语音|短信)[^。！？]{0,35}(?:秒|分钟|风声|脚步)", value):
            communication_cues.extend(eligible[index:index + 3])
    candidates = eligible[:3] + communication_cues + eligible[-12:] + eligible[3:-12]
    cues: List[Dict[str, Any]] = []
    seen: set[str] = set()
    remaining = 1400
    for paragraph in candidates:
        if paragraph["id"] in seen:
            continue
        seen.add(paragraph["id"])
        # Keep complete sentences/quotes, rather than cutting mid-dialogue.
        span = paragraph["characterRange"]
        parts = re.split(r"(?<=[。！？])", text[span["start"]:span["end"]].strip())
        value = ""
        for part in parts:
            if len(value + part) > 180:
                break
            value += part
            if value.count("“") == value.count("”") and len(value) >= 60:
                break
        if not value or value.count("“") != value.count("”") or len(value) > remaining:
            continue
        cues.append({"text": value, "evidenceParagraphId": paragraph["id"], "lineRange": copy.deepcopy(paragraph["lineRange"])})
        remaining -= len(value)
    return sorted(cues, key=lambda cue: cue["lineRange"]["start"])


def _compile_beats(plot_nodes: List[Dict[str, Any]], location_ids: Dict[str, str], fallback_location_id: str) -> List[Dict[str, Any]]:
    beats: List[Dict[str, Any]] = []
    current_location = fallback_location_id
    event_locations = []
    for plot_node in plot_nodes:
        current_location = _event_location(plot_node, location_ids, current_location)
        event_locations.append(current_location)
    for index, plot_node in enumerate(plot_nodes, start=1):
        event = plot_node["event"]
        location_id = event_locations[index - 1]
        next_directions = []
        if index < len(plot_nodes):
            next_node = plot_nodes[index]
            next_event = next_node["event"]
            next_directions.append({
                "id": f"direction_chapter_{index:03d}_to_{index + 1:03d}", "title": next_event["title"], "summary": next_event["summary"],
                "suggestedInput": "沿母本推进到下一关键剧情节点", "canonicalBeatId": f"beat_chapter_{index + 1:03d}",
                "statePatch": {"sourceProgress": _progress_value(index + 1), "playerLocationId": event_locations[index]},
            })
        beats.append({
            "id": f"beat_chapter_{index:03d}", "nodeId": "node_source_continuity", "summary": event["summary"],
            "sourceExcerpt": {"text": plot_node["sourceExcerpt"], "lineRange": copy.deepcopy(plot_node["lineRange"])},
            "narrativeAnchor": event["summary"], "openThreads": event.get("openThreads", []) or ([next_directions[0]["title"]] if next_directions else []),
            "nextDirections": next_directions,
            "branchState": {"playerLocationId": location_id, "sourceProgress": _progress_value(index), "freeTextProgress": 0},
        })
    return beats


def _event_location(plot_node: Dict[str, Any], location_ids: Dict[str, str], fallback: str) -> str:
    # Scene-setting in the event's evidence outranks incidental references
    # such as "整座车站都听见了". An event with no new setting stays put.
    excerpt = plot_node["sourceExcerpt"].lstrip()
    for name in sorted(location_ids, key=len, reverse=True):
        if excerpt.startswith(name):
            return location_ids[name]
    for name in plot_node["event"].get("locationNames", []):
        if name in location_ids and re.search(r"(?:走进|进入|抵达|回到|返回|站在)[^。！？]{0,4}" + re.escape(name), excerpt):
            return location_ids[name]
    return fallback


def _compile_arc_model(plot_nodes: List[Dict[str, Any]], directions: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not directions:
        return None
    grouped: List[List[tuple[int, Dict[str, Any]]]] = []
    for index, (plot_node, direction) in enumerate(zip(plot_nodes, directions), start=1):
        if not grouped or grouped[-1][0][1]["chapterId"] != plot_node["chapterId"]:
            grouped.append([])
        grouped[-1].append((index, {**plot_node, "direction": direction}))
    arcs = []
    for group_index, group in enumerate(grouped, start=1):
        first_index, first = group[0]
        last_index, last = group[-1]
        arcs.append({
            "id": f"arc_source_chapter_{group_index:03d}", "title": "推进：" + first["chapterTitle"], "summary": first["event"]["summary"],
            "availableWhen": {"sourceProgress": {"oneOf": [_progress_value(index) for index, _ in group]}},
            "completionWhen": {"sourceProgress": {"equals": _progress_value(last_index + 1)}},
            "phaseDirectionIds": [item["direction"]["id"] for _, item in group], "nextArcIds": [f"arc_source_chapter_{group_index + 1:03d}"] if group_index < len(grouped) else [],
        })
    return {"arcs": arcs, "entryArcIds": [arc["id"] for arc in arcs]}


def _compile_entry_model(plot_nodes: List[Dict[str, Any]], beats: List[Dict[str, Any]], important: List[Dict[str, Any]], timeline: List[Dict[str, Any]]) -> Dict[str, Any]:
    source_character_ids = [item["id"] for item in important]
    canonical_character_id = source_character_ids[0]
    entries = []
    for index, (plot_node, beat, timeline_entry) in enumerate(zip(plot_nodes, beats, timeline)):
        event_names = set(plot_node["event"]["characterNames"])
        related = source_character_ids if index == 0 else (
            [item["id"] for item in important if item["name"] in event_names] or source_character_ids[:1]
        )
        narratives = {
            character_id: f"以该原著角色的身份，从《{plot_node['chapterTitle']}》的“{plot_node['event']['title']}”之后进入共创；已确认母本事实保持不变。"
            for character_id in related if character_id != canonical_character_id
        }
        entries.append({
            "id": "entry_" + plot_node["id"], "title": plot_node["event"]["title"], "summary": plot_node["event"]["summary"], "chapterTitle": plot_node["chapterTitle"],
            "sourceChapterId": plot_node["chapterId"],
            "nodeId": beat["nodeId"], "beatId": beat["id"], "sourceCharacterIds": related, "timelineRefs": [timeline_entry["id"]],
            "availableToNewCharacter": True, "newCharacterNarrative": f"新角色从《{plot_node['chapterTitle']}》的“{plot_node['event']['title']}”之后进入；不得改写此前已确认的母本事实。",
            "sourceCharacterNarratives": narratives,
        })
    return {"sourceCharacterIds": source_character_ids, "entryPoints": entries, "defaultEntryPointId": entries[0]["id"], "newCharacter": {"enabled": True, "profileFields": copy.deepcopy(DEFAULT_NEW_CHARACTER_PROFILE_FIELDS)}}


def _stable_id(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _progress_value(index: int) -> str:
    return f"chapter_{index:03d}"


def _source_range_text(text: str, character_range: Dict[str, int]) -> str:
    return text[character_range["start"]:character_range["end"]]
