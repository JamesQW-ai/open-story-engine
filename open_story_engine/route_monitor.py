"""Read-only route signals from committed ancestry, never a narrative verdict."""
import hashlib
import json
import re

from .reader_consequences import goals_for
from .reader_threads import threads_for

VERSION = 'route-monitor/2'
WINDOW = 3
RECENT_LIMIT = 12

# Transaction ids, counters and event summaries grow even during a wait. They
# are deliberately not evidence of world/goal advancement.
FIELDS = {
    'position': ('playerLocationId', 'currentLocationId', 'characterLocationIds'),
    'source': ('sourceProgress', 'storyScope'),
    'items': ('inventory', 'itemOwnerCharacterIds', 'itemLocationIds', 'derivedItems'),
    'entities': ('readerEntityStates', 'derivedCharacters', 'derivedLocations'),
    'knowledge': ('knownFacts', 'knownSecrets', 'derivedClues', 'derivedCharacterReveals'),
    'relationships': ('relationships', 'derivedRelationships'),
}


def _stable(value):
    if isinstance(value, dict):
        return {key: _stable(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _unordered(value):
    value = _stable(value)
    return sorted(value, key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)) if isinstance(value, list) else value


def _records(records, fields):
    return _unordered([{key: _unordered(item.get(key, [])) if key in ('dependencies', 'itemDependencies') else item.get(key)
                        for key in fields} for item in records])


def _projection(state, package, contract):
    result = {group: {key: _unordered(state.get(key)) for key in keys} for group, keys in FIELDS.items()}
    result['outcomes'] = {cid: {key: item.get(key) for key in ('status', 'permanence')}
                          for cid, item in state.get('characterOutcomeStates', {}).items()}
    result['goals'] = _records(goals_for(package, contract, state), ('id', 'title', 'status', 'dependencies', 'itemDependencies', 'successor'))
    result['threads'] = _records(threads_for(package, contract, state), ('id', 'title', 'status', 'itemDependencies'))
    return result


def _normalized(text):
    return re.sub(r'\s+', '', text or '')


def _pending(records, statuses):
    return [{key: item.get(key) for key in ('id', 'title', 'status')} for item in records if item.get('status') in statuses]


def _source_progress_ranks(package):
    beats = package.get('story', {}).get('narrativeGraph', {}).get('beats', [])
    ranks = {}
    try:
        iterator = iter(beats)
    except TypeError:
        return ranks
    for index, beat in enumerate(iterator):
        progress = beat.get('branchState', {}).get('sourceProgress') if isinstance(beat, dict) else None
        if isinstance(progress, str) and progress and progress not in ranks:
            ranks[progress] = index
    return ranks


def monitor(nodes, package, contract, route_status):
    previous = None
    unchanged = repeated = 0
    source_progress_streak = 0
    previous_source_progress = None
    source_progress_regressed = False
    previous_source_rank = None
    source_ranks = _source_progress_ranks(package)
    previous_action = ''
    bodies = {}
    rows = []
    unknown = []
    for index, node in enumerate(nodes):
        state = node.get('branchState')
        current = _projection(state, package, contract) if isinstance(state, dict) else None
        is_turn = index > 0 and node.get('kind') != 'arc_selection'
        if current is None:
            unknown.append(node['id'])
        text = _normalized(node.get('narrativeText'))
        # Empty or tiny dialogue is too weak a basis for a repeated-body signal.
        digest = hashlib.sha256(text.encode()).hexdigest() if len(text) >= 80 else None
        same_body = bodies.get(digest) if digest else None
        if is_turn:
            changed = [key for key in current if current[key] != previous[key]] if current is not None and previous is not None else None
            unchanged = unchanged + 1 if changed == [] else 0
            action = _normalized(node.get('playerDirection') or node.get('selectedDirection', {}).get('title'))
            repeated = repeated + 1 if action and action == previous_action else (1 if action else 0)
            source_progress = current.get('source', {}).get('sourceProgress') if current else None
            source_rank = source_ranks.get(source_progress)
            if source_rank is not None and previous_source_rank is not None and source_rank < previous_source_rank:
                source_progress_regressed = True
            if source_progress and source_progress == previous_source_progress:
                source_progress_streak += 1
            elif source_progress:
                source_progress_streak = 1
            else:
                source_progress_streak = 0
            previous_source_progress = source_progress
            previous_source_rank = source_rank
            previous_action = action
            rows.append(dict(branch_id=node['id'], parent_branch_id=node.get('parentId'),
                             turn=len(rows) + 1, changed=changed, unchanged_state_turns=unchanged,
                             repeated_action_turns=repeated, repeated_body_from=same_body))
        else:
            unchanged = repeated = 0
            source_progress_streak = 0
            previous_source_progress = None
            previous_source_rank = None
            previous_action = ''
        if digest:
            bodies.setdefault(digest, node['id'])
        previous = current
    current = nodes[-1]['branchState']
    goals = goals_for(package, contract, current)
    goal_coverage = 'recorded' if 'goalLedger' in current else 'unknown'
    if goal_coverage == 'unknown':
        goals = [dict(g, status='unknown') for g in goals]
    threads = threads_for(package, contract, current)
    signals = []
    if unchanged >= WINDOW:
        signals.append('unchanged_tracked_state')
    if repeated >= WINDOW:
        signals.append('repeated_action')
    if source_progress_streak >= WINDOW:
        signals.append('source_progress_stalled')
    if source_progress_regressed:
        signals.append('source_progress_regressed')
    if rows and rows[-1]['repeated_body_from']:
        signals.append('repeated_body')
    return dict(version=VERSION, branch_id=nodes[-1]['id'], root_branch_id=nodes[0]['id'],
                status=route_status, turns=len(rows), recent_turns=rows[-RECENT_LIMIT:],
                coverage='unknown' if unknown else 'recorded', unknown_state_branches=unknown,
                signals=signals, review_recommended=bool(signals) and route_status == 'active',
                window=WINDOW, unchanged_state_turns=unchanged, repeated_action_turns=repeated,
                source_progress_streak=source_progress_streak,
                closure=dict(goal_coverage=goal_coverage,
                             thread_coverage='recorded' if 'threadLedger' in current else 'unknown',
                             active_goals=_pending(goals, ('active',)), unknown_goals=_pending(goals, ('unknown',)),
                             open_threads=_pending(threads, ('open',)), unknown_threads=_pending(threads, ('unknown',)),
                             ending_eligibility='unknown'))
