"""Small identity lookups from visible package facts and this branch only."""
import re


def observation_state(state, entity_ids):
    """Use the selected entities' current facts, not every historical attribute."""
    result = {'playerLocationId': state.get('playerLocationId')}
    for key in ('characterLocationIds', 'itemLocationIds', 'itemOwnerCharacterIds',
                'characterOutcomeStates', 'readerEntityStates'):
        result[key] = {eid: value for eid, value in state.get(key, {}).items() if eid in entity_ids}
    return result


def turn_entity_facts(visible_entities, state, query, recent='', limit=8):
    """Similar names retrieve candidates; they never establish identity."""
    records = {}
    for group, derived, kind in (('characters', 'derivedCharacters', 'character'),
                                  ('items', 'derivedItems', 'item'),
                                  ('locations', 'derivedLocations', 'location')):
        for source, entries in (('visible_package', visible_entities.get(group, [])),
                                ('current_branch', state.get(derived, []))):
            for entry in entries:
                records[entry['id']] = dict(id=entry['id'], name=entry['name'], kind=kind,
                    source=source, detail=str(entry.get('detail') or entry.get('summary') or '')[:160])
    text = query + '\n' + recent
    mentions = list(dict.fromkeys(re.findall(r'[“「\"]([\u3400-\u4dbf\u4e00-\u9fff]{2,16})[”」\"]', text)))
    location = state.get('playerLocationId')
    selected = []
    for record in records.values():
        name = record['name']
        near = [m for m in mentions if m != name and (m in name or name in m)]
        # A shortened name need not be in quotation marks. Match an actual
        # shortened occurrence, not the prefix inside the full formal name.
        if record['kind'] == 'character' and re.fullmatch(r'[\u3400-\u4dbf\u4e00-\u9fff]{3,5}', name):
            near += [name[:n] for n in range(2, len(name))
                     if re.search(re.escape(name[:n]) + '(?!' + re.escape(name[n:]) + ')', text)]
        near = list(dict.fromkeys(near))
        score = (8 if name in query else 0) + (4 if name in recent else 0) + (2 if near else 0)
        if location and state.get('characterLocationIds', {}).get(record['id']) == location:
            score += 1
        if score or record['id'] == location:
            selected.append((score, record, near))
    selected.sort(key=lambda item: -item[0])
    cards = []
    for _, record, near in selected[:max(0, limit)]:
        card = dict(record)
        if near:
            card['unconfirmedMentions'] = near
        cards.append(card)
    current = records.get(location)
    return dict(entities=cards,
        lastRecordedPlayerLocation={k: current[k] for k in ('id', 'name')} if current else None,
        identityRule='名称相似只是候选，不是同一身份；无已确认别名关系时不得合并。',
        locationRule='登记位置可能漏记。当前正文实际场所与登记名不同且无已确认别名时，独立登记；不把新场所绑定旧ID。')


def check_observed_reference(item, known, evidence, new_location_ids=()):
    """Validate explicit reference labels, without judging or changing prose."""
    entity_id = item.get('entityId') or item.get('characterId')
    if 'entityName' in item:
        entity = known.get(entity_id) if isinstance(entity_id, str) else None
        if not entity or item['entityName'] != entity['name']:
            raise ValueError('实体称呼与登记身份不一致，不能用近似姓名绑定同一ID')
    if item.get('attribute') == 'locationId' and 'observedLocationName' in item:
        mention = item['observedLocationName']
        if not isinstance(mention, str) or not mention or mention not in evidence:
            raise ValueError('实际地点称呼必须直接引用本次段落')
        target = known.get(item.get('value')) if isinstance(item.get('value'), str) else None
        if not target or (mention != target['name'] and item.get('value') not in new_location_ids):
            raise ValueError('实际地点称呼未绑定该地点ID；保留未知或登记独立地点')
