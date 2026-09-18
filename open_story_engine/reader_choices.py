"""Public, evidence-bound action suggestions. They never contain state patches."""
import copy
import hashlib
import json
import re
from difflib import SequenceMatcher

from .api_journey import character_card
from .llm import LlmError, parse_json_content
from .prompts import render_prompt, catalog_version
from .reader_consequences import goals_for
from .reader_threads import threads_for
from .item_lifecycle import ATTRIBUTE

MAX_CHOICES = 4
DEPENDENCY_VERSION = 'reader-choice-dependencies/3'


def context_digest(context):
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def interaction_availability(state, character_id, locations):
    """A public name is not contact evidence; use committed spatial state."""
    outcome = state.get('characterOutcomeStates', {}).get(character_id, {})
    if outcome.get('status') in ('dead', 'departed', 'missing'):
        return {'available': False, 'interactionBasis': 'unavailable_outcome'}
    place = state.get('playerLocationId')
    if (isinstance(place, str) and place in locations
            and state.get('characterLocationIds', {}).get(character_id) == place):
        return {'available': True, 'interactionBasis': 'same_location'}
    # Unknown or different locations do not establish death, absence or a
    # public destination. Do not expose another character's hidden location.
    return {'available': False, 'interactionBasis': 'contact_unconfirmed'}


def choice_context(package, contract, history, node):
    current = dict(node, sequence=history[-1]['sequence'] + 1)
    state = current['branchState']
    player = contract['persona']['name']
    characters = list(package['characters']) + state.get('derivedCharacters', [])
    known = [c for c in characters if character_card([*history, current], c, player)]
    paragraphs = {f'P{i+1}': p for i, p in enumerate(node['narrativeText'].split('\n\n'))}
    player_id = contract['persona'].get('sourceCharacterId')
    public_items = set()
    model = package.get('story', {}).get('entryModel', {})
    # The reviewed official opening explicitly places public props on the
    # scene floor. Knowledge survives later removal; current state stays below.
    opening = next((e for e in model.get('entryPoints', []) if e['id'] == contract.get('entryPointId')), {})
    if (model.get('policy') == 'official_unknown_reader/1'
            and player_id in opening.get('sourceCharacterIds', [])
            and any(e.get('kind') == 'source_entry' and
                    e.get('entryChapter', {}).get('entryPointId') == opening.get('id') for e in history)):
        initial = opening.get('openingState', {})
        public_items.update(iid for iid, place in initial.get('itemLocationIds', {}).items()
                            if place is not None and place == initial.get('playerLocationId'))
    for entry in [*history, current]:
        public_items.update(iid for iid, owner in entry['branchState'].get('itemOwnerCharacterIds', {}).items()
                            if player_id is not None and owner == player_id)
        update = entry.get('consequenceUpdate', {})
        # A source prop's name or absence alone does not establish player knowledge.
        for change in update.get('stateChanges', []):
            if change.get('evidence') and change['evidence'] in entry.get('narrativeText', ''):
                public_items.add(change['entityId'])
        for item in update.get('introductions', {}).get('items', []):
            if item.get('evidence') and item['evidence'] in entry.get('narrativeText', ''):
                public_items.add(item['id'])
    items = list(package.get('items', [])) + state.get('derivedItems', [])
    locations = {p['id'] for p in list(package.get('locations', [])) + state.get('derivedLocations', [])}
    return {'player': player, 'playerLocationId': state.get('playerLocationId', 'unknown'), 'paragraphs': paragraphs,
            'people': [{'id': c['id'], 'name': c['name'],
                        'status': state.get('characterOutcomeStates', {}).get(c['id'], {}).get('status', 'unknown'),
                        'permanence': state.get('characterOutcomeStates', {}).get(c['id'], {}).get('permanence', 'unknown'),
                        **interaction_availability(state, c['id'], locations)}
                       for c in known],
            'items': [{'id': i['id'], 'name': i['name'],
                       'state': copy.deepcopy(state.get('readerEntityStates', {}).get(i['id'], {})),
                       'ownerCharacterId': state.get('itemOwnerCharacterIds', {}).get(i['id'], 'unknown'),
                       'locationId': state.get('itemLocationIds', {}).get(i['id'], 'unknown')}
                      for i in items if i['id'] in public_items],
            'threads': [{**{k: t[k] for k in ('id', 'title', 'status')},
                         **{k: copy.deepcopy(t[k]) for k in ('itemDependencies',) if k in t}}
                        for t in threads_for(package, contract, state)],
            'goals': [{'id': g['id'], 'title': g['title'], 'status': g['status'],
                       **{k: copy.deepcopy(g[k]) for k in ('itemDependencies',) if k in g},
                       'dependencies': g.get('dependencies', [])} for g in goals_for(package, contract, state)]}


def validate_choices(data, context, package):
    options = data.get('choices') if isinstance(data, dict) else None
    if not isinstance(options, list) or not 1 <= len(options) <= MAX_CHOICES:
        raise ValueError('当前剧情须提供 1–4 个有区别的行动建议')
    known = {p['id']: p for p in context['people']}
    items = {item['id']: item for item in context.get('items', [])}
    hidden_names = [c['name'] for c in package['characters'] if c['id'] not in known and len(c['name']) > 1]
    result, actions = [], []
    for option in options:
        if not isinstance(option, dict):
            continue
        title, action = option.get('title'), option.get('action')
        refs, interactions = option.get('paragraphIds'), option.get('interactWith')
        mentions = option.get('mentionOnly', [])
        uses = option.get('useItems', [])
        if (not isinstance(title, str) or not 2 <= len(title.strip()) <= 28
                or not isinstance(action, str) or not 8 <= len(action.strip()) <= 180
                or not isinstance(refs, list) or not refs or any(not isinstance(r, str) or r not in context['paragraphs'] for r in refs)
                or not isinstance(interactions, list)
                or not isinstance(mentions, list)
                or not isinstance(uses, list)
                or any(not isinstance(iid, str) or iid not in items or items[iid].get('state', {}).get(ATTRIBUTE) is True for iid in uses)
                or any(not isinstance(cid, str) or cid not in known for cid in mentions)
                or any(not isinstance(cid, str) or cid not in known or known[cid].get('available') is not True for cid in interactions)):
            continue
        if set(interactions) & set(mentions):
            continue
        named = {cid for cid, person in known.items() if person['name'] in title + action}
        if not named <= set(interactions) | set(mentions):
            continue
        if title.strip() in ('继续当前目标', '继续故事', '继续推进') or any(n in title + action for n in hidden_names):
            continue
        normalized = re.sub(r'\W+', '', action)
        if any(title.strip() == c['title'] or SequenceMatcher(None, normalized, previous).ratio() > .85
               for c, previous in zip(result, actions)):
            continue
        result.append({'id': 'reader-' + hashlib.sha256(action.strip().encode()).hexdigest()[:16],
                       'title': title.strip(), 'summary': action.strip(),
                       'evidence': [context['paragraphs'][r] for r in dict.fromkeys(refs)],
                       'dependencies': {'version': DEPENDENCY_VERSION, 'contextDigest': context_digest(context),
                                        'paragraphIds': list(dict.fromkeys(refs)),
                                        'interactWith': list(dict.fromkeys(interactions)),
                                        'mentionOnly': list(dict.fromkeys(mentions))}})
        if uses:
            result[-1]['dependencies']['useItems'] = list(dict.fromkeys(uses))
        actions.append(normalized)
    if not result:
        raise ValueError('没有通过公开依据与角色可用性检查的方向')
    return result


def restored_choices(choices, context, package):
    """Recheck stored suggestions without rewriting history or calling a model."""
    if not isinstance(choices, list):
        return []
    result = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        dependencies = choice.get('dependencies')
        if (not isinstance(dependencies, dict) or dependencies.get('version') != DEPENDENCY_VERSION
                or dependencies.get('contextDigest') != context_digest(context)):
            continue
        option = {'title': choice.get('title'), 'action': choice.get('summary'),
                  'paragraphIds': dependencies.get('paragraphIds'),
                  'interactWith': dependencies.get('interactWith'), 'mentionOnly': dependencies.get('mentionOnly'),
                  'useItems': dependencies.get('useItems', [])}
        try:
            checked = validate_choices({'choices': [option]}, context, package)[0]
        except ValueError:
            continue
        if checked == choice:
            result.append(checked)
    return result[:MAX_CHOICES]


def generate_choices(gateway, package, contract, history, node):
    context = choice_context(package, contract, history, node)
    audit = {'operation': 'reader_choices', 'model': gateway.model, 'promptVersion': catalog_version(),
             'requestSummary': '为已确认正文整理下一步行动建议', 'callObservations': []}
    try:
        completion = gateway.complete_json([
            {'role': 'system', 'content': render_prompt('reader.choices')},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False)},
        ])
        audit['rawResponse'] = completion.raw_response
        audit['callObservations'] = [{**o, 'generationStage': 'reader_choices'} for o in completion.observations]
        return validate_choices(parse_json_content(completion.content), context, package), audit
    except (LlmError, ValueError) as error:
        # Optional menu failure must not throw away checked, readable prose.
        audit['error'] = str(error)
        audit['callObservations'].extend(getattr(error, 'observations', []))
        return None, audit
