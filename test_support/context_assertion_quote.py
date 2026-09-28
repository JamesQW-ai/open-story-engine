"""Isolated exact-quote assertion contract; all results remain non-authoritative."""
from collections import Counter
import copy
import json

from test_support import context_assertion_locator as locator

VERSION = 'assertion-quote/0.1'
frame = locator.previous.frame
model_input = locator.project_input


def _within(inner, outer):
    return outer['start'] <= inner['start'] and inner['end'] <= outer['end']


def inspect(response, case, entities):
    result = dict(proposedResponse=copy.deepcopy(response), semanticStatus='unverified',
                  productionEnablement=False, acceptance=False, reviewRequired=True)
    try:
        frame._fields(response, {VERSION}, '必须使用独立逐字引用协议')
        data, table = model_input(case, entities), locator.units(case)
        decisions = response[VERSION]
        if not isinstance(decisions, list) or len(decisions) > frame.previous.MAX_SLOTS:
            raise ValueError('任务数组无效或超预算')
        seen, total, reported, unresolved, candidates = set(), 0, [], [], []
        for d in decisions:
            if not isinstance(d, dict):
                raise ValueError('任务必须为对象')
            subject, resolution = d.get('subject'), d.get('resolution')
            if not isinstance(subject, str) or subject not in data['tasks'] or subject in seen:
                raise ValueError('任务未知或重复')
            seen.add(subject)
            subject_anchor = locator.resolve(data['entityRefs'][subject]['anchor'], table, table)
            uid = subject_anchor['unitId']
            context = data['tasks'][subject]['contextUnits']
            allowed = [uid]+context
            if resolution in ('absent', 'unresolved'):
                frame._fields(d, {'subject', 'resolution', 'basis'}, '非位置决策字段不符')
                basis = locator.resolve(d['basis'], table, [uid])
                if not _within(subject_anchor, basis):
                    raise ValueError('非位置依据缺少当前主体')
                if resolution == 'unresolved': unresolved.append(subject)
                continue
            frame._fields(d, {'subject', 'resolution', 'items'}, '位置决策字段不符')
            if resolution != 'positions' or not isinstance(d['items'], list) or not d['items']:
                raise ValueError('位置决策须有候选')
            total += len(d['items'])
            if total > frame.previous.MAX_ITEMS:
                raise ValueError('候选数量超预算')
            for item in d['items']:
                frame._fields(item, {'boundary', 'position', 'core', 'origin', 'conditions',
                    'modifiers', 'nonPremiseUnits', 'unresolvedUnits'}, '候选字段不符')
                boundary = item['boundary']
                if not isinstance(boundary, str) or boundary not in data['tasks'][subject]['boundaries']:
                    raise ValueError('边界不属于当前任务')
                position = item['position']
                frame._fields(position, {'value', 'polarity'}, '位置字段不符')
                if (position['value'] not in ('inside', 'outside', 'on_boundary')
                        or position['polarity'] not in ('positive', 'negative')):
                    raise ValueError('位置或极性枚举无效')
                core = locator.resolve(item['core'], table, [uid])
                boundary_anchor = locator.resolve(data['entityRefs'][boundary]['anchor'], table, [uid])
                if not _within(boundary_anchor, core):
                    raise ValueError('核心没有包含所选边界出现')
                origin = item['origin']
                if origin is None:
                    if not _within(subject_anchor, core):
                        raise ValueError('直述核心不能借用外部主体')
                else:
                    frame._fields(origin, {'speaker', 'cue', 'scope', 'subjectMention'}, '转述来源字段不符')
                    speaker = origin['speaker']
                    if (not isinstance(speaker, str) or speaker not in data['entityRefs']
                            or data['entityRefs'][speaker]['kind'] != 'scene_person'
                            or data['entityRefs'][speaker]['ambiguous']):
                        raise ValueError('转述缺少唯一登记说话人')
                    speaker_span = locator.resolve(data['entityRefs'][speaker]['anchor'], table, [uid])
                    cue = locator.resolve(origin['cue'], table, [uid])
                    scope = locator.resolve(origin['scope'], table, [uid])
                    mention = locator.resolve(origin['subjectMention'], table, [uid])
                    if not all(_within(x, scope) for x in (core, cue, speaker_span, subject_anchor)):
                        raise ValueError('来源范围未包裹核心、线索和人物锚点')
                    if not _within(mention, core) or max(cue['start'], core['start']) < min(cue['end'], core['end']):
                        raise ValueError('主体称呼须在核心中，说话线索须在核心外')
                    # Mention-to-entity binding is a semantic hypothesis, not a
                    # verified pronoun mapping. Every reported candidate is withheld.
                    reported.append(subject)
                assigned = list(frame._units(item['nonPremiseUnits'], context))
                pending = frame._units(item['unresolvedUnits'], context)
                assigned.extend(pending)
                if pending: unresolved.append(subject)
                if not isinstance(item['conditions'], list) or not isinstance(item['modifiers'], list):
                    raise ValueError('限定必须为数组')
                for condition in item['conditions']:
                    frame._fields(condition, {'units', 'cue'}, '条件字段不符')
                    selected = frame._units(condition['units'], context)
                    if not selected:
                        raise ValueError('条件须有完整前提单元')
                    frame.extraction._selection(selected, list(table))
                    assigned.extend(selected)
                    locator.resolve(condition['cue'], table, selected)
                if len(assigned) != len(set(assigned)) or set(assigned) != set(context):
                    raise ValueError('依赖单元须恰好完整划分一次')
                for modifier in item['modifiers']:
                    frame._fields(modifier, {'kind', 'cue'}, '限定字段不符')
                    if modifier['kind'] not in ('modality', 'time', 'unresolved_subject', 'unresolved_object', 'ambiguity'):
                        raise ValueError('限定类型无效')
                    locator.resolve(modifier['cue'], table, allowed)
                candidates.append(dict(subject=subject, candidate=copy.deepcopy(item)))
        if seen != set(data['tasks']):
            raise ValueError('任务遗漏')
        pending_units = frame.previous.model_input(case, entities)['coverageIndex']['pendingUnits']
        result.update(status='scope_pending' if reported or unresolved or pending_units else 'structurally_valid',
                      candidates=candidates, reportedSubjects=reported, unresolvedSubjects=unresolved,
                      pendingUnits=pending_units)
    except (ValueError, TypeError, KeyError, IndexError) as error:
        result.update(status='invalid_response', error=str(error))
    return result


def _key(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def assess(response, case, entities, reference):
    if inspect(reference, case, entities)['status'] == 'invalid_response':
        raise ValueError('冻结参考结构无效')
    checked = inspect(response, case, entities)
    checked.update(referenceAgreement=None, comparisonScope='frozen_reference_not_semantic_truth')
    if checked['status'] == 'invalid_response':
        return checked
    if not response[VERSION]:
        checked.update(status='out_of_scope')
        return checked
    table = locator.units(case)
    def canonical_ref(ref):
        span = locator.resolve(ref, table, table)
        # Only surrounding whitespace and a terminal Chinese full stop may
        # vary. Negation, quotation marks and qualifiers are never normalized.
        text = span['quote']
        left = len(text)-len(text.lstrip())
        right = len(text.rstrip().removesuffix('。').rstrip())
        return [span['start']+left, span['start']+right]
    def projections(value):
        decisions, contents, origins, qualifiers, ranges = (Counter() for _ in range(5))
        for d in value[VERSION]:
            decisions[_key([d['subject'], d['resolution']])] += 1
            if d['resolution'] != 'positions':
                ranges[_key([d['subject'], 'basis', canonical_ref(d['basis'])])] += 1
            for item in d.get('items', []):
                key = [d['subject'], item['boundary'], item['position']]
                contents[_key(key)] += 1
                origin = item['origin']
                key += [origin['speaker'] if origin else None]
                origins[_key(key)] += 1
                q = dict(conditions=[dict(units=x['units'], cue=canonical_ref(x['cue'])) for x in item['conditions']],
                    modifiers=[dict(kind=x['kind'], cue=canonical_ref(x['cue'])) for x in item['modifiers']],
                    nonPremiseUnits=item['nonPremiseUnits'], unresolvedUnits=item['unresolvedUnits'])
                for field in ('conditions', 'modifiers'): q[field].sort(key=_key)
                qualifiers[_key(key+[q])] += 1
                r = dict(core=canonical_ref(item['core']), origin=None if not origin else
                    {k: canonical_ref(origin[k]) for k in ('cue', 'scope', 'subjectMention')})
                # Bind qualifier and ranges together to detect swapped features
                # even when individual marginal counts are identical.
                ranges[_key(key+[q, r])] += 1
        return dict(decisions=decisions, contents=contents, origins=origins, qualifiers=qualifiers, ranges=ranges)
    expected, actual = projections(reference), projections(response)
    dimensions = {k: dict(expected=sum(expected[k].values()), actual=sum(actual[k].values()),
        missing=sum((expected[k]-actual[k]).values()), extra=sum((actual[k]-expected[k]).values())) for k in expected}
    equal = all(not d['missing'] and not d['extra'] for d in dimensions.values())
    checked.update(structuralStatus=checked['status'], status='reference_match' if equal else 'reference_mismatch',
                   referenceAgreement=equal, dimensions=dimensions, legacySubtypeComparable=False)
    return checked
