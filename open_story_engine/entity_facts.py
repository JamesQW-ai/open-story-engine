"""Small identity lookups from visible package facts and this branch only."""
import re
import copy


def bind_player_mentions(data, body, player_id, player_name):
    """The session contract, not name similarity, identifies narrative 'you'."""
    if not isinstance(data, dict):
        return data
    result = copy.deepcopy(data)
    groups = [result]
    if isinstance(result.get('updates'), dict):
        groups.append(result['updates'])
    scene = result.get('currentScene')
    if isinstance(scene, dict):
        groups.append(scene)
    paragraphs = body.split('\n\n')
    for group in groups:
        for key in ('stateChanges', 'confirmedStates', 'outcomes', 'presentEntities'):
            entries = group.get(key, [])
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict) or (item.get('entityId') or item.get('characterId')) != player_id or item.get('entityName') != player_name:
                    continue
                quote = item.get('evidenceQuote')
                if quote is None:
                    refs = item.get('paragraphIds', [])
                    quote = '\n\n'.join(paragraphs[int(ref[1:]) - 1] for ref in refs
                        if isinstance(ref, str) and len(ref) <= 8 and re.fullmatch(r'P[1-9]\d*', ref)
                        and int(ref[1:]) <= len(paragraphs)) if isinstance(refs, list) else ''
                if isinstance(quote, str) and '你' in quote:
                    item['entityName'] = '你'
    return result


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


def observation_evidence(item, evidence, *, local_attribution=True):
    """Bind an optional short extract to the supplied paragraph, not a claim."""
    quote = item.get('evidenceQuote', evidence)
    if not isinstance(quote, str) or not quote.strip() or quote not in evidence:
        raise ValueError('状态依据必须逐字引用所选正文段落')
    if quote != evidence:
        # A literal substring is not independent evidence when cut out of a
        # spoken report. Keep its source sentence/quotation attribution.
        positions = [m.start() for m in re.finditer(re.escape(quote), evidence)]
        if len(positions) != 1:
            raise ValueError('短引文在所选段落中不唯一，需缩小段号范围')
        start = positions[0]
        end = start + len(quote)
        if any(m.start() < start < m.end() or m.start() < end < m.end()
               for m in re.finditer(r'“[^”]*”|「[^」]*」|"[^"]*"', evidence)):
            raise ValueError('不能截去引号或说话者，将引述冒充直接观察')
        left = max((m.end() for m in re.finditer(r'[。！？；\n]', evidence[:start])), default=0)
        following = re.search(r'[。！？；\n]', evidence[end:])
        right = end if local_attribution else (
            end + following.end() if following and quote[-1] not in '。！？；\n' else end)
        check_direct_evidence(evidence[left:right])
    return quote


def scene_evidence(item, evidence):
    """Use the named scene's own sentence; unrelated dialogue is not its source."""
    if 'evidenceQuote' in item:
        return observation_evidence(item, evidence)
    name = item.get('name')
    if not isinstance(name, str) or not name:
        return evidence
    for match in reversed(list(re.finditer(r'[^。！？；\n]+[。！？；\n]?', evidence))):
        quote = match.group()
        if name not in quote:
            continue
        try:
            quote = observation_evidence({'evidenceQuote': quote}, evidence)
            check_direct_evidence(quote)
            return quote
        except ValueError:
            continue
    return evidence


def check_direct_evidence(evidence):
    # This only limits writes to authoritative state. The story is always kept.
    # Mixed paragraphs can provide a shorter literal evidenceQuote instead.
    if re.search(r'听说|听闻|传闻|据说|传话|自称|声称|推测|猜测|'
                 r'(?:说|道|问|答|喊)[：:，,]?[“「\"]|'
                 r'尚未核实|未证实|身份.{0,8}(?:未明|不确定|待核|未核)|'
                 r'(?:并未|没有|未曾|不曾|尚未)(?:亲眼|确认|核实)|'
                 r'如果|假如|打算|准备去', evidence):
        raise ValueError('状态依据含转述、设想或未核实信息，保留待澄清')


def check_observed_reference(item, known, evidence, new_location_ids=(), *, strict=False, player_id=None):
    """Validate explicit reference labels, without judging or changing prose."""
    entity_id = item.get('entityId') or item.get('characterId')
    if strict:
        if item.get('basis') != 'observed' or 'entityName' not in item:
            raise ValueError('状态记录缺少明确观察依据或实体称呼，保留待澄清')
        entity = known.get(entity_id) if isinstance(entity_id, str) else None
        name = item['entityName']
        player_reference = entity_id == player_id and name == '你'
        if not entity or not isinstance(name, str) or not name or (
                name != entity['name'] and not player_reference) or name not in evidence:
            raise ValueError('实际称呼没有绑定登记身份，不能补全近似姓名')
        check_direct_evidence(evidence)
    if 'entityName' in item:
        entity = known.get(entity_id) if isinstance(entity_id, str) else None
        if not entity or (item['entityName'] != entity['name'] and not (
                strict and entity_id == player_id and item['entityName'] == '你')):
            raise ValueError('实体称呼与登记身份不一致，不能用近似姓名绑定同一ID')
    if strict and item.get('attribute') == 'locationId' and 'observedLocationName' not in item:
        raise ValueError('位置记录缺少实际地点称呼，保留待澄清')
    if item.get('attribute') == 'locationId' and 'observedLocationName' in item:
        mention = item['observedLocationName']
        if not isinstance(mention, str) or not mention or mention not in evidence:
            raise ValueError('实际地点称呼必须直接引用本次段落')
        target = known.get(item.get('value')) if isinstance(item.get('value'), str) else None
        if not target or (mention != target['name'] and item.get('value') not in new_location_ids):
            raise ValueError('实际地点称呼未绑定该地点ID；保留未知或登记独立地点')
