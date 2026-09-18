"""Public character metadata from one already-read lineage; never infer outcomes."""
import hashlib
import re


STATUS_LABELS = {'unknown': '状态未确认', 'alive': '存活', 'dead': '已死亡',
                 'departed': '已离队', 'missing': '失踪', 'injured': '受伤'}


def evidence(node, quote, source):
    return {'branch_id': node['id'], 'page': node['sequence'] + 1,
            'quote': quote, 'source': source}


def known_opening_name(node, name):
    return (name in node.get('narrativeText', '') and
            any(relation.get('name') == name for relation in (node.get('openingContext') or {}).get('relationships', [])))


def portrait(character, package_id):
    # Only a published, same-origin asset reference; no history or prompt is sent.
    asset = character.get('portraitAsset')
    if not isinstance(asset, str) or not re.fullmatch(r'/images/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)', asset):
        asset = None
    seed = hashlib.sha256((package_id + ':' + character['id']).encode()).digest()[0] % 8
    return {'url': asset, 'source': 'published' if asset else 'preset',
            'fallback_key': 'person-' + str(seed + 1)}


def known_status(nodes, character_id):
    status = {'code': 'unknown', 'label': STATUS_LABELS['unknown'],
              'permanence': None, 'evidence': None}
    if not nodes:
        return status
    record = nodes[-1].get('branchState', {}).get('characterOutcomeStates', {}).get(character_id)
    if not isinstance(record, dict) or record.get('status') not in STATUS_LABELS or record.get('status') == 'unknown':
        # Absence or a biography cannot create a public consequence.
        return status
    origin = next((n for n in nodes if n['id'] == record.get('causeBranchId')), None)
    quote = record.get('evidence')
    if (origin is None or not isinstance(quote, str) or not quote.strip()
            or quote not in origin.get('narrativeText', '')
            or record.get('permanence') not in ('temporary', 'permanent')):
        return status
    # A state value alone may be internal knowledge. Display only a recorded
    # consequence with the same public passage on this lineage.
    matching = any(isinstance(change, dict) and change.get('characterId') == character_id
                   and all(change.get(key) == record.get(key) for key in ('status', 'permanence', 'evidence'))
                   for change in origin.get('consequenceUpdate', {}).get('outcomes', []))
    if not matching:
        return status
    return {'code': record['status'], 'label': STATUS_LABELS[record['status']],
            'permanence': record['permanence'], 'evidence': evidence(origin, quote, 'consequence')}


def character_presentation(nodes, character, card, package_id, appears):
    """Enrich an admitted character card, without admitting unseen catalog people."""
    first = next(n for n in nodes if n['sequence'] + 1 == card['first_page'])
    status = known_status(nodes, character['id'])
    public_outcome_here = status['evidence'] and status['evidence']['branch_id'] == first['id']
    quote = next((p for p in first.get('narrativeText', '').split('\n\n')
                  if appears(p, character['name'], card['is_player']) or
                  (character['name'] in p and (known_opening_name(first, character['name']) or public_outcome_here))), '')
    return {**card,
            # Another playable role's menu can reveal knowledge this player lacks.
            'identity': card['identity'] if card['is_player'] else '身份随剧情逐渐明确',
            'name_evidence': evidence(first, quote, 'narrative') if quote else None,
            'portrait': portrait(character, package_id),
            'status': status}
