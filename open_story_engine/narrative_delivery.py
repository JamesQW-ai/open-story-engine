"""Save prose once; observed state and unresolved continuity travel with it.

No comparison to the desired scene plan grants or denies prose delivery.
Unusable observations remain unresolved rather than becoming invented facts.
"""
import copy
import hashlib
import json
import re

from . import reader_actions as actions, reader_consequences as consequences, reader_threads

VERSION = 'narrative-delivery/5'
LEGACY_VERSION = 'narrative-delivery/1'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _continuity_terms(text):
    from .context_bundle import CONTEXT_FACT_STOPWORDS, context_fact_terms
    terms = context_fact_terms(text)
    if isinstance(text, str):
        terms.update(term for term in re.findall(r'(?=([\u3400-\u4dbf\u4e00-\u9fff]{2}))', text)
                     if term not in CONTEXT_FACT_STOPWORDS)
    return terms


def _context_issue(issue):
    result = {k: copy.deepcopy(issue[k]) for k in
              ('id', 'kind', 'status', 'entityIds', 'originParentId') if k in issue}
    # The saved question remains intact. An answered question's old "unknown"
    # evidence must not compete with its answer in the current prompt.
    fields = (('resolution', 400), ('resolutionEvidence', 240)) if issue.get('status') == 'resolved' else (
        ('summary', 400), ('evidence', 240))
    for key, budget in fields:
        if isinstance(issue.get(key), str):
            result[key] = issue[key][:budget]
            if len(issue[key]) > budget:
                result[key + 'Truncated'] = True
    return result


def pending(node, query='', limit=4):
    items = [copy.deepcopy(i) for i in node.get('continuityFollowups', []) if i.get('status') == 'open']
    # Persist every issue; inject only a relevant bounded subset. Unselected
    # issues survive on the branch and can be retrieved on later turns.
    terms = _continuity_terms(query)
    items.reverse()
    if terms:
        items = [i for i in items if terms & _continuity_terms(i['summary'])]
    items.sort(key=lambda i: (-len(terms & _continuity_terms(i['summary'])),
                             i.get('kind') == 'state_uncertain' and not i.get('entityIds')))
    return [_context_issue(i) for i in items[:max(0, limit)]]


def continuity_context(node, query='', limit=4):
    """Retrieve an answer by either the old question or newly learned details."""
    terms = _continuity_terms(query)
    limit = max(0, limit)
    def relevance(issue):
        return len(terms & _continuity_terms(' '.join(
            issue.get(key, '') for key in ('summary', 'resolution') if isinstance(issue.get(key), str))))
    answered = [copy.deepcopy(i) for i in reversed(node.get('continuityFollowups', []))
                if i.get('status') == 'resolved' and relevance(i)]
    answered.sort(key=lambda i: -relevance(i))
    answered = [_context_issue(i) for i in answered[:min(2, max(0, limit - 2))]]
    return dict(pendingFollowups=pending(node, query, limit=limit - len(answered)),
                resolvedFollowups=answered)


def _quote(item, body):
    paragraphs = body.split('\n\n')
    refs = item.get('paragraphIds', [])
    if not isinstance(refs, list) or not refs:
        raise ValueError('实际记录缺少正文段号')
    positions = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith('P') or not ref[1:].isdigit():
            raise ValueError('正文段号格式无效')
        index = int(ref[1:]) - 1
        if not 0 <= index < len(paragraphs):
            raise ValueError('正文段号不存在')
        positions.append(index)
    return '\n\n'.join(paragraphs[min(positions):max(positions) + 1])


def _text(value, maximum=180):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= maximum:
        raise ValueError('记录文字缺失或过长')
    return value.strip()


def observe(context, body, data, failure=None, *, legacy=False, literal_scope=True, narrated_location=True,
            separate_identity=True):
    """Project usable observations; report uncertain items without vetoing prose."""
    state, package = context['parent']['branchState'], context['package']
    action = context['playerDirection']
    player = context['contract']['persona']['sourceCharacterId']
    update = dict(decision='ready', message='', requirements={'A1': dict(mode='attempt', summary=action)},
                  method='只登记正文已发生的结果',
                  steps=[dict(id='S1', actorId=player, action=action, requirementIds=['A1'],
                              authority='player', causeStepId=None, usedItemIds=[])],
                  introductions={k: [] for k in ('characters', 'items', 'locations')},
                  stateChanges=[], outcomes=[], goalUpdates=[], threadUpdates=[])
    issues = copy.deepcopy(context['parent'].get('continuityFollowups', []))
    # Keep a bounded cache of recent answers on the child. Older evidence is
    # still available on immutable ancestors; only selected items enter prompts.
    recent_resolved = [i for i in issues if i.get('status') == 'resolved'][-12:]
    issues = [i for i in issues if i.get('status') == 'open'] + recent_resolved
    diagnostics = []

    def unresolved(summary, kind='state_uncertain', evidence='', entities=()):
        summary = summary[:400]
        identifier = 'followup-' + digest([kind, summary, list(entities)])[:20]
        if not any(i['id'] == identifier for i in issues):
            issues.append(dict(id=identifier, kind=kind, status='open', summary=summary,
                               evidence=evidence, entityIds=list(entities),
                               originParentId=context['parent']['id'], originNarrativeSha256=digest(body)))

    def deferred(item, error):
        diagnostics.append(dict(item=copy.deepcopy(item), reason=str(error)))
        entity = item.get('entityId') or item.get('characterId') or item.get('id') if isinstance(item, dict) else None
        unresolved('本回合有尚未确认的持续结果，需要结合原文澄清。' +
                   (str(item.get('reason') or item.get('summary') or entity or '')[:160] if isinstance(item, dict) else ''),
                   evidence=body[-600:], entities=[entity] if isinstance(entity, str) else [])

    if not isinstance(data, dict):
        data = {}
        failure = failure or '状态记录未返回对象'
    if failure:
        diagnostics.append(dict(reason=failure))
        unresolved('上次行动的实际结果尚未完整登记，先承接原文并确认影响后续的变化：' + action[:180],
                   evidence=body[-600:])
    raw = data.get('updates', {})
    if not isinstance(raw, dict):
        raw = {}
        deferred({}, 'updates格式无效')
    # Some responses place individual observation arrays beside updates.
    # Preserve an explicitly supplied nested value (including []); only fill
    # absent keys, and run every recovered entry through the same validation.
    raw = {**raw}
    for key in ('introductions', 'stateChanges', 'outcomes', 'goalUpdates', 'threadUpdates'):
        if key not in raw and key in data:
            raw[key] = data[key]

    def entries(container, key, limit=32):
        value = container.get(key, [])
        if not isinstance(value, list):
            deferred({}, key + '格式无效')
            return []
        if len(value) > limit:
            deferred({}, key + '超出单回合登记容量')
        return value[:limit]

    additions = raw.get('introductions', {})
    scene_confirmations = []
    if isinstance(additions, list):
        # Providers also emit the documented prefixed entities as a flat list.
        # Normalize representation only; unknown kinds stay unresolved.
        grouped = {key: [] for key in update['introductions']}
        for item in additions[:36]:
            identifier = item.get('id') if isinstance(item, dict) else None
            group = next((group for prefix, group in (('character_', 'characters'), ('item_', 'items'),
                          ('location_', 'locations')) if isinstance(identifier, str) and identifier.startswith(prefix)), None)
            if group is None:
                deferred(item, '新增实体缺少可识别类型前缀')
            else:
                grouped[group].append(item)
        if len(additions) > 36:
            deferred({}, '新增实体超出单回合登记容量')
        additions = grouped
    if not isinstance(additions, dict):
        additions = {}
        deferred({}, 'introductions格式无效')
    from .cocreation import validate_branch_additions
    for group in update['introductions']:
        for item in entries(additions, group, 12):
            try:
                evidence = _quote(item, body)
                entity = {k: _text(item.get(k), 400 if k == 'summary' else 100) for k in ('id', 'name', 'summary')}
                known = actions.registry(package, state, update['introductions'])
                if entity['id'] in known:
                    if entity['name'] != known[entity['id']]['name']:
                        raise ValueError('已有ID不能改名或指向另一个实体')
                    continue
                candidate = copy.deepcopy(update['introductions'])
                candidate[group].append({**entity, 'sourceStepId': 'S1', 'evidence': evidence})
                validate_branch_additions(package, state, candidate)
                update['introductions'] = candidate
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                deferred(item, error)
    known = actions.registry(package, state, update['introductions'])

    scene = data.get('currentScene')
    if scene is not None:
        try:
            evidence = _quote(scene, body)
            if not legacy:
                from .entity_facts import observation_evidence, scene_evidence, check_direct_evidence
                if scene.get('basis') != 'observed':
                    raise ValueError('当前场所缺少直接观察依据，不能用传闻登记位置')
                evidence = scene_evidence(scene, evidence) if literal_scope else observation_evidence(
                    scene, evidence, local_attribution=False)
                check_direct_evidence(evidence)
            name = _text(scene.get('name'), 100)
            observed_name = name
            if name not in evidence:
                # A newly introduced room may have a descriptive display name.
                # Its literal mention must still be linked in this observation;
                # this exception never aliases a previously registered place.
                fresh = {e['id'] for e in update['introductions']['locations'] if e['name'] == name}
                mentions = [i.get('observedLocationName') for i in entries(raw, 'stateChanges') + entries(data, 'confirmedStates', 12)
                            if isinstance(i, dict) and i.get('attribute') == 'locationId'
                            and isinstance(i.get('value'), str)
                            and (i['value'] in fresh or i['value'] not in known and i.get('locationName') == name)
                            and isinstance(i.get('observedLocationName'), str)
                            and i['observedLocationName'] and i['observedLocationName'] in evidence]
                if not mentions:
                    raise ValueError('当前场所名称不在引用原文中')
                observed_name = mentions[0]
                if not fresh:
                    # The model proposed an unregistered ID. Register the
                    # actual literal place, never the guessed ID/display label.
                    name = observed_name
            if not legacy and narrated_location:
                from .entity_facts import check_narrated_location
                check_narrated_location(evidence, observed_name)
            people = scene.get('presentEntityIds') if legacy else scene.get('presentEntities')
            if not legacy:
                if not isinstance(people, list) or not people or len(people) > 12:
                    raise ValueError('当前场所缺少逐人在场依据')
                verified = []
                from .entity_facts import check_observed_reference
                for person in people:
                    try:
                        quote = observation_evidence(person, _quote(person, body), local_attribution=literal_scope)
                        check_observed_reference(person, known, quote, strict=True, player_id=player)
                        if re.search(r'不在|并未到|尚未到|没有到|离开|离去|走出|告辞', quote):
                            raise ValueError('人物未到或已离场，不能登记为仍在场')
                        if known[person['entityId']]['kind'] != 'character':
                            raise ValueError('在场实体不是人物')
                        verified.append(person)
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        deferred(person, error)
                people = [p['entityId'] for p in verified]
            if not isinstance(people, list) or not people or len(people) > 12 or any(
                    not isinstance(p, str) or known.get(p, {}).get('kind') != 'character' for p in people):
                raise ValueError('当前场所在场人物引用无效')
            matches = [e for e in known.values() if e['kind'] == 'location' and e['name'] == name]
            if len(matches) > 1:
                raise ValueError('同名地点有多个，不能推断对应关系')
            if matches:
                location_id = matches[0]['id']
            else:
                location_id = 'location_observed_' + digest(name)[:16]
                candidate = copy.deepcopy(update['introductions'])
                candidate['locations'].append(dict(id=location_id, name=name,
                    summary='本回合原文确认的场所：' + name, sourceStepId='S1', evidence=evidence))
                validate_branch_additions(package, state, candidate)
                update['introductions'] = candidate
                known = actions.registry(package, state, candidate)
            scene_confirmations = [dict(entityId=p, attribute='locationId', value=location_id,
                locationName=name, observedLocationName=observed_name, basis='observed',
                reason='本回合当前场所原文确认', paragraphIds=scene['paragraphIds']) for p in dict.fromkeys(people)]
            if not legacy:
                # Keep each person's literal evidence. A list of IDs cannot
                # turn an off-page or merely mentioned person into a witness.
                for confirmation, person in zip(scene_confirmations, {p['entityId']: p for p in verified}.values()):
                    confirmation.update(entityName=person['entityName'],
                        paragraphIds=person['paragraphIds'],
                        evidenceQuote=observation_evidence(person, _quote(person, body), local_attribution=literal_scope))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            deferred(scene, error)

    def observed_quote(item):
        evidence = _quote(item, body)
        if separate_identity and 'evidenceParagraphId' in item:
            ref = item['evidenceParagraphId']
            paragraphs = body.split('\n\n')
            if (not isinstance(ref, str) or not re.fullmatch(r'P[1-9]\d{0,6}', ref)
                    or int(ref[1:]) > len(paragraphs) or ref not in item['paragraphIds']
                    or item.get('evidenceQuote') != paragraphs[int(ref[1:]) - 1]):
                raise ValueError('结果段号未绑定原文，保留待澄清')
        from .entity_facts import check_observed_reference, observation_evidence
        if not legacy:
            evidence = observation_evidence(item, evidence, local_attribution=literal_scope)
        else:
            check_observed_reference(item, known, evidence,
                {e['id'] for e in update['introductions']['locations']})
        # Route explicitly attributed accounts to continuity memory, not world
        # state. This never edits or rejects prose; omitted basis stays compatible
        # with previously saved observation receipts.
        basis = item.get('basis', 'observed' if legacy else None)
        if basis in ('reported', 'inferred'):
            reason = _text(item.get('reason') or item.get('cause'), 400)
            entity = item.get('entityId') or item.get('characterId')
            if not isinstance(entity, str) or entity not in known:
                raise ValueError('未核实说法引用的实体未登记')
            label = '人物说法尚未核实：' if basis == 'reported' else '推测尚未核实：'
            unresolved(label + reason, 'reported_state', evidence, [entity])
            return None
        if basis != 'observed':
            raise ValueError('状态依据类型无效')
        # Scene location was independently grounded above; individual presence
        # can be stated in a later paragraph without repeating the room name.
        reference = item
        if not legacy and any(item is c for c in scene_confirmations):
            reference = {k: v for k, v in item.items() if k != 'attribute'}
        if not legacy:
            # A newly introduced person's label and a later pronoun outcome
            # have separate sources. The explicit ID joins them; the outcome
            # paragraph still needs direct, literal evidence of its own.
            identity_evidence = ''
            if separate_identity and 'evidenceParagraphId' in item:
                from .entity_facts import check_direct_evidence
                eid = item.get('characterId') or item.get('entityId')
                name = item.get('entityName')
                introduced = [e for group in update['introductions'].values() for e in group]
                matches = [e for e in introduced if e['id'] == eid and e['name'] == name]
                if len(matches) == 1 and sum(e['name'] == name for e in introduced) == 1:
                    for paragraph in matches[0]['evidence'].split('\n\n'):
                        narration = re.sub(r'“[^”]*”|「[^」]*」|"[^"]*"', '', paragraph)
                        if name not in narration:
                            continue
                        try:
                            check_direct_evidence(paragraph)
                        except ValueError:
                            continue
                        identity_evidence = paragraph
                        break
            check_observed_reference(reference, known, evidence,
                {e['id'] for e in update['introductions']['locations']}, strict=True, player_id=player,
                identity_evidence=identity_evidence)
            if narrated_location and reference.get('attribute') == 'locationId':
                from .entity_facts import check_narrated_location
                check_narrated_location(evidence, reference['observedLocationName'], reference['entityName'])
        return evidence

    seen = set()
    for item in entries(raw, 'outcomes', 12):
        try:
            evidence = observed_quote(item)
            if evidence is None:
                continue
            cid, status = item.get('characterId'), item.get('status')
            if not isinstance(cid, str) or known.get(cid, {}).get('kind') != 'character' or cid in seen:
                raise ValueError('人物后果ID无效或重复')
            if status not in ('alive', 'injured', 'missing', 'dead', 'departed'):
                raise ValueError('人物后果值无效')
            permanence = 'permanent' if status == 'dead' else item.get('permanence', 'temporary')
            if permanence not in ('permanent', 'temporary') or status in ('alive', 'injured', 'missing') and permanence != 'temporary':
                raise ValueError('人物后果期限无效')
            old = state.get('characterOutcomeStates', {}).get(cid, {})
            if old.get('permanence') == 'permanent' and old.get('status') in ('dead', 'departed') and (status, permanence) != (old['status'], old['permanence']):
                raise ValueError('不能撤销已发生的永久后果')
            seen.add(cid)
            if (status, permanence) == (old.get('status'), old.get('permanence')):
                continue
            update['outcomes'].append(dict(characterId=cid, status=status, permanence=permanence,
                requirementId='A1', cause=_text(item.get('cause')), evidence=evidence))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            deferred(item, error)
    # A current on-page confirmation may repair a missed earlier observation.
    # It changes this new branch only; saved prose and ancestor state stay intact.
    changes = entries(raw, 'stateChanges')
    confirmations = entries(data, 'confirmedStates', 12)
    accepted = {}
    for item in scene_confirmations + changes + confirmations:
        try:
            # These labels name the same reserved location field, not new
            # arbitrary attributes that may bypass entity-reference handling.
            if isinstance(item, dict) and item.get('attribute') in ('当前位置', '当前地点', '所在地点', '所在位置'):
                raise ValueError('位置须通过currentScene或locationId登记，不使用自由属性代替')
            evidence = observed_quote(item)
            if evidence is None:
                continue
            entity, attribute = item.get('entityId'), item.get('attribute')
            if not isinstance(entity, str) or entity not in known:
                raise ValueError('变化实体未登记')
            if attribute == 'holderCharacterId' and known[entity]['kind'] == 'item':
                attribute = 'ownerCharacterId'
            if attribute == 'locationId' and 'locationName' in item:
                target = known.get(item.get('value')) if isinstance(item.get('value'), str) else None
                clearing = item.get('value') is None and item['locationName'] is None
                if not clearing and (not target or target['kind'] != 'location'
                                     or item['locationName'] != target['name']):
                    raise ValueError('地点名称与ID不对应；新地点应独立登记，不能沿用旧场景ID')
            before = actions.value_at(state, entity, attribute, known[entity]['kind'])
            if 'value' not in item:
                raise ValueError('状态变化缺少value')
            key = (entity, attribute)
            if key in accepted:
                if item['value'] != accepted[key]:
                    raise ValueError('同回合状态记录冲突，保留先验证的实际变化')
                continue
            if item['value'] == before:
                accepted[key] = item['value']
                continue
            change = dict(id='C' + str(len(update['stateChanges']) + 1), entityId=entity,
                          attribute=attribute, before=before, value=item['value'], stepId='S1',
                          reason=_text(item.get('reason')), evidence=evidence)
            candidate = {**update, 'stateChanges': update['stateChanges'] + [change]}
            actions.validate_plan(candidate, {**context, 'narrativePolicy': 'context_only'})
            update = candidate
            accepted[key] = item['value']
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            deferred(item, error)
    goals = {g['id']: g for g in consequences.goals_for(package, context['contract'], state)}
    seen = set()
    for item in entries(raw, 'goalUpdates', 8):
        try:
            evidence = _quote(item, body)
            gid, status = item.get('id'), item.get('status')
            if not isinstance(gid, str) or gid in seen or gid != 'new' and gid not in goals:
                raise ValueError('目标引用无效')
            if status not in ('active', 'completed', 'transformed', 'abandoned') or gid == 'new' and status != 'active':
                raise ValueError('目标状态无效')
            title = _text(item.get('title'))
            if gid in goals and (title != goals[gid]['title'] or goals[gid]['status'] != 'active'):
                raise ValueError('不能改名或重开已结束目标')
            deps = item.get('dependencies', goals.get(gid, {}).get('dependencies', []))
            if not isinstance(deps, list) or any(not isinstance(c, str) or known.get(c, {}).get('kind') != 'character' for c in deps):
                raise ValueError('目标依赖引用无效')
            result = dict(id=gid, title=title, status=status, dependencies=deps,
                          reason=_text(item.get('reason')), evidence=evidence,
                          successor=_text(item.get('successor')) if status == 'transformed' else '')
            from . import item_lifecycle
            if 'itemDependencies' in item:
                result['itemDependencies'] = copy.deepcopy(item['itemDependencies'])
            item_lifecycle.validate_dependencies(result, goals.get(gid), update, known, state, status in ('active', 'transformed'))
            seen.add(gid)
            update['goalUpdates'].append(result)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            deferred(item, error)
    current_threads = {t['id']: t for t in reader_threads.threads_for(package, context['contract'], state)}
    for item in entries(raw, 'threadUpdates', 8):
        try:
            result = {**item, 'stepIds': ['S1'], 'evidence': _quote(item, body)}
            result.pop('paragraphIds', None)
            prior = current_threads.get(result.get('id'))
            if prior:
                from . import item_lifecycle
                if (result.get('title') == prior['title'] and result.get('status') == prior['status']
                        and reader_threads.metadata(result, prior) == reader_threads.metadata(prior)
                        and item_lifecycle.dependencies(result, prior) == item_lifecycle.dependencies(prior)):
                    continue  # Repeating an unchanged question is not a new uncertainty.
            candidate = {**update, 'threadUpdates': update['threadUpdates'] + [result]}
            reader_threads.validate_updates(candidate, context)
            update = candidate
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            deferred(item, error)
    summary = data.get('summary')
    summary = summary.strip()[:700] if isinstance(summary, str) and summary.strip() else body[-600:]
    status = data.get('actionStatus')
    if status not in ('performed', 'partial', 'blocked', 'unknown'):
        status = 'unknown'
    if status != 'performed':
        unresolved('尚未完全落实的行动：' + action[:220], 'action_pending', body[-600:])
    for item in entries(data, 'followups', 4):
        try:
            quote = _quote(item, body)
            text = _text(item.get('summary'), 400)
            identifier = item.get('id')
            existing = next((i for i in issues if i['id'] == identifier and i['status'] == 'open'), None) if isinstance(identifier, str) else None
            if identifier is not None and existing is None:
                raise ValueError('待澄清事项更新须引用已有ID')
            if existing:
                existing.update(summary=text, evidence=quote, lastObservedNarrativeSha256=digest(body))
            else:
                unresolved(text, 'continuity', quote)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            diagnostics.append(dict(reason=str(error)))
    inherited_ids = {i['id'] for i in continuity_context(context['parent'], action)['pendingFollowups']}
    for item in entries(data, 'resolvedFollowups', 8):
        try:
            identifier = item.get('id')
            if identifier not in inherited_ids:
                raise ValueError('只可解决已有待澄清事项')
            raw_followups = data.get('followups', [])
            if isinstance(raw_followups, list) and any(isinstance(f, dict) and f.get('id') == identifier
                                                      for f in raw_followups):
                raise ValueError('本回合仍待澄清的事项不能同时标为解决')
            quote = _quote(item, body)
            resolution = _text(item.get('summary'), 400)
            issue = next(i for i in issues if i['id'] == identifier)
            issue.update(status='resolved', resolution=resolution, resolutionEvidence=quote,
                         resolutionNarrativeSha256=digest(body))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            diagnostics.append(dict(reason=str(error)))
    outcome = dict(action=dict(status=status, summary=summary[:160], evidence=body[:600]),
                   actions=[], clues=[], relationships=[])
    issues = ([i for i in issues if i['status'] == 'open'] +
              [i for i in issues if i['status'] == 'resolved'][-12:])
    return dict(update=update, outcome=outcome, followups=issues, diagnostics=diagnostics, summary=summary)


def seal(context, body, data, failure=None):
    return dict(version=VERSION, kind='observed_state_not_prose_approval',
                parentSha256=digest(context['parent']), narrativeSha256=hashlib.sha256(body.encode()).hexdigest(),
                actionSha256=digest(context['playerDirection']), data=copy.deepcopy(data), failure=failure)


def validate_commit(context, result):
    receipt = result.get('deliveryReceipt')
    action = context.get('playerDirection') or result['actionIntent']['input']
    if (not isinstance(receipt, dict) or receipt.get('version') not in (VERSION, LEGACY_VERSION, 'narrative-delivery/2', 'narrative-delivery/3', 'narrative-delivery/4')
            or receipt.get('kind') != 'observed_state_not_prose_approval'
            or receipt.get('parentSha256') != digest(context['parent'])
            or receipt.get('narrativeSha256') != hashlib.sha256(result['narrativeText'].encode()).hexdigest()
            or receipt.get('actionSha256') != digest(action)):
        raise ValueError('正文交付凭据与请求、父分支或正文不符')
    recorded = observe({**context, 'playerDirection': action}, result['narrativeText'], receipt.get('data'),
                       receipt.get('failure'), legacy=receipt['version'] == LEGACY_VERSION,
                       literal_scope=receipt['version'] in (VERSION, 'narrative-delivery/3', 'narrative-delivery/4'),
                       narrated_location=receipt['version'] in (VERSION, 'narrative-delivery/4'),
                       separate_identity=receipt['version'] == VERSION)
    if (recorded['update'] != result.get('consequenceUpdate')
            or recorded['followups'] != result.get('continuityFollowups')
            or recorded['outcome'] != result.get('readerOutcome')):
        raise ValueError('交付记录与实际提取绑定不符')
