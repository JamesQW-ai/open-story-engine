"""Session-only identity openings; fixed source packages remain untouched."""

import copy

from .cocreation import entry_point_by_id, find_beat, script_generated_package


RAINY_SOURCE = "918fd8656c43a65b0bab33a4099a4a556b29a847db7f97f6d54b0a15f7f924a8"
RAINY_OPENINGS = {
    "许川": {
        "location": "候车厅", "title": "十九秒之后",
        "paragraphs": [
            "推开临潮站的玻璃门时，雨水顺着衣袖滴到了手机上。屏幕停在唐栖发来的那条语音，十九秒，你已经听了三遍。",
            "“别让陈砚拿到储物柜里的录音。隧道里还有人。”她的声音被风声切碎，最后是一声金属门的巨响。再拨过去，电话始终没有接通。",
            "候车厅的电子钟亮着二十三点十分。末班列车停在雨里，车门紧闭。你抬头，看见一个穿深蓝色站务制服的男人正在安抚乘客。唐栖在语音里提到的名字，就是他。",
            "男人注意到了你手中的手机。“找人？”他问。靠墙的长椅旁，一个穿旧雨衣的维修工也抬起了头。你还没有开口，候车厅里便有两道目光落在了你身上。",
        ],
        "actions": [
            {"title": "向陈砚打听唐栖，暂不提录音", "summary": "只询问她最后出现的时间和去向，留意他是否回避问题。"},
            {"title": "走近长椅旁的维修工", "summary": "先问他是否见过唐栖，试着听到站务主管之外的说法。"},
        ],
    },
    "唐栖": {
        "location": "信号室", "title": "门的另一侧",
        "paragraphs": [
            "信号室的铁门隔断了外面的声响，只剩雨水敲击墙面的闷声。你伸手碰了碰冰冷的门板，停下来听，却听不到回应。",
            "发给许川的那条语音，是眼下唯一的指望。你已经把储物柜和隧道的事告诉他，但他有没有听到、会不会赶来，你无从知道。手机里的信号再也没有恢复。",
            "你想起下午翻过的旧档案，那些对不上的验收日期，还有姜序没能回答完的话。追问把你带到了这里，而现在，证据留在外面，你被留在了门的这一侧。",
            "昏暗的设备指示灯映着潮湿的墙。你压低呼吸，辨认门缝附近的声音。是雨，是风，还是有人经过？你得决定，先让外面的人听见你，还是先弄清这间屋子里还有什么。",
        ],
        "actions": [
            {"title": "敲击铁门，留出间隔听回应", "summary": "用短促而有规律的敲击尝试求助，停下来辨认门外是否有人。"},
            {"title": "查看门边和设备旁的环境", "summary": "借指示灯观察屋内可见的设施与门缝，先寻找能够利用的信息。"},
        ],
    },
    "陈砚": {
        "location": "站台", "title": "放行之前",
        "paragraphs": [
            "站台的雨被风推到檐下，乘客又围了上来。有人问什么时候发车，有人举着没有信号的手机，你重复了一遍：“请耐心等候。”",
            "末班列车仍停在原处。你看向候车厅的电子钟，零点正在逼近。线路不能一直占着，可每回答一次“很快”，周围的质疑声就更响一些。",
            "唐栖下午追问过维修档案，姜序也在场。那些话尚未平息，候车厅的玻璃门又被推开了。一个浑身带雨的年轻人站在门边，低头听着手机里的声音。",
            "你认出了他抬眼时寻找人的神情。长椅旁的姜序也在看他。你离开乘客几步，又停住了：先稳住眼前的站台，还是先弄清这个年轻人的来意？",
        ],
        "actions": [
            {"title": "先回应乘客对发车时间的追问", "summary": "听清他们最担心的事，说明目前仍在等候放行，观察人群反应。"},
            {"title": "到候车厅询问年轻人的来意", "summary": "向门边的人问一句是否在找人，先听他说什么，不急着表态。"},
        ],
    },
    "姜序": {
        "location": "候车厅", "title": "未说完的回答",
        "paragraphs": [
            "旧雨衣的下摆贴着靴边，潮气一点点渗进长椅旁的阴影。你把手留在雨衣口袋边，指腹擦着湿透的衣料，始终没有坐下。",
            "下午，唐栖在站务室门口问你，隧道的排水泵为什么三年没换。你没来得及回答完。现在雨势比那时更大，候车厅里的人还只惦记着末班车什么时候能走。",
            "陈砚在站台那边安抚乘客。一个刚进门的年轻人反复听着手机，提起唐栖的名字时，你不由得抬了头。他正在找她，而陈砚已经看了过来。",
            "你知道有些问题不能一直搁着。可如果现在开口，主管一定会听见。雨声压在玻璃上，你把那句尚未说完的回答在心里重过一遍，等待一个可以说话的间隙。",
        ],
        "actions": [
            {"title": "趁陈砚走开，询问年轻人收到的消息", "summary": "压低声音问他与唐栖是否联系过，先弄清她留下了什么线索。"},
            {"title": "向陈砚重提排水泵的隐患", "summary": "以维修工的身份询问今晚如何处理故障，听清他对风险的回应。"},
        ],
    },
}


# These are role-known opening facts, pinned to the source hash above.
RAINY_KNOWN_CLUES = {
    '许川': ['唐栖留下十九秒语音，提到储物柜里的录音和隧道里的人。', '唐栖的电话目前无法接通。'],
    '唐栖': ['下午翻看的旧档案中，有几处验收日期对不上。', '你已发出求助语音，但不知道许川是否收到。'],
    '陈砚': ['末班列车仍在等候放行。', '唐栖下午追问过维修档案，姜序也在场。'],
    '姜序': ['唐栖追问过隧道排水泵的问题，那次谈话还没说完。'],
}


def identity_opening_package(package, selection):
    """Overlay one validated entry for session creation, never rewrite modules.

    Authored openings are pinned to exact source bytes. Other imported books
    use their own identity description and entry excerpt, not this story's cast.
    """
    if not script_generated_package(package):
        return package
    entry = copy.deepcopy(entry_point_by_id(package, selection["entryPointId"]))
    character_id = selection.get("sourceCharacterId")
    approved_ids = set(entry.get("sourceCharacterIds", []))
    if (package["story"]["entryModel"].get("policy") == "official_unknown_reader/1"
            and character_id in approved_ids):
        return package
    beat = find_beat(package, entry["beatId"])
    character = next((c for c in package["characters"] if c["id"] == character_id), None)
    name = character["name"] if character else selection["name"]
    profile = None
    if (package.get("sourceAnalysis", {}).get("sha256") == RAINY_SOURCE
            and entry["id"] == package["story"]["entryModel"]["defaultEntryPointId"]):
        profile = RAINY_OPENINGS.get(name) if character else None
    if profile:
        location = next((p for p in package["locations"] if p["name"] == profile["location"]), None)
        if location is None:
            raise ValueError("身份开场的地点不在故事包内")
        entry["sourceCharacterLocationIds"] = {character_id: location["id"]}
        entry["chapterTitle"] = profile["title"]
        entry["openingActions"] = copy.deepcopy(profile["actions"])
        entry["openingClues"] = list(RAINY_KNOWN_CLUES[name])
        entry["openingSummary"] = profile["paragraphs"][0]
        entry["openingThreads"] = [action["title"] for action in profile["actions"]]
        narrative = "\n\n".join(profile["paragraphs"])
    else:
        description = (character or {}).get("menuDescription") or (character or {}).get("description", "")
        excerpt = beat.get("sourceExcerpt", {}).get("text") or entry["summary"]
        narrative = "\n\n".join(p for p in ["你停下脚步，留意眼前的动静。", excerpt,
                                             "眼前的故事还在继续。你会如何面对接下来的事？"] if p)
        entry["openingSummary"] = f"{name}：{entry['summary']}"
    if character:
        if character_id not in approved_ids:
            # The selected persona is allowed to enter any declared opening
            # in the web player. Bind the session-only view to that persona
            # without changing the frozen StoryPackage on disk.
            entry["sourceCharacterIds"] = [character_id]
            entry["sourceCharacterLocationIds"] = {
                character_id: entry.get("openingState", {}).get("playerLocationId")
            }
            entry["sourceCharacterNarratives"] = {}
        entry["sourceCharacterNarratives"] = {character_id: narrative}
    else:
        entry["newCharacterNarrative"] = narrative
    # Only start consumes this view. Future requests load the original package
    # and the persisted root (including its persona, location and prose).
    model = {**package["story"]["entryModel"], "entryPoints": [entry]}
    return {**package, "story": {**package["story"], "entryModel": model}}
