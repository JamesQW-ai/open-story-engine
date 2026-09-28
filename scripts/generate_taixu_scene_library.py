#!/usr/bin/env python3
"""Generate and register the Taixu opening and chapter scene library.

The script reads STORY_IMAGE_* from the repository .env, calls the configured
OpenAI-compatible image endpoint, and writes prompts plus image bytes into the
versioned illustration directory. Existing approved files are never replaced.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ID = "taixu-relics-part1"
PACKAGE_VERSION = "0.1.3"
LIBRARY = ROOT / "content" / "illustrations" / PACKAGE_ID / PACKAGE_VERSION
PACKAGE = ROOT / "content" / "packages" / PACKAGE_ID / PACKAGE_VERSION
CHAPTER_DOCS = ROOT / "docs" / "太虚遗录-第一部"

STYLE = (
    "古典修仙小说《太虚遗录》第一部的单幅横向剧情插图，东方幻想写实数字绘画，"
    "电影感构图，青灰、墨蓝与克制金色光线，细腻纸张和胶片质感，1536x1024。"
    "只画下面正文锚定的一个瞬间，不拼接多个时刻，不添加未来事件；不出现可读文字、"
    "水印、现代物件、科幻机械或海报排版。"
)


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("STORY_IMAGE_BASE_URL", "STORY_IMAGE_API_KEY", "STORY_IMAGE_MODEL", "STORY_IMAGE_SIZE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def image_bytes(raw: bytes) -> bytes:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return raw
    if raw.startswith(b"\xff\xd8\xff"):
        return raw
    raise ValueError("image provider returned unsupported bytes")


def generate(prompt: str, env: dict[str, str]) -> bytes:
    payload = {
        "model": env["STORY_IMAGE_MODEL"],
        "prompt": prompt,
        "n": 1,
        "size": env.get("STORY_IMAGE_SIZE", "1536x1024"),
    }
    request = Request(
        env["STORY_IMAGE_BASE_URL"].rstrip("/") + "/images/generations",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + env["STORY_IMAGE_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=120) as response:
        result = json.loads(response.read())
    item = result["data"][0]
    if not item.get("b64_json"):
        raise ValueError("image provider did not return b64_json")
    return image_bytes(base64.b64decode(item["b64_json"], validate=True))


def paragraphs(path: Path) -> list[str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def beat_cards(chapter: int) -> tuple[dict, dict]:
    index = PACKAGE / "modules" / "indexes" / "beats" / f"chapter-{chapter:03d}.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    last = data["beats"][-1]
    beat_path = PACKAGE / "modules" / last["path"]
    beat = json.loads(beat_path.read_text(encoding="utf-8"))["beat"]
    return last, beat


def opening_specs() -> list[dict]:
    return [
        {
            "id": "opening-shen-register-v1",
            "file": "opening-shen-register-v1.png",
            "scene_id": "entry_shen_register",
            "location_id": "location_open_registry",
            "beat": "beat_chapter_005",
            "title": "沈砚秋·名册上的空白",
            "prompt": STYLE + "玄霄宗登记堂清晨，沈砚秋坐在青石长桌后，青袍木簪，手边摊开登记簿，最后一页有三行刚被裁掉的纸口；三盏长明灯中一盏火焰很低，门外是雨后山雾。人物克制而警觉，画面不出现其他明确人物。",
            "source_evidence": ["你坐在登记堂的长桌后，登记簿摊开在手边。", "三行新裁的纸口藏在最后一页，边缘没有积灰。"],
            "alt": "沈砚秋在登记堂面对被裁掉三行的名册",
        },
        {
            "id": "opening-lu-chenzhou-inscription-v1",
            "file": "opening-lu-chenzhou-inscription-v1.png",
            "scene_id": "entry_lu_ledger",
            "location_id": "location_open_inscription",
            "beat": "beat_chapter_261",
            "title": "陆沉舟·石碑下的无名令",
            "prompt": STYLE + "北境荒原的石碑旁，失踪多年的中年引灯人陆沉舟穿旧斗篷，半侧脸被幽蓝纹路照亮，手指压住一页没有落款的诏书，纸角在冷风中掀动；石碑、雪尘和远处微弱灯火构成悬疑场景。不要把诏书画成可读文字。",
            "source_evidence": ["你站在石碑旁，手指压住刚被抄下的无名诏书。", "三年前押送引路灯后，你的名字从宗门记录里消失过。"],
            "alt": "陆沉舟在石碑旁压住无名诏书",
        },
        {
            "id": "opening-xiao-edict-v1",
            "file": "opening-xiao-edict-v1.png",
            "scene_id": "entry_xiao_edict",
            "location_id": "location_open_edict_square",
            "beat": "beat_chapter_258",
            "title": "萧问蝉·没有签名的诏书",
            "prompt": STYLE + "天门废墟广场，萧问蝉身穿黑底银纹巡界使衣甲，站在风沙与残垣中央，手持一卷没有签名的金色诏书，身后三百名修士只作为压低的整齐剪影；几枚记忆水珠悬在空中发冷光。不要出现可读文字或夸张战斗。",
            "source_evidence": ["你站在天门废墟的广场中央，三百名黑底银纹修士已经列在身后。", "手里的金色诏书没有签名。"],
            "alt": "萧问蝉在天门废墟宣读无名诏书",
        },
        {
            "id": "opening-ye-qingming-ancestral-v1",
            "file": "opening-ye-qingming-ancestral-v1.png",
            "scene_id": "entry_ye_qingming_hall",
            "location_id": "location_open_ancestral_hall",
            "beat": "beat_chapter_335",
            "title": "叶青冥·灯火之外",
            "prompt": STYLE + "玄霄宗祖师殿前的高石阶，苍老而沉静的掌门叶青冥穿深色掌门长袍，手边放着断剑，殿门半掩，长老与山门外的人群只作远处模糊轮廓；沈砚秋捧着无字感的古旧书册走近，氛围是承认旧错前的沉默。不要出现可读文字。",
            "source_evidence": ["你坐在祖师殿前最高的石阶上，手边放着断剑。", "沈砚秋捧着《太虚遗录》走进来，承认自己曾删去旧记录。"],
            "alt": "叶青冥在祖师殿前等待面对旧记录",
        },
    ]


def chapter_specs() -> list[dict]:
    specs = []
    reused = {
        1: ("gate-detail-v1.png", "entry_lu_gate"),
        3: ("trial-detail-v1.png", "entry_gu_trial"),
        7: ("gallery-detail-v1.png", "entry_ye_gallery"),
    }
    for chapter in range(1, 51):
        path = next(CHAPTER_DOCS.glob(f"太虚遗录-第{chapter:02d}章-*.md"))
        title = path.stem.split("-", 2)[-1]
        lines = paragraphs(path)
        # Start and ending evidence anchor the location and the chapter's closing image.
        evidence = "；".join(lines[:2] + lines[-2:])
        evidence = evidence[:520]
        last, beat = beat_cards(chapter)
        anchor = beat.get("narrativeAnchor") or beat.get("summary") or title
        location = beat.get("branchState", {}).get("playerLocationId")
        specs.append({
            "id": f"chapter-{chapter:03d}-highlight-v1",
            "file": reused.get(chapter, (f"chapter-{chapter:03d}-highlight-v1.png", ""))[0],
            "reused_scene_id": reused.get(chapter, ("", ""))[1],
            "scene_id": f"highlight_chapter_{chapter:03d}",
            "location_id": location,
            "beat": last["id"],
            "title": f"第{chapter:02d}章·{title}",
            "prompt": STYLE + f"《太虚遗录》第一部第{chapter:02d}章《{title}》的代表性瞬间。正文锚点：{anchor}。场景证据：{evidence}。以正文中明确出现的地点、人物和道具为准，突出一个静止而有叙事张力的画面。",
            "source_evidence": [anchor, evidence[:180]],
            "alt": f"《太虚遗录》第一部第{chapter:02d}章{title}的关键场面",
        })
    return specs


def make_manifest(specs: list[dict], existing: dict) -> dict:
    assets = list(existing.get("assets", []))
    known = {asset.get("id") for asset in assets}
    for spec in specs:
        path = LIBRARY / spec["file"]
        if not path.exists():
            continue
        card = {
            "id": spec["id"], "file": spec["file"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "review_status": "approved", "kind": "story_highlight",
            "priority": 8, "scene_id": spec["scene_id"], "source_beats": [spec["beat"]],
            "beats": [spec["beat"]], "location_id": spec["location_id"],
            "perspective": "cinematic_landscape", "perspective_ids": [],
            "appearance_version": "taixu-characters-v1", "characters": {},
            "required_state": {}, "source_evidence": spec["source_evidence"],
            "alt": spec["alt"], "required_text": [], "forbidden_text": [],
            "review_note": "2026-09-20：由当前冻结母本章节锚点生成，需人工逐图复核后再用于正式剧情展示。",
            "visual_prohibitions": ["不画可读文字或水印", "不添加正文未出现的人物、道具或通道"],
        }
        if spec["id"] not in known:
            assets.append(card)
            known.add(spec["id"])
    return {**existing, "assets": assets,
            "coverage": "七位官方角色开场与五十章逐章关键场面；所有新增图均绑定章节 beat 与地点，需人工逐图复核。"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0, help="仅生成前 N 张缺失图，0 表示全部")
    args = parser.parse_args()
    env = load_env()
    required = ("STORY_IMAGE_BASE_URL", "STORY_IMAGE_API_KEY", "STORY_IMAGE_MODEL")
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise SystemExit("missing image configuration: " + ", ".join(missing))
    LIBRARY.mkdir(parents=True, exist_ok=True)
    specs = opening_specs() + chapter_specs()
    prompts_path = LIBRARY / "prompts-v2.json"
    prompts_path.write_text(json.dumps({"model": env["STORY_IMAGE_MODEL"], "size": env.get("STORY_IMAGE_SIZE", "1536x1024"), "items": specs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    existing = json.loads((LIBRARY / "manifest.json").read_text(encoding="utf-8"))
    missing_specs = [spec for spec in specs if not (LIBRARY / spec["file"]).exists()]
    if args.limit:
        missing_specs = missing_specs[:args.limit]
    print(f"configured model={env['STORY_IMAGE_MODEL']} missing={len(missing_specs)} workers={args.workers}", flush=True)

    def one(spec: dict) -> tuple[str, int]:
        target = LIBRARY / spec["file"]
        last_error = None
        for attempt in range(3):
            try:
                data = generate(spec["prompt"], env)
                target.write_bytes(data)
                return spec["id"], len(data)
            except Exception as error:  # network/provider failures are retried per asset
                last_error = error
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"{spec['id']}: {last_error}")

    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(one, spec): spec for spec in missing_specs}
        for future in as_completed(futures):
            spec = futures[future]
            try:
                asset_id, size = future.result()
                print(f"generated {asset_id} bytes={size}", flush=True)
            except Exception as error:
                failures.append(str(error))
                print(f"failed {spec['id']}: {error}", flush=True)
    if failures:
        raise SystemExit("generation failures: " + " | ".join(failures))
    manifest = make_manifest(specs, existing)
    (LIBRARY / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"registered assets={len(manifest['assets'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
