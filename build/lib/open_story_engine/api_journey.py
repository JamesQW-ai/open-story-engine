"""Read-only player journal derived from the selected lineage, never model scores."""
import json
import re
from . import api_routes
from .api_relationships import known_relationships
from .api_openings import RAINY_SOURCE
from .cocreation import beat_for_state, branch_direction, assert_published_directions

ROLE_GOALS = {
    '许川': '找到唐栖，查清她求助的缘由',
    '唐栖': '设法脱困，让调查得到回应',
    '陈砚': '保障人员安全，保留真实记录并完成现场交接',
    '姜序': '面对维修隐患，把未说完的话说清楚',
}


def player_beat(package, state):
    # Identity-specific location / persona must not erase the chapter cursor.
    beats = package['story'].get('narrativeGraph', {}).get('beats', [])
    lookup = getattr(beats, 'get_by_source_progress', None)
    if callable(lookup):
        return lookup(state.get('sourceProgress'))
    return next((b for b in beats if b.get('branchState', {}).get('sourceProgress') == state.get('sourceProgress')), None) if state.get('sourceProgress') else beat_for_state(package, state)


def player_directions(package, state):
    route_name = api_routes.role_name(package, state)
    if route_name:
        return api_routes.directions(package, state)
    beat = player_beat(package, state)
    if not beat:
        return []
    result = []
    for source in beat['nextDirections']:
        direction = branch_direction(source)
        try:
            assert_published_directions(package, beat['nodeId'], state, [direction])
        except ValueError:
            continue
        result.append(direction)
    return result


def preferences(store, sid):
    exists = store.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_play_preferences'").fetchone()
    if not exists:
        return {'title': None, 'ended': {}}
    row = store.connection.execute('SELECT title,ended_branches_json FROM session_play_preferences WHERE session_id=?', (sid,)).fetchone()
    return {'title': row['title'], 'ended': json.loads(row['ended_branches_json'])} if row else {'title': None, 'ended': {}}


def save_preferences(store, sid, *, title=None, ended=None):
    previous = preferences(store, sid)
    with store.connection:
        store.connection.execute('INSERT INTO session_play_preferences VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET title=excluded.title,ended_branches_json=excluded.ended_branches_json',
                                 (sid, title if title is not None else previous['title'], json.dumps(ended if ended is not None else previous['ended'], ensure_ascii=False)))


def character_appears(text, name, is_player=False):
    from .cocreation import narration_outside_dialogue
    prose = narration_outside_dialogue(text)
    if is_player:
        return '你' in prose or name in prose
    for sentence in re.split(r'[。！？\n]', prose):
        if name not in sentence:
            continue
        # A memory, hypothetical future or name inside an old recording does
        # not mean that this person has entered the player's present scene.
        if re.search(r'想起|回忆|记得|脑海|曾经|下午|当年|假如|如果|语音里|录音(?:笔)?(?:里|中)|提到|提起|名字|发给', sentence):
            continue
        if re.search(re.escape(name) + r'(?:尚未|还未|并未|没有)(?:来|到|出现|露面)', sentence):
            continue
        if re.search(re.escape(name) + r'(?:[：:]|的声音|在门外|在门边|走|站|坐|说|问|答|回应|呼喊|喊|看|抬|低|伸|接|递|扶|推|握|抱|停|摇|点|穿|靠|蹲|从|也|却|正|仍|就在|没有(?:回答|接话|说话)|一)', sentence):
            return True
        if re.search(r'(?:看见|看到|认出|走向|望向|身旁的|身边的)' + re.escape(name), sentence):
            return True
    return False


def character_card(nodes, character, player_name):
    """Known-character header only; summaries are generated separately on demand."""
    name = character['name']
    is_player = name == player_name
    appearances = [n['sequence'] + 1 for n in nodes
                   if character_appears(n.get('narrativeText', ''), name, is_player)]
    if not appearances:
        return None
    # menuDescription is the public identity already shown during role selection.
    # Never substitute a character's private description or an LLM guess.
    identity = character.get('menuDescription') or ('你选择的身份' if is_player else '身份尚未明确')
    identity = identity.rstrip('。')
    if len(identity) > 40:
        identity = identity[:39] + '…'
    return {'id': character['id'], 'name': name, 'is_player': is_player,
            'first_page': appearances[0] if appearances else nodes[0]['sequence'] + 1,
            'last_page': appearances[-1] if appearances else nodes[0]['sequence'] + 1,
            'identity': identity,
            'summary': '立场与经历，随着你的探索逐渐明晰。', 'summarized': False}


def journey(store, sid, bid, package):
    nodes = store.lineage(sid, bid)
    root, current = nodes[0], nodes[-1]
    state = current['branchState']
    persona = store.contract(sid)['persona']
    name = persona['name']
    locations = {p['id']: p['name'] for p in package['locations']}
    text = '\n'.join(n.get('narrativeText', '') for n in nodes)
    characters = list(package['characters']) + state.get('derivedCharacters', [])
    if not any(c['name'] == name for c in characters):
        characters.append({'id': persona.get('id', 'player'), 'name': name})
    people = [card for c in characters if (card := character_card(nodes, c, name))]
    def effects(before, after):
        changes = []
        old = before.get('playerLocationId', before.get('currentLocationId'))
        new = after.get('playerLocationId', after.get('currentLocationId'))
        if new != old and new in locations:
            changes.append('来到' + locations[new])
        items = {i['id']: i['name'] for i in list(package.get('items', [])) + after.get('derivedItems', [])}
        for iid in set(after.get('inventory', [])) - set(before.get('inventory', [])):
            if iid in items:
                changes.append('获得' + items[iid])
        if set(after.get('knownFacts', [])) - set(before.get('knownFacts', [])):
            changes.append('记下了新的线索')
        if any(after.get(key) != before.get(key) for key in ('relationships', 'derivedRelationships')):
            changes.append('相处的关系有了变化')
        return changes
    def node_effects(previous, node):
        recorded = node.get('readerOutcome', {}).get('action', {})
        summary, quote = recorded.get('summary'), recorded.get('evidence')
        if summary and quote and quote in node.get('narrativeText', ''):
            return [summary]
        return effects(previous['branchState'], node['branchState'])
    feedback = node_effects(nodes[-2], current) if len(nodes) > 1 else []
    recap = [{'branch_id': n['id'], 'action': n.get('playerDirection') or n.get('selectedDirection', {}).get('title') or n.get('summary', ''),
              'effects': node_effects(nodes[i-1], n)}
             for i, n in enumerate(nodes) if i and n.get('kind') != 'arc_selection']
    clues = list(root.get('openingClues', []))
    for node in nodes:
        for clue in node.get('readerOutcome', {}).get('clues', []):
            if clue.get('evidence') and clue['evidence'] in node.get('narrativeText', ''):
                clues.append(clue['summary'])
    # Older saved pages have no extracted notes. Surface only discoveries with
    # concrete textual support; never use future source passages as evidence.
    legacy_text = '\n'.join(n.get('narrativeText', '') for n in nodes if not n.get('readerOutcome'))
    if legacy_text:
        for pattern, label in (
            (r'录音[\s\S]{0,160}工程做没做完，不影响列车按时跑', '旧录音中，陈砚说工程未完成不影响列车按时运行。'),
            (r'姜序[^。！？]{0,90}(?:没签过|不是我|不是自己的签名)', '姜序否认验收记录上的签名出自本人。'),
            (r'唐栖从里面迈出来|唐栖(?:带着材料)?(?:获救|走出)', '唐栖已从信号室脱困。'),
            (r'(?:已经|明确)[^。！？]{0,35}(?:事故程序上报|接手上报)', '现场情况已按事故程序上报，最终责任仍待核查。'),
        ):
            if re.search(pattern, legacy_text):
                clues.append(label)
    for clue in state.get('derivedClues', []):
        summary = clue.get('summary') or clue.get('description') or clue.get('name', '')
        if summary and (summary in text or clue.get('name', '\0') in text):
            clues.append(summary)
    # Source progress is a script-validated milestone, not freeTextProgress,
    # token count, model prose or the number of branches in other routes.
    beat = player_beat(package, state)
    ending_ids = set(package['story'].get('narrativeGraph', {}).get('endingBeatIds', {}).values())
    terminal = bool(state.get('storyScope') == 'source' and beat and beat['id'] in ending_ids)
    rainy = package.get('sourceAnalysis', {}).get('sha256') == RAINY_SOURCE
    checkpoints = []
    if rainy:
        reached = {n['branchState'].get('sourceProgress') for n in nodes[1:]}
        for value, label in [('chapter_003', '迈出调查的第一步'), ('chapter_006', '推进调查'), ('chapter_010', '直面危机')]:
            checkpoints.append({'label': label, 'complete': value in reached})
    checkpoints.append({'label': '走到这条路线的结局', 'complete': terminal})
    progress = 100 if terminal else round(100 * sum(m['complete'] for m in checkpoints) / len(checkpoints))
    route_name = api_routes.role_name(package, state)
    if route_name:
        completed = api_routes.completed_steps(state)
        terminal = completed == api_routes.STEPS
        progress = round(completed * 100 / api_routes.STEPS)
        checkpoints = [{'label': scene[2], 'complete': i < completed}
                       for i, scene in enumerate(api_routes.SCENES[route_name])]
    endings = preferences(store, sid)['ended']
    ended = next((endings[n['id']] for n in nodes if n['id'] in endings), None)
    status = ended or ('completed' if terminal else 'active')
    current_scene = api_routes.scene(package, state)
    current_task = (api_routes.directions(package, state)[0]['title'] if current_scene else
                    (current.get('openingActions') or [{}])[0].get('title') or '留意眼前的变化，选择下一步行动')
    if api_routes.role_name(package, state) and len(nodes) > 1:
        old_steps = api_routes.completed_steps(nodes[-2]['branchState'])
        for event in state.get('derivedEvents', []):
            if event['id'] == api_routes.PREFIX + str(old_steps + 1):
                feedback.append(event['name'])
    from .reader_consequences import enabled, goals_for
    goals = goals_for(package, store.contract(sid), state) if enabled(package) else []
    active_goals = [g for g in goals if g['status'] == 'active']
    if active_goals:
        current_task = active_goals[0]['title']
    elif goals:
        current_task = '决定接下来想做的事'
    goal_text = '；'.join(g['title'] for g in active_goals) if active_goals else ('当前目标已告一段落，下一步由你决定' if goals else None)
    return {'goals': goals, 'role_name': name, 'goal': goal_text or ( ROLE_GOALS.get(name, '探索这段故事，走到属于你的结局') if rainy else '探索这段故事，走到属于你的结局'),
            'progress': progress, 'progress_label': '路线阶段', 'milestones': [m for m in checkpoints if m['complete']],
            'current_task': '回顾你作出的选择' if status != 'active' else current_task,
            'status': status, 'feedback': feedback[:2], 'clues': list(dict.fromkeys(clues)), 'people': people,
            'relationships': known_relationships(nodes, people, name),
            'recap': recap, 'lineage': [n['id'] for n in nodes],
            'location': locations.get(state.get('playerLocationId', state.get('currentLocationId')))}
