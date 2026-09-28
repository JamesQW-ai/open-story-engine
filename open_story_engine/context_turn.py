"""Current reader path: bounded context, one draft, non-blocking observations."""
import json
import copy

from . import reader_actions, reader_consequences as consequences
from .api_reader_quality import action_requirements
from .cocreation import empty_branch_additions, plan_result, scripted_followup_directions
from .llm import LlmError, parse_json_content
from .prompts import render_prompt, catalog_version
from .reader_scene_plan import cjk_character_count


def bind_plan_base(candidate, context):
    """The bound parent owns old values; the model only proposes new values."""
    if not isinstance(candidate, dict) or candidate.get('decision') != 'ready':
        return candidate
    candidate = copy.deepcopy(candidate)
    additions = candidate.get('introductions')
    if (not isinstance(additions, dict) or set(additions) != {'characters', 'items', 'locations'}
            or any(not isinstance(group, list) or any(not isinstance(e, dict) or not e.get('id')
                       for e in group) for group in additions.values())):
        return candidate
    state = context['parent']['branchState']
    known = reader_actions.registry(context['package'], state)
    for group, entries in additions.items():
        retained = []
        for entity in entries:
            old = known.get(entity['id'])
            if old:
                if old['name'] != entity.get('name'):
                    raise ValueError(f"实体ID {entity['id']} 已属于 {old['name']}，不能改成 {entity.get('name')}。"
                                     '不同人物须使用新的独立ID，并同步修改其步骤及状态引用；不能仅删除introductions而混用旧人物ID。')
            else:
                retained.append(entity)
        additions[group] = retained
    known = reader_actions.registry(context['package'], state, additions)
    changes = candidate.get('stateChanges')
    if isinstance(changes, list):
        for change in changes:
            if (isinstance(change, dict) and isinstance(change.get('entityId'), str)
                    and change['entityId'] in known and isinstance(change.get('attribute'), str)):
                if known[change['entityId']]['kind'] == 'item' and change['attribute'] == 'holderCharacterId':
                    change['attribute'] = 'ownerCharacterId'
                change['before'] = reader_actions.value_at(state, change['entityId'], change['attribute'],
                                                           known[change['entityId']]['kind'])
    return candidate


def plan_turn(planner, context, selected, resolved_state, stream=None, stream_reset=None):
    from .api_narrative import complete_with_retry, player_action
    from . import narrative_delivery
    action = player_action(context, selected)
    context = {**context, 'playerDirection': action, 'narrativePolicy': 'context_only'}
    raw, observations, notes = [], [], []
    planner._preflight_context_projection(context, selected, resolved_state)
    requirements = action_requirements(action)
    payload, audit = planner._result_contract_context(context, selected, requirements)
    state = context['parent']['branchState']
    payload['branchEntities'] = {key: [{field: item[field] for field in ('id', 'name') if field in item}
                                     for item in state.get(key, [])]
                                 for key in ('derivedCharacters', 'derivedItems', 'derivedLocations')}
    planner.last_prompt_context['resultContractProjection'] = audit
    contract = None
    projected = resolved_state
    try:
        # The plan helps the writer. It is not a second permission system and
        # its failure cannot discard an otherwise usable narrative response.
        try:
            planned = complete_with_retry(planner.gateway, 'complete_json', [
                {'role': 'system', 'content': render_prompt('reader.context_plan')},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}], stage='result_contract')
            raw.append(planned.raw_response)
            observations.extend({**o, 'generationStage': 'result_contract'} for o in planned.observations)
            candidate = bind_plan_base(parse_json_content(planned.content), context)
            candidate = consequences.validate_plan(candidate, requirements, context)
            if candidate['decision'] == 'ready':
                scene = candidate.get('scenePlan')
                if isinstance(scene, dict):
                    target = scene.get('targetCjk')
                    if type(target) is int and 80 <= target <= 1500:
                        scene['targetCjk'] = [target, target]
                    elif (not isinstance(target, list) or len(target) != 2
                          or any(type(n) is not int for n in target)
                          or not 80 <= target[0] <= target[1] <= 1500):
                        scene['targetCjk'] = [150, 600]
                    scene.setdefault('lengthReason', '按本次互动需要自然完成')
                projected = consequences.projected_state(resolved_state, candidate, context['package'])
                contract = candidate
            else:
                notes.append(dict(stage='planning', reason=candidate.get('message', '计划未完成')))
        except (ValueError, TypeError, KeyError, AttributeError, LlmError) as error:
            notes.append(dict(stage='planning', reason=str(error)))
        context = {**context, 'resultContract': contract}
        body, _ = planner._scene_draft(context, selected, projected, None, None, raw, observations, 0, '')
        if not body.strip() or not any(c.isalnum() for c in body):
            raise LlmError('未返回实际正文', 'empty_narrative')
        planner.last_prompt_context.update(resultContractProjection=audit,
            proseReview=dict(mode='developer_direct', automaticGate=False), deliveryPolicy=narrative_delivery.VERSION)
        from .entity_facts import turn_entity_facts, observation_state
        hard = payload.get('contextProjection', {}).get('hardConstraints', {})
        entity_facts = turn_entity_facts(hard.get('entityContext', {}), state, action, body)
        entity_ids = {e['id'] for e in entity_facts['entities']}
        entity_ids.add(context['contract']['persona']['sourceCharacterId'])
        extraction = dict(input=action, desiredPlan=contract,
            entityFacts=entity_facts,
            authoritativeState=observation_state(state, entity_ids),
            registry={k: {f: e[f] for f in ('id', 'name', 'kind') if f in e}
                      for k, e in reader_actions.registry(context['package'], state).items() if k in entity_ids
                      or k == state.get('playerLocationId')},
            goals=payload.get('goals', []), threads=payload.get('threads', []),
            **narrative_delivery.continuity_context(context['parent'], action),
            recentContext=payload.get('contextProjection', {}).get('allowedEvidence', []),
            draft={f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))})
        data, extraction_failure = None, None
        try:
            # One observation call, no approval verdict, repair or regeneration.
            extracted = planner.gateway.complete_json([
                {'role': 'system', 'content': render_prompt('reader.state_extract')},
                {'role': 'user', 'content': json.dumps(extraction, ensure_ascii=False)}])
            raw.append(extracted.raw_response)
            observations.extend({**o, 'generationStage': 'state_observation'} for o in extracted.observations)
            data = parse_json_content(extracted.content)
        except (ValueError, TypeError, LlmError) as error:
            extraction_failure = str(error)
            if getattr(error, 'raw_response', None):
                raw.append(error.raw_response)
            observations.extend({**o, 'generationStage': 'state_observation'}
                                for o in getattr(error, 'observations', []))
        from .entity_facts import bind_player_mentions
        player_id = context['contract']['persona']['sourceCharacterId']
        player_name = reader_actions.registry(context['package'], state)[player_id]['name']
        data = bind_player_mentions(data, body, player_id, player_name)
        recorded = narrative_delivery.observe(context, body, data, extraction_failure)
        actual = consequences.projected_state(resolved_state, recorded['update'], context['package'])
        directions = consequences.filter_directions(scripted_followup_directions(context, selected, actual),
                                                     context['package'], actual)
        result = plan_result(context, selected, body, selected['title'], directions, 'medium')
        result.update(summary=recorded['summary'], branchAdditions=empty_branch_additions(),
            consequenceUpdate=recorded['update'], actionIntent={'input': action, 'requirements': requirements},
            deliveryReceipt=narrative_delivery.seal(context, body, data, extraction_failure),
            readerOutcome=recorded['outcome'], continuityFollowups=recorded['followups'],
            observationDiagnostics=recorded['diagnostics'],
            readingProfile={**planner.last_prompt_context.get('pacing', {}),
                            'actualCjk': cjk_character_count(body), 'expansionApplied': False},
            proseQuality={'status': 'not_automatically_reviewed', 'reviewer': 'developer'})
        return result, {'operation': 'branch_planner', 'model': planner.gateway.model,
                        'promptVersion': 'context-player-v2+' + catalog_version(), 'requestSummary': action,
                        'rawResponse': '\n\n'.join(raw), 'callObservations': observations,
                        'nonBlockingNotes': notes + recorded['diagnostics'],
                        'promptContext': planner.last_prompt_context}
    except (ValueError, LlmError) as error:
        rejected = error if isinstance(error, LlmError) else LlmError(str(error), 'delivery_failed')
        rejected.audit = {'operation': 'branch_planner', 'model': planner.gateway.model,
                          'rawResponse': '\n\n'.join(raw), 'callObservations': observations,
                          'promptContext': planner.last_prompt_context,
                          'error': str(error), 'failureStage': 'delivery', 'errorCode': rejected.code}
        raise rejected
