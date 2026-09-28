"""Offline assertion-origin prototype; validates references, never truth or ownership."""
import copy

from test_support import context_qualifier_frame as frame

VERSION = 'assertion-scope/0.1'
model_input = frame.model_input


def _scope(value, table, allowed_units, required):
    ids = frame.extraction._selection(value, list(table))
    if any(table[s]['unitId'] not in allowed_units for s in ids):
        raise ValueError('断言范围越过当前标点候选范围')
    if not set(required).issubset(ids):
        raise ValueError('断言范围没有覆盖主体、命题或完整限定')
    return ids


def inspect(response, case, entities):
    result = dict(proposedResponse=copy.deepcopy(response), semanticStatus='unverified',
                  reviewRequired=True, productionEnablement=False, acceptance=False)
    try:
        frame._fields(response, {VERSION}, '须显式使用 '+VERSION+'；不迁移旧响应')
        data = model_input(case, entities)
        table = frame.extraction.segments(case, entities)
        decisions = response[VERSION]
        if not isinstance(decisions, list) or len(decisions) > frame.previous.MAX_SLOTS:
            raise ValueError('决策数组无效或超预算')
        converted, origins, absent, unresolved, seen = [], [], [], [], set()
        item_count = 0
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError('任务决策须为对象')
            subject, resolution = decision.get('subject'), decision.get('resolution')
            if not isinstance(subject, str) or subject not in data['tasks'] or subject in seen:
                raise ValueError('主体任务未知、重复或不属于当前原文')
            seen.add(subject)
            anchor = data['entityRefs'][subject]['segmentId']
            own_unit = table[anchor]['unitId']
            allowed = [own_unit]+data['tasks'][subject]['contextUnits']
            if resolution in ('absent', 'unresolved'):
                frame._fields(decision, {'subject', 'resolution', 'basis'}, '无位置或未决决策字段无效')
                _scope(decision['basis'], table, [own_unit], [anchor])
                (absent if resolution == 'absent' else unresolved).append(copy.deepcopy(decision))
                converted.append(dict(subject=subject, attribution='no_position' if resolution == 'absent'
                                      else 'unresolved', items=[]))
                continue
            if resolution != 'positions':
                raise ValueError('决策须为 positions/absent/unresolved')
            frame._fields(decision, {'subject', 'resolution', 'items'}, '位置决策字段无效')
            if not isinstance(decision['items'], list) or not decision['items']:
                raise ValueError('位置决策必须有候选')
            item_count += len(decision['items'])
            if item_count > frame.previous.MAX_ITEMS:
                raise ValueError('位置候选数量超限')
            items = []
            for item in decision['items']:
                frame._fields(item, {'boundary', 'position', 'core', 'conditions', 'modifiers',
                                     'nonPremiseUnits', 'unresolvedUnits', 'origin'}, '位置候选字段无效')
                origin = item['origin']
                frame._fields(origin, {'speaker', 'scope'}, '断言来源字段无效')
                speaker = origin['speaker']
                if speaker is not None and (not isinstance(speaker, str) or speaker not in data['entityRefs']
                                            or data['entityRefs'][speaker]['kind'] != 'scene_person'
                                            or data['entityRefs'][speaker].get('ambiguous')):
                    raise ValueError('转述来源须为当前唯一登记人物引用')
                required = set(item['core']) | {anchor}
                for condition in item['conditions']:
                    required.update(s for s, part in table.items() if part['unitId'] in condition['units'])
                    required.add(condition['cue']['segmentId'])
                for modifier in item['modifiers']:
                    required.add(modifier['cue']['segmentId'])
                if speaker is not None:
                    required.add(data['entityRefs'][speaker]['segmentId'])
                _scope(origin['scope'], table, allowed, required)
                items.append({k: copy.deepcopy(v) for k, v in item.items() if k != 'origin'})
                origins.append(dict(subject=subject, itemIndex=len(items)-1, origin=copy.deepcopy(origin),
                                    candidate=copy.deepcopy(item)))
            converted.append(dict(subject=subject, attribution='position', items=items))
        if seen != set(data['tasks']):
            raise ValueError('任务决策不完整')
        # Compatibility is only a structural check. Do not score collapsed
        # absence classes against the old fine-grained attribution gold.
        checked = frame.inspect({frame.VERSION: converted}, case, entities)
        if checked['status'] == 'invalid_response':
            raise ValueError(checked['error'])
        direct = [o for o in origins if o['origin']['speaker'] is None]
        reported = [o for o in origins if o['origin']['speaker'] is not None]
        pending_units = checked['coverage']['pendingUnits']
        pending_dependencies = checked['coverage']['unresolvedDependencies']
        status = 'scope_pending' if (reported or unresolved or pending_units or pending_dependencies) else 'structurally_valid'
        # Both kinds remain observations of the proposed response. There is no
        # decodedProposal or commit-ready output, even for narrator candidates.
        result.update(status=status, narratorCandidates=direct, reportedCandidates=reported,
                      absenceDecisions=absent, unresolvedDecisions=unresolved,
                      pendingUnits=pending_units, pendingDependencies=pending_dependencies,
                      legacyAttributionComparable=False)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        result.update(status='invalid_response', error=str(error))
    return result
