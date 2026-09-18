"""Script-owned local obligations; proposed work never establishes future facts."""
import hashlib
import json

from .reader_consequences import initial_goals
from . import reader_threads
from .reader_threads import initial_threads
from .character_presentation import known_status
from . import item_lifecycle

VERSION = 'route-outline/3'


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _progress_matches(condition, progress):
    if not isinstance(condition, dict) or not isinstance(progress, str):
        return False
    if condition.get('equals') == progress:
        return True
    values = condition.get('oneOf')
    return isinstance(values, list) and progress in values


def _structure(package, state):
    """Project only source-declared Beat/Arc metadata into the local outline."""
    progress = state.get('sourceProgress') if isinstance(state, dict) else None
    story = package.get('story', {}) if isinstance(package, dict) else {}
    graph = story.get('narrativeGraph', {}) if isinstance(story, dict) else {}
    beat = next((item for item in graph.get('beats', [])
                 if isinstance(item, dict)
                 and (item.get('id') == state.get('canonicalBeatId')
                      or item.get('branchState', {}).get('sourceProgress') == progress)), None)
    beat_view = None
    if beat and isinstance(progress, str):
        beat_view = beat.get('id')
    arc_view = None
    arcs = story.get('arcModel', {}).get('arcs', []) if isinstance(story.get('arcModel'), dict) else []
    for arc in arcs:
        if not isinstance(arc, dict):
            continue
        if (_progress_matches(arc.get('availableWhen', {}).get('sourceProgress'), progress)
                or (not arc.get('availableWhen')
                    and _progress_matches(arc.get('completionWhen', {}).get('sourceProgress'), progress))):
            arc_view = arc.get('id')
            break
    return dict(volume=None, arc=arc_view, beat=beat_view, source_progress=progress)


def _origin(item, kind, nodes, initial, opening_verified):
    """Resolve a ledger record to a matching change on this exact ancestry."""
    if item.get('source') == 'opening' and item.get('causeBranchId') is None:
        expected = next((x for x in initial if x['id'] == item['id']), None)
        if (opening_verified and expected and all(item.get(k) == expected.get(k) for k in ('title', 'status'))
                and reader_threads.metadata(item) == reader_threads.metadata(expected)
                and item_lifecycle.dependencies(item) == item_lifecycle.dependencies(expected)
                and (kind != 'goal' or item.get('dependencies', []) == expected.get('dependencies', []))):
            return dict(kind='opening', branch_id=nodes[0]['id'], ref=item['id'], quote=item['title'])
        return None
    origin = next((n for n in nodes if n['id'] == item.get('causeBranchId')), None)
    quote = item.get('evidence')
    if not origin or not isinstance(quote, str) or not quote.strip() or quote not in origin.get('narrativeText', ''):
        return None
    for index, update in enumerate(origin.get('consequenceUpdate', {}).get(kind + 'Updates', [])):
        parent = next((n for n in nodes if n['id'] == origin.get('parentId')), {})
        previous = next((r for r in parent.get('branchState', {}).get(kind + 'Ledger', [])
                         if r['id'] == update['id']), None)
        expected_id, title, status = update['id'], update.get('title'), update.get('status')
        new = (kind == 'goal' and expected_id == 'new') or (kind == 'thread' and expected_id.startswith('new-'))
        transformed = kind == 'goal' and status == 'transformed' and item.get('previousGoalId') == expected_id
        if new or transformed:
            expected_id = kind + '-' + hashlib.sha256((origin['id'] + ':' + str(index)).encode()).hexdigest()[:16]
        if transformed:
            title, status = update.get('successor'), 'active'
        if (item['id'] == expected_id and item.get('title') == title and item.get('status') == status
                and quote == update.get('evidence')
                and reader_threads.metadata(item, previous) == reader_threads.metadata(update, previous)
                and item_lifecycle.dependencies(item) == item_lifecycle.dependencies(update, previous)
                and (kind != 'goal' or item.get('dependencies', []) == update.get('dependencies', []))):
            return dict(kind='narrative', branch_id=origin['id'], ref=item['id'], quote=quote)
    return None


def build_outline(package, contract, nodes):
    current, root = nodes[-1], nodes[0]
    status_nodes = [dict(n, sequence=n.get('sequence', index)) for index, n in enumerate(nodes)]
    state = current['branchState']
    entry = next((e for e in package['story'].get('entryModel', {}).get('entryPoints', [])
                  if e['id'] == contract.get('entryPointId')), {})
    opening_verified = bool(entry.get('openingContext')) and root.get('openingContext') == entry['openingContext']
    binding = _digest(dict(version=VERSION, package=[package['id'], package['version'], package.get('moduleIndexSha256')],
                           contract=contract, entry=entry.get('openingThreads', []),
                           opening=entry.get('openingContext'),
                           lineage=[{key: n.get(key) for key in ('id', 'parentId', 'branchState', 'narrativeText',
                                                                'openingContext', 'consequenceUpdate')} for n in nodes]))
    result = dict(version=VERSION, branch_id=current['id'], root_branch_id=root['id'], binding_digest=binding,
                  structure=_structure(package, state),
                  goals=[], threads=[], conflicts=[], steps=[])
    initial = {'goal': initial_goals(package, contract), 'thread': initial_threads(package, contract)}
    for kind, field in (('goal', 'goalLedger'), ('thread', 'threadLedger')):
        # A fresh verified opening declares its initial obligations. Missing
        # ledgers later in the route do not establish their current status.
        records = state.get(field, initial[kind])
        present = field in state or (len(nodes) == 1 and opening_verified)
        seen = set()
        for item in records:
            if item['id'] in seen:
                raise ValueError('局部大纲账本 ID 重复')
            seen.add(item['id'])
            allowed = ('active', 'completed', 'transformed', 'abandoned') if kind == 'goal' else ('open', 'resolved', 'abandoned')
            evidence = _origin(item, kind, nodes, initial[kind], opening_verified) if present and item.get('status') in allowed else None
            status = item['status'] if evidence else 'unknown'
            target = ['completed'] if kind == 'goal' else ['resolved', 'abandoned']
            completion = dict(ledger=field, target_id=item['id'], target_statuses=target,
                              met=status in target if evidence else None, requires_narrative_evidence=True)
            record = dict(id=item['id'], title=item['title'], status=status, evidence=evidence,
                          priority=reader_threads.metadata(item)['priority'],
                          recoveryWindow=reader_threads.metadata(item)['recoveryWindow'],
                          completion=completion)
            result[kind + 's'].append(record)
            if status not in ('active', 'open', 'unknown'):
                continue
            blockers = []
            if kind == 'goal' and status == 'active':
                for cid in item.get('dependencies', []):
                    outcome = known_status(status_nodes, cid)
                    if outcome['code'] in ('dead', 'departed', 'missing'):
                        conflict = dict(id='dependency:' + item['id'] + ':' + cid, goal_id=item['id'],
                                        character_id=cid, status=outcome['code'],
                                        evidence=dict(kind='narrative', ref=cid,
                                                      branch_id=outcome['evidence']['branch_id'], quote=outcome['evidence']['quote']))
                        result['conflicts'].append(conflict)
                        blockers.append(conflict['id'])
            if status in ('active', 'open'):
                for iid in item_lifecycle.dependencies(item):
                    if item_lifecycle.destroyed(state, iid):
                        proof = item_lifecycle.destruction_evidence(nodes, iid)
                        conflict = dict(id='item-dependency:' + kind + ':' + item['id'] + ':' + iid,
                                        target_kind=kind, target_id=item['id'], item_id=iid,
                                        status='destroyed' if proof else 'unknown', evidence=proof)
                        result['conflicts'].append(conflict)
                        blockers.append(conflict['id'])
            task = 'verify_status' if status == 'unknown' else ('review_dependency' if blockers else ('pursue_goal' if kind == 'goal' else 'address_thread'))
            result['steps'].append(dict(id=kind + ':' + item['id'], kind=task, target_id=item['id'],
                                        title=item['title'], blockers=blockers, evidence=evidence,
                                        priority=reader_threads.metadata(item)['priority'],
                                        recoveryWindow=reader_threads.metadata(item)['recoveryWindow'],
                                        completion=completion))
    return result


def outline_view(package, contract, nodes, status):
    # Recompute before using persisted data. A stale or injected outline cannot
    # revive a dropped goal, borrow a sibling's evidence or claim completion.
    outline = build_outline(package, contract, nodes)
    storage = 'saved' if nodes[-1].get('routeOutline') == outline else 'reconstructed'
    return dict(outline=outline, storage=storage, status=status, actionable=status == 'active')
