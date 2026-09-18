"""Source-pinned, code-owned scene outcomes for the playable rainy-station route.

These are session adaptations of source chapters 1–4, not edits to the novel.
Progress records completed events, never token counts or an LLM-assigned score.
"""
import hashlib
import re

from .api_openings import RAINY_SOURCE

PREFIX = 'event_player_rainy_v2_'
LABELS = ['弄清阻碍', '走出险境', '核对证据', '完成交代']
STEPS = len(LABELS)

# Each scene supplies enough new action to sustain a chapter. Only this scene's
# brief is exposed to the narrator; later scenes are not prompt context.
SCENES = {'唐栖': [('维修隧道',
         '维修隧道',
         '走出锁死的门',
         '先承接你在信号室的观察或敲门，不重演已经检查过的环境。许川循求助语音来到门外，喊你的名字，你辨认声音后说明控制箱掉电、手动栓在门外。你抱着调查文件袋，手机仍无信号。许川在门外确认金属卡住滑栓，告诉你已取到十七号柜录音，暂不复述录音内容。你主动说明门内用力位置，与他约定听口令配合。他找到原著已有的维修扳手，卡进滑栓。第一次试推受阻，随后调整角度；双方配合终于将门打开。开门前只能听见门外，不能提前看见他。你跨出信号室，终于当面见到许川这位原本的朋友，也看见跟来的陈砚，后者索要录音，许川要求先救人。姜序在旁边岔口开阀后走来会合，水流减弱；你抱好文件袋，确认自己能走，许川与姜序准备搀扶。结尾四人仍在维修隧道，尚未返回候车厅，尚未播放录音或铺开证据。你已经脱困，不再重演被锁住。'),
        ('候车厅',
         '候车厅',
         '把材料带出去',
         '铁门上章已经打开，你已经脱困，绝不能重演开门或再次困住。按玩家选择先确认行走能力和照明，或先护好文件袋并请人搀扶。许川与姜序协助你沿来路从维修隧道经站务室返回候车厅，陈砚跟在后面，没有逃走。通过不同路段的水位、落脚、狭窄转身和同伴分工写清撤离过程，不新增事故。你只能看到当前经过的地方。进入候车厅后乘客让出长椅，提供干毛巾和热水。你坐下缓过气，逐渐能连贯说话。许川把录音笔交还你，你从自己的文件袋抽出原始采购单、验收单和门禁记录，确认字迹仍可辨认，再收好，尚不逐项核对或播放录音。陈砚要求解释这些材料，许川要求等你先缓过来，姜序说明列车尚未放行。结尾列车仍停着，人和材料已安全抵达候车厅，下一步由你选择从哪一份材料开始说明。'),
        ('候车厅',
         '候车厅',
         '互相印证的证据',
         '接着长椅上的材料，按玩家选择先播放录音或先比对纸面记录。你让姜序当面核对验收单签名，他明确说不是自己的签名。录音中的陈砚承认工程未完成却继续按时运行，这是旧录音，不是新来电。将采购单、签名、门禁记录与录音相互对照，区分已证实的异常和仍待调查的责任。陈砚当场质疑材料解释，众人要求保留原件。不要虚构拘捕、定罪、工程已修复或外部回电。'),
        ('候车厅',
         '候车厅',
         '调查有了回应',
         '承接已经核对的证据，不重新发现它们。司机当面确认没有完成放行复诵，列车继续停站，并明确按事故程序上报积水、受困和记录异常。姜序在日志写明故障未排除及人员救出，承诺作证并留下真实记录。你明确获得了安全脱困、材料保全、调查有人正式接手的回应；陈砚留在现场面对质询。按玩家所选安排证据保管或核对日志，回扣最初那条求助语音。以雨中仍亮着的候车厅收尾，承认后续调查尚待进行，不另起危机，不留下下一步选项。')],
 '许川': [('站务室',
         '站务室',
         '十七号柜里的声音',
         '先执行玩家的询问。陈砚回避唐栖去向，只强调零点放行；长椅旁的维修工说出自己叫姜序，坦言唐栖问过排水泵和信号室门锁。你根据唐栖旧语音检查旧时刻表，找到十七号铜牌。姜序说明西侧消防通道锁坏、站务室有编号柜，提醒别碰终端右侧那根线。将回避与实用指路形成对照。循已经获得的线索，从候车厅经消防通道到站务室；若自行进入，使用原著未关严的小窗；若请求姜序引路，写出他带路的过程。用已找到的铜牌打开十七号柜，取出防水录音笔、维修图纸和撕下的调度日志。旧录音是唐栖追问伪造姜序签名和挪用排水泵款，陈砚承认工程未完成。终端同时显示排水泵故障、检修却被标记完成，你拍照留证。陈砚来到门边索要录音，你保留材料，不离开去救人。'),
        ('维修隧道',
         '维修隧道',
         '门开之后',
         '姜序带应急灯来到站务室，表示愿意带路；你按所选先确认排水路径或要求他讲清风险。两人从站务室后方进入维修隧道，姜序用自己的通行证开门。图纸标明入口左侧手动阀，右侧是旧电缆槽，先开阀降低水压。姜序留在附近岔口开阀，你到信号室门外，呼喊唐栖后听见她的回答：门锁死，手动栓在外面。陈砚跟到隧道。随后在信号室门外继续。按所选先与唐栖约定推门时机，或请姜序协助检查卡栓；陈砚在旁索要录音，救人不再以交换证据为前提。使用原著已有、积水中的维修扳手，调整撬动角度，与门内的唐栖配合，终于打开门。姜序开阀后会合，水流减弱。唐栖走出门，抱着文件袋，明确说材料有原始采购单和门禁记录。你护住录音和她，结尾众人仍在维修隧道，准备撤离。'),
        ('候车厅',
         '候车厅',
         '回到灯下',
         '众人沿来路从维修隧道经站务室回到候车厅，陈砚跟随而未离开。你按选择优先照顾唐栖或保护材料，但两件事都通过在场的人协作完成。你把录音笔交还唐栖，她把文件袋中的采购单、验收单和门禁记录摊在长椅上；姜序当面否认验收单的签名，唐栖播放旧录音核对。列车仍停站，乘客让出长椅，提供毛巾和热水。结尾证据得到相互印证，等待正式记录。'),
        ('候车厅',
         '候车厅',
         '那十九秒有了回声',
         '司机当面确认列车继续停站，将按事故程序上报；姜序写下真实故障和救援记录并愿意作证，唐栖确认材料保全。按玩家选择陪唐栖确认经过或同姜序核对日志，回顾本路线选择带来的实际帮助。陈砚留在现场面对质询，不能写成已被定罪。你找到唐栖、理解求助缘由且共同保住了证据，完整回应开篇十九秒语音。结局停在候车厅灯下，不新设悬案，不再要求选择。')],
 '姜序': [('站务室',
         '站务室',
         '面对那个签名',
         '承接玩家向陈砚重提故障或向年轻人询问消息。年轻人当面自报许川，给你听唐栖原有的求助语音。你说清排水泵未更换的风险和自己没有在验收单上签名；陈砚当面要求不要耽误放行。许川从旧时刻表处找到十七号铜牌，你指出消防通道和站务室的位置。你从沉默转为提供可以行动的帮助，你同许川经消防通道进入站务室，他用铜牌打开十七号柜，取出录音笔、图纸和日志。按选择先核对故障终端或先听旧录音，确认检修被写成已完成、录音里唐栖追问你没有签过的验收单。你明确否认签名，承认自己先前没有说出隐患的犹豫。陈砚在门边质疑，但你决定提供通行证与应急灯协助寻找唐栖。'),
        ('维修隧道',
         '维修隧道',
         '这次没有再等',
         '你按选择先说明安全路线或检查入口状态，用自己的通行证打开站务室后方的维修通道。带许川进入维修隧道，指出右侧旧电缆槽和左侧手动阀。你留在岔口尝试松开锈死的阀门，分步检查阻力、调整用力、终于使水流减弱。许川在附近信号室门外呼喊并得到唐栖回应，你只能听到，不描写门内。陈砚跟入隧道，你从阀门旁走向许川，把已降水压的情况说明。按所选检查外侧卡栓或同唐栖约定内外配合；原著已有的维修扳手用于撬开卡住的金属。你和许川合作，唐栖在里面推门，终于救出她；她抱着保存材料的文件袋。陈砚索要录音，你明确要求先保护人和材料。结尾众人仍在维修隧道，转向撤离。'),
        ('候车厅',
         '候车厅',
         '纸上的名字',
         '你同许川护送唐栖沿维修隧道、站务室回到候车厅，陈砚跟随。按玩家选择先安置伤者或先整理材料，其他人在场协助。许川交还录音笔，唐栖摊开文件袋中的单据，递给你验收单。你当面确认不是自己的签名；旧录音与采购单、门禁记录互相印证。不要新增事故、通信或场外人物。结尾你准备写下此前不敢写的真实记录。'),
        ('候车厅',
         '候车厅',
         '写下没有修好的故障',
         '司机当面说明列车继续停站并将按事故程序上报。你按选择先记录维修事实或先核对救援经过，在日志写清排水泵故障未排除、唐栖受困并已救出，拒绝再把未完成写成完成。唐栖保留原始证据，你明确愿意说明未签名的事实并配合调查。陈砚留在现场，尚无定罪结果。回扣开篇未说完的回答，以不再沉默、人员安全、记录真实收尾，不另起事故。')],
 '陈砚': [('站务室',
         '站务室',
         '无法涂去的记录',
         '先承接你在站台安抚乘客或向年轻人询问来意；需要从站台走入候车厅时写出路程。年轻人当面自报许川，询问唐栖；姜序也当面重提排水泵故障。你听见原有求助语音，意识到无法只靠“等放行”回答这两个人。许川找到十七号铜牌，要去站务室核对。根据玩家态度写出不同回应，不强迫你悔罪，也不让列车提前驶离。你从候车厅到站务室，许川和姜序同场。许川用十七号铜牌打开柜子取得录音笔、图纸和日志；按玩家选择先核对终端或先听对方质问。故障终端与已完成检修相矛盾，旧录音让你说过的话当面重现。姜序明确表示要进隧道救人。可以辩解或承认风险，不替玩家编造自白，但不能抹去材料或强制抢到录音。'),
        ('维修隧道',
         '维修隧道',
         '给救援让出位置',
         '按玩家选择查验风险或协助维修，你与姜序、许川从站务室后方进入维修隧道，门由姜序的通行证打开。姜序解释旧电缆槽不能踩、先开左侧手动阀，他去处理水压。许川到信号室门外，得到唐栖的隔门回应；你听见她仍困在里面。把“有人受困”变成当场无法回避的事实。列车仍在等待，不能发送成功放行指令。按玩家选择协助撬栓或退开不阻拦，把决定落实为实际动作。许川用原著已有的维修扳手处理门外卡住的滑栓，唐栖在里面配合推门；姜序开阀使水压减弱后会合。铁门终于打开，唐栖带着文件袋走出来。你仍可对材料解释有异议，但没有抢走证据，也没有再阻拦救人。结尾四人在维修隧道，准备撤离。'),
        ('候车厅',
         '候车厅',
         '在众人面前',
         '你和其他三人沿维修隧道、站务室返回候车厅。许川把录音笔递给唐栖，唐栖实际接住，再把采购单、验收单和门禁记录铺在长椅上。现在由你亲自落实所选行动：若请姜序核对签名，你开口请他当面辨认，他明确否认验收单签名；若说明门禁设置，你只说明自己已经确定的操作。不要把“区分事实和指控”演成查问姜序的新身世。姜序没有新造的值班、进场签名、调档、卡号或操作记录。门禁修改和防火门切手动属于陈砚，绝不能转嫁给姜序。唐栖按下她已接到的录音笔，播放陈砚“工程做没做完，不影响列车按时跑”的旧谈话，让记录之间的矛盾留在现场。你可提出按原件核查，不强迫认罪或宣称获谅解。结尾唐栖仍保管录音笔，许川仍保管日志，司机走到众人面前，列车仍停站，未开始下一章正式交接。'),
        ('候车厅',
         '候车厅',
         '留下来面对',
         '你先亲自落实所选行动。若选择说明经手记录，你按已知事实留下署名说明；若选择交出现场处置，你明确请司机负责事故上报，请姜序如实登记，自己留在现场接受核查。许川必须先把日志递给姜序，姜序接过后才写故障与救援经过，不能从自己衣袋凭空取出。司机当面确认列车继续停站并按事故程序接手上报；若此前你安排过报警，此处明确回应司机通过应急终端转报的安排，不虚构警方回电。唐栖继续保管录音笔与原始材料，姜序写完后保管日志。按已经发生的先后记事，不新增具体分钟数。回扣零点前放行的压力：这次人员安全先于时刻表。现场危机结束，责任待调查，不强迫忏悔、不写拘捕、定罪或新危机。')]}

CHOICES = {'唐栖': [('敲门求助，辨认回应', '留在门边，听清外面的动静'), ('先护好文件袋，请许川搀扶撤离', '先确认路况和照明，再跟着姜序撤离'), ('先播放录音，请在场的人听完', '先核对验收单，请姜序辨认签名'), ('确认材料由谁保管和接手', '核对故障日志，让救援经过留下记录')],
 '许川': [('向陈砚打听唐栖，暂不提录音', '走近维修工，问他是否见过唐栖'), ('先核对图纸，再请姜序带路', '请姜序说清进水风险，一起去救人'), ('先扶唐栖撤离，请姜序照应材料', '先护住材料，和姜序一起扶人撤离'), ('陪唐栖核对经过，确认调查接手', '与姜序核对日志，保留救援记录')],
 '姜序': [('询问年轻人收到的消息，说出隐患', '重提排水泵故障，听陈砚回应'), ('先讲清安全路线，再开阀降水压', '先检查通道入口，再带许川进去'), ('先安置唐栖，再当面核对签名', '先护住材料，再请乘客协助伤者'), ('如实记下未完成的维修', '与唐栖核对救援经过，留下证词')],
 '陈砚': [('回应乘客，再听年轻人的来意', '走向年轻人，听清他在找谁'), ('随维修人员查验隧道积水', '让姜序带路，去确认受困情况'), ('留在现场，听完逐项质问', '先核对原始单据，再回应质疑'), ('说明自己经手的记录，配合核查', '交出现场处置，让故障如实登记')]}


def role_name(package, state):
    if package.get('sourceAnalysis', {}).get('sha256') != RAINY_SOURCE or state.get('storyScope') != 'source':
        return None
    # A previously completed canonical save must not restart at zero when the
    # newer interactive scene policy is installed.
    if state.get('sourceProgress') == 'chapter_014' and not any(e['id'].startswith(PREFIX) for e in state.get('derivedEvents', [])):
        return None
    return next((c['name'] for c in package['characters'] if c['id'] == state.get('playerCharacterId') and c['name'] in SCENES), None)


def completed_steps(state):
    ids = {e['id'] for e in state.get('derivedEvents', [])}
    step = 0
    while step < STEPS and PREFIX + str(step + 1) in ids:
        step += 1
    return step


def scene(package, state):
    name = role_name(package, state)
    step = completed_steps(state)
    return SCENES[name][step] if name and step < STEPS else None


def directions(package, state):
    name = role_name(package, state)
    current = scene(package, state)
    if not current:
        return []
    step = completed_steps(state)
    # Public summaries explain the tactic, without revealing the outcome.
    return [{'id': f'direction_player_{step+1}_{i+1}', 'title': title,
             'summary': CHOICE_DETAILS.get(name, {}).get(step, {}).get(i, title + '。' + CHOICE_HINTS[name][step]),
             'statePatch': scene_patch(package, state, title)} for i, title in enumerate(CHOICES[name][step])]


def scene_patch(package, state, action):
    name = role_name(package, state)
    step = completed_steps(state)
    location, other_location, title, brief = SCENES[name][step]
    locations = {p['name']: p['id'] for p in package['locations']}
    characters = {c['name']: c['id'] for c in package['characters']}
    # Only bind people who participate directly, including a voice across a door.
    present = list(SCENES) if name == '唐栖' else ['许川', '姜序', '陈砚']
    if step >= 1:
        present = list(SCENES)
    character_locations = {characters[n]: locations[other_location] for n in present}
    character_locations[characters[name]] = locations[location]
    # Source props become available only in their authored scene, recorded in
    # the branch ledger. No model may invent these availability decisions.
    props = ['手机', '录音笔', '图纸', '日志', '档案']
    if name == '唐栖':
        props += ['文件袋']
    else:
        props += ['铜牌', '通行证']
    if step >= 1 or name == '唐栖':
        props += ['通行证', '扳手', '文件袋']
    owners = {'手机': name, '文件袋': '唐栖', '档案': '唐栖', '通行证': '姜序',
              '铜牌': '许川', '图纸': '许川', '扳手': '许川', '日志': '姜序' if step == STEPS-1 else '许川',
              '录音笔': '唐栖' if step >= 2 or (name == '唐栖' and step >= 1) else '许川'}
    additions = {'events': [{'id': PREFIX + str(step+1), 'name': title, 'summary': OUTCOMES[name][step] + ' 本次选择：' + action}],
                 'changes': [{'kind': 'item', 'entityId': item['id'], 'summary': '当前场景中已确认出现：' + item['name'],
                              'attributes': {'available': True, 'ownerCharacterId': characters[owners[item['name']]]}}
                             for item in package['items'] if item['name'] in props]}
    return {'playerLocationId': locations[location], 'characterLocationIds': character_locations,
            'derivedAdditions': additions}


def prepare_direction(package, parent, selected, player_direction=None):
    current = scene(package, parent['branchState'])
    if not current:
        return selected
    action = player_direction or selected['title']
    # A typed action is not permission to perform the entire next chapter.
    # Opening cards are submitted as text by the UI, so recognize those exact
    # authored actions as well as currently published direction titles.
    authored = [d['title'] for d in parent.get('nextDirections', [])]
    authored += [d['title'] for d in parent.get('openingActions', [])]
    advance = (not player_direction or action in authored
               or any(action == title + '。' + d.get('summary', '') for d in parent.get('openingActions', []) for title in [d['title']])
               or advances_current_scene(package, parent['branchState'], action))
    # Preserve the exact chosen action for history/idempotency. The scene event
    # supplies external responses; prose must not make an unchosen player decision.
    action_id = hashlib.sha256((parent['id'] + action).encode()).hexdigest()[:16]
    return {**{k: v for k, v in selected.items() if k not in ('canonicalBeatId', 'followupBeatId', 'rejoinTargetId', 'arcId', 'directionLevel')},
            'title': action, 'summary': action if player_direction else selected['summary'], 'readerInterlude': not advance,
            'statePatch': scene_patch(package, parent['branchState'], action) if advance else {
                'derivedAdditions': {'events': [{'id': 'event_player_action_' + action_id,
                                                'name': '临场行动', 'summary': '在当前场景尝试处理所选行动：' + action}]}}}


def advances_current_scene(package, before, action):
    """Conservatively map explicit free-text tactics to authored scene work.

    This only proposes a milestone. Both its outcome and the requested action
    must still pass review before state is committed. Unrecognized tactics stay
    local, with the stage choices still available afterwards.
    """
    if re.search(r'不|别|拒绝|假装|先等等|稍后', action):
        return False
    name, step = role_name(package, before), completed_steps(before)
    if step == 0:
        pattern = r'敲门求助|呼救|配合.{0,8}(?:推门|开门)' if name == '唐栖' else r'(?:询问|打听|问).{0,10}(?:年轻人|维修工|姜序|陈砚|唐栖)|(?:说明|重提).{0,8}(?:故障|停站|隐患)'
    elif step == 1:
        pattern = r'撤离|(?:回到|返回|回).{0,5}候车厅' if name == '唐栖' else r'(?:进入|走进|前往|去).{0,8}(?:隧道|救人)|开阀|撬栓|配合.{0,8}推门'
    elif step == 2:
        pattern = r'(?:回到|返回|回).{0,5}候车厅|(?:核对|比对|辨认|播放|说明).{0,10}(?:签名|门禁|记录|录音|材料)'
    else:
        pattern = r'(?:核对|记录|写下|记下|登记).{0,10}(?:日志|故障|经过)|配合.{0,5}核查|(?:确认|安排|交接|交出|接手|交代).{0,10}(?:上报|调查|保管|现场|记录)'
    return bool(re.search(pattern, action))


def turn_material(package, before, selected):
    current = scene(package, before)
    if not current:
        return selected['summary']
    if not selected.get('readerInterlude'):
        return current[3]
    place = next((p['name'] for p in package['locations'] if p['id'] == before.get('playerLocationId')), '')
    return (f'本回合只执行玩家提出的行动，仍停留在{place}。不自动展开后续救援、转场、证据揭示或结局。'
            '把复合行动逐项回应：先尝试、由现场已有条件产生结果，再写下一步安排。'
            '不可行的行动必须在故事里写出具体尝试、阻碍与可行替代，不能略过，也不能虚构成功。'
            '普通手机无信号；若要求报警，写出拨号尝试、无法接通。有同伴在场才可商议到候车厅后请求司机用应急终端转报，独处时不得凭空出现同伴。'
            '只有司机实际在场且已经接手时，才可写他上报；不能虚构警方接警、回电或抵达。'
            '不替玩家承诺认罪、交出证据或改变立场。用当场人的回应使选择有效，但不能凭空改变权威状态。')


def check_scene_result(package, before, text, interlude=False):
    """Reject missing milestone evidence before its state patch can be saved.

    These are minimum textual checks, not a claim of literary/semantic scoring.
    The full fact and viewpoint guards still run alongside them.
    """
    import re
    name = role_name(package, before)
    if not name:
        return
    step = completed_steps(before)
    if step >= STEPS:
        raise ValueError('这条路线已经完成')
    surnames = {c['name'][0] for c in package['characters']}
    for match in re.finditer(r'([赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕张孔曹严华金魏陶姜谢邹苏潘董袁唐马刘叶林])(?:师傅|师父|老师|先生|女士|主管)', text):
        if match[1] not in surnames:
            raise ValueError('正文新增了素材中没有的人物：' + match[0] + '。不得用新证人或其经历解释现有证据')
    for date in re.findall(r'(?:\d{1,2}|[一二三四五六七八九十]{1,3})月(?:\d{1,2}|[一二三四五六七八九十]{1,3})[日号]', text):
        if date not in ('十月十八日', '十月十八号', '10月18日', '10月18号'):
            raise ValueError('正文擅自补写了未经登记的证据日期：' + date + '。只核对材料中明确给出的异常，不新造日期')
    recording_mixup = (r'录音笔(?:里|中|内)[^，。！？\n]{0,20}(?:求助语音|求救语音)|'
                       r'录音笔(?:(?!手机)[^。！？\n]){0,30}(?:播放|放出|重播)[^，。！？\n]{0,8}(?:求助语音|求救语音)|'
                       r'(?:求助语音|求救语音)[^，。！？\n]{0,15}(?:存|保留)[^，。！？\n]{0,8}录音笔')
    if re.search(recording_mixup, text):
        raise ValueError('求助语音保存在手机中；录音笔保存的是唐栖与陈砚的调查谈话，两者不能混用')
    for prop in ('撬棍', '复印机', '备用钥匙', '墨水颜色', '新的刮痕', '巡检表', '进场登记', '施工方确认', '现场负责人栏', '你的卡号', '他的卡号'):
        if prop in text:
            raise ValueError('本场景没有提供这些工具或可取证细节：' + prop + '。只使用已提供的材料和工具')
    if re.search(r'姜序[^。！？\n]{0,55}(?:终端[^。！？\n]{0,12}(?:腰|口袋)|(?:腰|口袋)[^。！？\n]{0,12}终端)', text):
        raise ValueError('随车应急终端由司机使用，姜序没有随身终端；不能新增通信设备或设备故障')
    if step == 2 and re.search(r'回勾|编号章|编号跳|采购申请页|(?:数量|数字|金额|拨付)[^。！？]{0,20}(?:不一致|对不上|差额)|(?:另一|两)组数字|(?:签名|自己的字)[^。！？]{0,25}(?:倾斜|工整|笔画|末笔|这么斜)', text):
        raise ValueError('验收单只能确认姜序否认签名，不得新造笔迹特征、编号缺页或采购数量差额作为证据；沿原著的假签名、挪款、故障未修和门禁修改核对')
    if (step == 3 or step == 2 and name == '唐栖') and re.search(r'(?:先|再)回候车厅|你[^。！？\n]{0,20}(?:往|向)候车厅(?:方向)?走', text):
        raise ValueError('众人此前已到候车厅，本章一直在候车厅核对或收尾，不能又走回同一个候车厅')
    if interlude:
        return
    required = [('门', '锁'), ('候车厅',), ('签名', '验收单'), ('日志', '记录')] if name == '唐栖' else [('录音',), ('唐栖',), ('候车厅',), ('日志', '记录')]
    if not any(word in text for word in required[step]):
        raise ValueError('正文遗漏本阶段关键事件：' + SCENES[name][step][2])
    if step == STEPS-1:
        donor, recipient = ('你' if name == '许川' else '许川'), ('你' if name == '姜序' else '姜序')
        from .cocreation import narration_outside_dialogue
        prose = narration_outside_dialogue(text)
        # Accept pronoun handoffs and receiver-led phrasing, not just one
        # template sentence. The separate ending check confirms final ownership.
        handed_over = False
        for paragraph in prose.split('\n\n'):
            positive = re.sub(r'(?:没有|没|未|拒绝)[^。！？]{0,12}(?:递|交|接)(?:过|给|到|住|稳|向)?', '', paragraph)
            if (donor in paragraph and recipient in paragraph and '日志' in paragraph
                    and re.search(r'(?:递|交|接)(?:过|给|到|住|稳|向)', positive)):
                handed_over = True
        if not handed_over:
            raise ValueError('日志此前由许川保管，结局必须交代许川把日志递给姜序后才可由姜序登记和保管')
        if re.search(r'(?:下午[^。！？\n]{0,16}语音|值班表|没值过那个班|日志[^。！？\n]{0,25}(?:抽屉|柜子)|(?:许川|你)[^。！？\n]{0,8}(?:还没|尚未|没)[^。！？\n]{0,4}听过)', text):
            raise ValueError('结局不能推翻已完成的事实：求助语音在当晚发送，录音已当面共同听过；没有下午的值班表争议；日志由姜序随身保管，不能放进抽屉或柜子')
        if not re.search(r'(?:列车|火车|放行)', text) or not re.search(r'上报|报告|事故程序|调查', text):
            raise ValueError('结局必须交代停止放行、真实记录与事故调查的接手，不能只有抒情')


def fact_sheet(package, before, interlude=False):
    name = role_name(package, before)
    if not name:
        return ''
    facts = ('固定人物：许川、陈砚、姜序为男性，NPC用“他”；唐栖为女性，NPC用“她”；当前玩家始终用“你”。'
             '固定关系与环境：许川和唐栖原本就是朋友，救出后是重新见面，不是初次认识或只见过照片。'
             '许川确实收到了唐栖当晚二十二点五十五分的十九秒求助语音，正是循语音来救人；不能改成下午发送、没有收到、凭定位或下午新短信找来。'
             '阀门固定在隧道岔口，姜序不能拿着阀门走动；返回候车厅必须沿维修隧道经站务室，不能新增通往站台的出口。'
             '固定边界：手机保存最初的十九秒求助语音，防水录音笔保存唐栖与陈砚的调查谈话（没有许川的声音），可以正常播放，不能新增录音笔故障。不能把求助语音放进录音笔。'
             '不新增具名人物、证人、过去的会面、来电或纸面的日期数字。没有谁当天没上班、谁的卡号、谁共同点击检修完成的事实，不能用这些说法辩解。原文只提供的异常才是证据；不要用推理补造证据。'
             '人物经历绑定：下午追查旧档案、质疑维修记录的是唐栖；陈砚是被质询的值班主管，不是调查者。'
             '隧道左侧为手动阀，右侧为旧电缆槽；不要把左右写反。'
             '铜牌须在现场找到后才能使用，不能让许川从未交代过的背包里直接拿出。'
             '照明沿用应急灯或手机屏幕亮光，没有手电、撬棍、备用钥匙或复印机。站务室没有已登记可用的座机、分机或无线电。'
             '应急通信终端仅在司机处，姜序没有随身终端；站务室的故障终端是固定设备，不能取下带走。'
             '不编造刮痕、门框凹痕、墨水颜色、笔画形状或设备型号来证明责任。')
    step = completed_steps(before)
    if (step >= 2 or name != '唐栖') and (not interlude or step >= (3 if name == '唐栖' else 1)):
        facts += ('本章可用的证据事实：姜序未在隧道防水工程验收单上签字，单上却有他的签名。'
                  '排水泵故障未排除，检修在终端被标记完成。采购款被陈砚挪用，原始采购单留存。'
                  '陈砚把防火门切到手动模式，并改过门禁记录；不能另造别人冒用工牌的日期。'
                  '旧录音中陈砚说过：“工程做没做完，不影响列车按时跑。”除此之外不要编造新证人或替人签收的故事。')
    if step == 3 or step == 2 and name == '唐栖':
        facts += '众人已经在候车厅，本章开场和结尾都在这里，不能无故转回隧道、摸阀门或再次走回候车厅。'
    if step == STEPS-1 and not interlude:
        facts += '上一章四人已共同听过调查录音，不能又说许川没听过；没有下午查看值班表或不该值班的争议，不要补造这些往事。日志要由姜序随身保管，不能前面说不离手后面又放进抽屉或柜子。'
        facts += '证据保管方式固定：文件袋和录音笔由唐栖贴身保管，日志由姜序保管，司机负责上报现场情况；日志此前由许川拿到，交给姜序时必须交代递交动作。不要新增钥匙、上锁的箱柜或把原件交给别的人。司机有原著明确的随车应急卫星终端，普通手机无信号不妨碍他按事故程序上报；不编造外部回电。日志只按本路线已经发生的先后记下故障、发现受困、救援与上报；不照搬原著具体时刻，不补造分钟数。调查会继续，不能宣称已定罪或隐患已修复。'
    return facts


def check_reviewed_ending(package, before, body, ending, interlude=False):
    """Require extracted story outcomes to agree with the pending state patch."""
    current = scene(package, before)
    if not current:
        return
    if not isinstance(ending, dict):
        raise ValueError('缺少章末实际状态核对')
    quote = ending.get('evidence')
    if not isinstance(quote, str) or len(quote) < 8 or quote not in body:
        raise ValueError('章末状态必须引用本章实际出现的原句作为证据')
    for field in ('record_editor', 'door_operator'):
        actor = ending.get(field)
        if actor is not None and actor != '陈砚':
            raise ValueError('门禁记录和防火门手动设置由陈砚修改，不能改写为' + str(actor) + '所为')
    if ending.get('recording_speaker') not in (None, '陈砚'):
        raise ValueError('旧录音中承认工程未完成仍按时运行的是陈砚，不能把录音声音改成别的人或伪造剪辑')
    if ending.get('archive_investigator') not in (None, '唐栖'):
        raise ValueError('下午追查档案的是唐栖，不能把调查经历转移给别人')
    destination = next((p['name'] for p in package['locations'] if p['id'] == before.get('playerLocationId')), '') if interlude else current[0]
    if ending.get('final_location') != destination:
        raise ValueError('章末玩家实际地点与已确认事件不符，本章必须停在' + destination)
    step = completed_steps(before)
    if interlude:
        rescued = step >= (1 if role_name(package, before) == '唐栖' else 2)
        if ending.get('tang_safe') is not None and ending['tang_safe'] != rescued:
            raise ValueError('自由行动不能自动完成尚未进行的救援，也不能推翻已经完成的救援')
        return
    rescue_scene = step == (0 if role_name(package, before) == '唐栖' else 1)
    if rescue_scene:
        if ending.get('tang_safe') is not True or ending.get('door_open') is not True:
            raise ValueError('本阶段救援已经完成，正文不能让唐栖仍被困住或又回到门内')
    elif step >= 1 and ending.get('tang_safe') is False:
        raise ValueError('此前已经完成救援，正文不能让唐栖重新被困在信号室')
    if step == STEPS-1 and ending.get('handoff_confirmed') is not True:
        raise ValueError('本章是结局，正文必须交代司机接手事故程序和真实记录，不能停在等待决定')
    if step == STEPS-1 and ending.get('log_holder') != '姜序':
        raise ValueError('结局日志应由姜序实际保管，不能写完后仍由别人持有或放进无人看管的柜子')
    if step >= (1 if role_name(package, before) == '唐栖' else 2) and ending.get('recorder_holder') != '唐栖':
        raise ValueError('返回候车厅后录音笔应交还唐栖并由她保管，必须实际完成交接，不能拒接或仍留在许川手中')
    if step == (1 if role_name(package, before) == '唐栖' else 2):
        handoff = ending.get('recorder_handoff_evidence')
        if (not isinstance(handoff, str) or len(handoff) < 8 or handoff not in body
                or re.search(r'没接|未接|拒接|拒绝', handoff)):
            raise ValueError('本章必须交代许川交还录音笔、唐栖实际接到的动作，不能只凭结尾道具在场推定已经交接')


CHOICE_HINTS = {'唐栖': ['在门边停下来听，分辨雨声之外是否有人，避免白白耗尽体力。', '保护随身材料，确认照明、落脚点和谁来搀扶，尽快回到安全的候车厅。', '让不同来源的材料互相印证，分清已经证实和仍需追问的部分。', '把这一夜留下的材料和经过交代清楚，确认后续由谁负责。'],
 '许川': ['先问清她最后留下的线索，从不同人的回应中寻找能采取的下一步。', '和熟悉设备的人确认路线及风险，再按分工行动。', '与同伴分工撤离，照顾受困的人，同时保住能够说明问题的材料。', '对照最初的求助，确认人已安全、材料有人保管、经过留下记录。'],
 '姜序': ['用你知道的维修事实回应眼前的问题，听清其他人带来的信息。', '先确认水流、通道和分工，让后面的行动有依据。', '在安全的位置安置人和材料，核对你能够确认的事实。', '把未完成的维修与实际发生的救援分开记录，让自己的说法可以被核查。'],
 '陈砚': ['先听清眼前人的问题，再作回应，不能只用放行时间搪塞。', '离开口头争论，到现场确认人员与设备的实际风险。', '在众人面前逐项核对材料，区分事实和对责任的解释。', '明确现场如何交接、记录如何保留，并留下来面对后续核查。']}
OUTCOMES = {'唐栖': ['许川隔门回应并协作打开铁门，唐栖带着文件袋脱困，与三人在维修隧道会合。',
        '众人返回候车厅，录音笔交还唐栖，文件袋材料保全，等待当面核对。',
        '录音、采购单和门禁记录相互印证，姜序否认验收单签名。',
        '人员安全，证据保全，真实日志留下，司机明确按事故程序接手上报。'],
 '许川': ['取得十七号铜牌，姜序说明可进入站务室的路线。 取得录音、图纸和日志，发现故障与维修记录矛盾。',
        '进入维修隧道，确认开阀分工，并与门内的唐栖取得联系。 铁门打开，唐栖带着材料脱困，众人在维修隧道会合。',
        '众人安全返回候车厅，录音交还唐栖，材料相互印证。',
        '求助得到回应，人员安全，证据和真实记录有人接手。'],
 '姜序': ['当面说出维修隐患，许川提供唐栖的求助线索。 核对故障和录音，明确验收单签名并非本人所签。',
        '进入维修隧道，开阀减小水压，与门内的唐栖取得联系。 同许川协作打开铁门，唐栖获救，材料保全。',
        '护送唐栖回到候车厅，当面核对原始材料和签名。',
        '真实记录故障与救援，明确愿意配合调查，停止继续隐瞒。'],
 '陈砚': ['在候车厅听取许川与姜序对唐栖、排水泵的询问。 当面面对录音与维修记录的矛盾，救援成为当前事务。',
        '到维修隧道亲眼确认积水，并听见门内唐栖回应。 没有继续阻拦救援，铁门打开，唐栖带着材料获救。',
        '回到候车厅，当面核对原始记录、录音与签名争议。',
        '列车继续停站，危机交由事故程序处理，本人留在现场接受核查。']}

# Distinct tactics specify what the player personally does, rather than two
# synonyms for the same movement. Milestones remain authored and validated.
CHOICES['陈砚'] = [
    ('向乘客说明停站原因，再问年轻人的来意', '先问年轻人唐栖的去向，请姜序回应乘客'),
    ('接过照明，配合许川检查门外滑栓', '让姜序先说明积水风险，自己守住通道'),
    ('先说明自己经手的门禁设置，再接受质询', '请姜序核对签名，逐项区分事实和指控'),
    ('留下署名说明，配合后续核查', '交出现场处置，让故障如实登记'),
]
CHOICE_DETAILS = {'陈砚': {
    0: {0: '亲自解释为什么暂不能放行，听取乘客最迫切的需要，再接待年轻人。',
        1: '请姜序暂时回应乘客，把注意力放在失联者身上，问清求助消息。'},
    1: {0: '亲自照亮滑栓位置，按维修人员的指引协助救援，不抢动设备。',
        1: '问清不能踩踏的位置，提醒同行者绕开危险区域，为救援留出通道。'},
    2: {0: '主动交代已知的设置操作，回应唐栖的具体问题；不替未查清的事情下结论。',
        1: '请姜序指出他没有签过的记录，再逐项回应；保留解释权，也保留原件。'},
    3: {0: '在已有日志上留下本人经手事项的署名说明，接受后续核查。',
        1: '把上报和维修记录交由司机、姜序分别处理，自己退开并留在现场配合。'},
}}
