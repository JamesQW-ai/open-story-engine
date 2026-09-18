"""Read-only player journal derived from the selected lineage, never model scores."""
import json
import re
from . import api_routes
from .api_relationships import known_relationships
from .character_presentation import character_presentation, known_opening_name, known_status
from .api_openings import RAINY_SOURCE
from .cocreation import beat_for_state, branch_direction, assert_published_directions

ROLE_GOALS = {
    '许川': '找到唐栖，查清她求助的缘由',
    '唐栖': '设法脱困，让调查得到回应',
    '陈砚': '保障人员安全，保留真实记录并完成现场交接',
    '姜序': '面对维修隐患，把未说完的话说清楚',
}

SOURCE_PROGRESS_RE = re.compile(r'^chapter_(\d+)$')


def source_route_progress(package, state, beat, terminal=False):
    """Return a graph-backed source-route percentage when its denominator is explicit."""
    if state.get('storyScope') != 'source' or not isinstance(beat, dict):
        return None
    graph = package.get('story', {}).get('narrativeGraph', {})
    beats = graph.get('beats', [])
    ending_ids = set(graph.get('endingBeatIds', {}).values())
    if not ending_ids:
        return None

    def chapter_number(value):
        match = SOURCE_PROGRESS_RE.fullmatch(value) if isinstance(value, str) else None
        return int(match.group(1)) if match else None

    available = set()
    ending_progress = []
    for candidate in beats:
        if not isinstance(candidate, dict):
            continue
        number = chapter_number(candidate.get('branchState', {}).get('sourceProgress'))
        if number is None:
            continue
        available.add(number)
        if candidate.get('id') in ending_ids:
            ending_progress.append(number)
    if not ending_progress:
        return None
    denominator = max(ending_progress)
    if denominator < 1 or not set(range(1, denominator + 1)).issubset(available):
        return None
    current = chapter_number(state.get('sourceProgress'))
    if current is None:
        current = chapter_number(beat.get('branchState', {}).get('sourceProgress'))
    if current is None or current < 1 or current > denominator:
        return None
    if terminal or denominator == 1:
        return 100 if terminal else 0
    return round(100 * (current - 1) / (denominator - 1))


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


HEALTH_NOTE = '停滞信号与收束清单仅作路线诊断；不表示应强制结束，也不表示自然结局已写成。'


def route_health(store, sid, nodes, package, status):
    """Compact stall + closure signals for the journal; never invents progress."""
    from .route_monitor import monitor
    from .route_closure import preparation
    # Keep health resilient: incomplete fixtures or retired packages must not
    # break the journal, and unknown never becomes "checklist clear".
    try:
        contract = store.contract(sid)
    except (ValueError, TypeError, KeyError, AttributeError):
        contract = None
    if contract is None:
        return dict(signals=[], review_recommended=False, window=None,
                    unchanged_state_turns=None, repeated_action_turns=None,
                    source_progress_streak=None,
                    closure_readiness='unknown', closure_outstanding_count=None,
                    ending_written=False, note=HEALTH_NOTE)
    try:
        mon = monitor(nodes, package, contract, status)
        signals = list(mon['signals'])
        review = bool(mon['review_recommended'])
        window = mon['window']
        unchanged = mon['unchanged_state_turns']
        repeated = mon['repeated_action_turns']
        source_progress_streak = mon.get('source_progress_streak')
    except (ValueError, TypeError, KeyError, AttributeError):
        signals, review, window, unchanged, repeated, source_progress_streak = [], False, None, None, None, None
    try:
        closure = preparation(package, contract, nodes, status)
        from .route_lifecycle import view
        ending_written = view(store, sid, nodes, closure)['ending_written']
        readiness = closure['readiness']
        outstanding = closure['outstanding_count']
    except (ValueError, TypeError, KeyError, AttributeError):
        readiness, outstanding = 'unknown', None
        ending_written = False
    return dict(signals=signals, review_recommended=review, window=window,
                unchanged_state_turns=unchanged, repeated_action_turns=repeated,
                source_progress_streak=source_progress_streak,
                closure_readiness=readiness, closure_outstanding_count=outstanding,
                ending_written=ending_written, note=HEALTH_NOTE)


def route_status(store, sid, nodes, package, terminal=None):
    state = nodes[-1]['branchState']
    endings = preferences(store, sid)['ended']
    ended = next((endings[n['id']] for n in nodes if n['id'] in endings), None)
    if ended:
        return ended
    if terminal is None:
        if api_routes.role_name(package, state):
            terminal = api_routes.completed_steps(state) == api_routes.STEPS
        else:
            beat = player_beat(package, state)
            ids = set(package['story'].get('narrativeGraph', {}).get('endingBeatIds', {}).values())
            terminal = bool(state.get('storyScope') == 'source' and beat and beat['id'] in ids)
            terminal = terminal or _mock_fixture_terminal(package, nodes)
    return 'completed' if terminal else 'active'


def _mock_fixture_terminal(package, nodes):
    """Recognize only the deterministic browser fixture's authored terminal turn."""
    if package.get('id') != 'taixu-relics-part1' or not nodes:
        return False
    current = nodes[-1]
    planning = current.get('planning') or {}
    state = current.get('branchState') or {}
    return (
        planning.get('narrativeOrigin') == 'mock_structural_fixture'
        and isinstance(state.get('freeTextProgress'), int)
        and state['freeTextProgress'] >= 4
    )


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
                   if character_appears(n.get('narrativeText', ''), name, is_player) or known_opening_name(n, name)]
    if not appearances and nodes:
        public_status = known_status(nodes, character['id'])
        ref = public_status['evidence']
        if ref and any(n['id'] == ref['branch_id'] and name in n.get('narrativeText', '') for n in nodes):
            appearances = [ref['page']]
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
    locations = {p['id']: p['name'] for p in list(package['locations']) + state.get('derivedLocations', [])}
    text = '\n'.join(n.get('narrativeText', '') for n in nodes)
    characters = list(package['characters']) + state.get('derivedCharacters', [])
    if not any(c['name'] == name for c in characters):
        characters.append({'id': persona.get('id', 'player'), 'name': name})
    people = [character_presentation(nodes, c, card, package['id'], character_appears)
              for c in characters if (card := character_card(nodes, c, name))]
    character_names = {p['id']: p['name'] for p in people if isinstance(p, dict) and isinstance(p.get('name'), str)}
    item_names = {i['id']: i['name'] for i in list(package.get('items', [])) + state.get('derivedItems', [])
                  if isinstance(i, dict) and isinstance(i.get('name'), str) and i['name'] in text}
    outcome_labels = {'alive': '已确认存活', 'dead': '已死亡', 'departed': '已离队',
                      'missing': '已确认失踪', 'injured': '已受伤'}
    goal_labels = {'completed': '已完成', 'transformed': '已转化', 'abandoned': '已放下'}
    thread_labels = {'resolved': '已解决', 'abandoned': '已放下'}

    def effects(before, after):
        changes = []
        old = before.get('playerLocationId', before.get('currentLocationId'))
        new = after.get('playerLocationId', after.get('currentLocationId'))
        if new != old and new in locations:
            changes.append('来到' + locations[new])
        for iid in set(after.get('inventory', [])) - set(before.get('inventory', [])):
            if iid in item_names:
                changes.append('获得' + item_names[iid])
        if set(after.get('knownFacts', [])) - set(before.get('knownFacts', [])):
            changes.append('记下了新的线索')

        old_outcomes = before.get('characterOutcomeStates', {})
        new_outcomes = after.get('characterOutcomeStates', {})
        if isinstance(old_outcomes, dict) and isinstance(new_outcomes, dict):
            for cid, outcome in new_outcomes.items():
                if not isinstance(outcome, dict) or cid not in character_names:
                    continue
                previous = old_outcomes.get(cid, {})
                if not isinstance(previous, dict) or any(previous.get(k) != outcome.get(k) for k in ('status', 'permanence')):
                    label = outcome_labels.get(outcome.get('status'))
                    if label:
                        permanent = '（永久）' if outcome.get('permanence') == 'permanent' else ''
                        changes.append(character_names[cid] + label + permanent)

        old_entities = before.get('readerEntityStates', {})
        new_entities = after.get('readerEntityStates', {})
        if isinstance(old_entities, dict) and isinstance(new_entities, dict):
            for iid, entity in new_entities.items():
                if iid in item_names and isinstance(entity, dict) and entity.get('destroyedPermanently') is True:
                    previous = old_entities.get(iid, {})
                    if not isinstance(previous, dict) or previous.get('destroyedPermanently') is not True:
                        changes.append(item_names[iid] + '已永久损毁')

        old_goals = {g.get('id'): g for g in before.get('goalLedger', []) if isinstance(g, dict) and g.get('id')}
        new_goals = {g.get('id'): g for g in after.get('goalLedger', []) if isinstance(g, dict) and g.get('id')}
        for gid, goal in new_goals.items():
            if not isinstance(goal, dict) or not isinstance(goal.get('title'), str):
                continue
            previous = old_goals.get(gid, {})
            if previous.get('status') != goal.get('status'):
                label = goal_labels.get(goal.get('status'))
                if label:
                    changes.append('目标' + label + '：' + goal['title'])
                elif previous == {} and goal.get('status') == 'active':
                    changes.append('新增目标：' + goal['title'])

        old_threads = {t.get('id'): t for t in before.get('threadLedger', []) if isinstance(t, dict) and t.get('id')}
        new_threads = {t.get('id'): t for t in after.get('threadLedger', []) if isinstance(t, dict) and t.get('id')}
        for tid, thread in new_threads.items():
            if not isinstance(thread, dict) or not isinstance(thread.get('title'), str):
                continue
            previous = old_threads.get(tid, {})
            if previous.get('status') != thread.get('status'):
                label = thread_labels.get(thread.get('status'))
                if label:
                    changes.append('问题' + label + '：' + thread['title'])
                elif previous == {} and thread.get('status') == 'open':
                    changes.append('新增问题：' + thread['title'])

        if any(after.get(key) != before.get(key) for key in ('relationships', 'derivedRelationships')):
            changes.append('相处的关系有了变化')
        return [change for change in changes if change]
    def node_effects(previous, node):
        recorded = node.get('readerOutcome', {}).get('action', {})
        summary, quote = recorded.get('summary'), recorded.get('evidence')
        if summary and quote and quote in node.get('narrativeText', ''):
            return list(dict.fromkeys([summary, *effects(previous['branchState'], node['branchState'])]))
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
    terminal = terminal or _mock_fixture_terminal(package, nodes)
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
    source_progress = source_route_progress(package, state, beat, terminal)
    if source_progress is not None and not route_name and not rainy:
        progress = source_progress
    status = route_status(store, sid, nodes, package, terminal)
    # Open-ended routes have no validated denominator yet. A single ending
    # checkpoint would misleadingly report 0% throughout a long adventure.
    measured_progress = rainy or bool(route_name) or source_progress is not None or terminal
    progress_label = ('原著路线进度' if source_progress is not None else '路线阶段') if measured_progress else '本路线历程'
    if not measured_progress:
        progress = None
    current_scene = api_routes.scene(package, state)
    current_task = (api_routes.directions(package, state)[0]['title'] if current_scene else
                    (current.get('openingActions') or [{}])[0].get('title') or '留意眼前的变化，选择下一步行动')
    if api_routes.role_name(package, state) and len(nodes) > 1:
        old_steps = api_routes.completed_steps(nodes[-2]['branchState'])
        for event in state.get('derivedEvents', []):
            if event['id'] == api_routes.PREFIX + str(old_steps + 1):
                feedback.append(event['name'])
    from .reader_consequences import enabled, goals_for
    from .reader_threads import threads_for

    def ledger_records(loader, key):
        if not enabled(package):
            return [], 'recorded'
        try:
            records = loader()
        except (ValueError, TypeError, KeyError, AttributeError):
            return [], 'unknown'
        if not isinstance(records, list):
            return [], 'unknown'
        # A legacy save can expose the public opening projection while its
        # current lifecycle ledger is absent; keep that distinction visible.
        return records, 'recorded' if key in state else 'unknown'

    goals, goal_coverage = ledger_records(lambda: goals_for(package, store.contract(sid), state), 'goalLedger')
    active_goals = [g for g in goals if isinstance(g, dict) and g.get('status') == 'active']
    if active_goals:
        current_task = active_goals[0]['title']
    elif goals:
        current_task = '当前阶段已收束，新的目标将在后续行动中记录'
    else:
        current_task = '当前还没有可记录的进行中目标'
    goal_text = '；'.join(g['title'] for g in active_goals) if active_goals else ('当前没有进行中的目标' if goals else None)
    threads, thread_coverage = ledger_records(lambda: threads_for(package, store.contract(sid), state), 'threadLedger')
    health = route_health(store, sid, nodes, package, status)
    return {'branch_id': bid, 'threads': threads, 'goals': goals,
            'ledger_coverage': {'goals': goal_coverage, 'threads': thread_coverage},
            'role_name': name, 'goal': goal_text or ( ROLE_GOALS.get(name, '探索这段故事，走到属于你的结局') if rainy else '探索这段故事，走到属于你的结局'),
            'progress': progress, 'progress_label': progress_label, 'choices_made': len(recap),
            'milestones': [m for m in checkpoints if m['complete']],
            'current_task': '回顾你作出的选择' if status != 'active' else current_task,
            'status': status, 'feedback': feedback[:2], 'clues': list(dict.fromkeys(clues)), 'people': people,
            'relationships': known_relationships(nodes, people, name),
            'recap': recap, 'lineage': [n['id'] for n in nodes],
            'route_health': health,
            'location': locations.get(state.get('playerLocationId', state.get('currentLocationId')))}
