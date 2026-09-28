"""Validate quoted qualifier proposals without claiming semantic authority."""
import copy

from test_support import context_fullscan as f


def resolve(ref, units, selected):
    """Model selects a quote occurrence; the program computes code-point offsets."""
    if not isinstance(ref, dict) or set(ref) != {'unitId', 'quote', 'occurrence'}:
        raise ValueError('引用字段无效')
    uid, quote, occurrence = ref['unitId'], ref['quote'], ref['occurrence']
    if (not isinstance(uid, str) or uid not in selected or uid not in units
            or not isinstance(quote, str) or not quote.strip()
            or type(occurrence) is not int or occurrence < 0):
        raise ValueError('引用单元、引文或出现次数无效')
    text = units[uid]['quote']
    matches = [i for i in range(len(text)) if text.startswith(quote, i)]
    if occurrence >= len(matches):
        raise ValueError('所选引文出现位置不存在')
    start = units[uid]['start'] + matches[occurrence]
    return dict(unitId=uid, start=start, end=start+len(quote), quote=quote)


def _spans(refs, units, selected, *, empty=False):
    if not isinstance(refs, list) or (not refs and not empty):
        raise ValueError('跨度列表无效')
    spans = [resolve(ref, units, selected) for ref in refs]
    keys = [(s['start'], s['end']) for s in spans]
    if keys != sorted(set(keys)) or any(a['end'] > b['start'] for a, b in zip(spans, spans[1:])):
        raise ValueError('跨度重叠、重复或未按原文排序')
    return spans


def _within(span, spans):
    return any(p['start'] <= span['start'] and span['end'] <= p['end'] for p in spans)


def inspect(response, case, entities):
    """Return unchanged proposals, derived spans and issues. Never remove a label."""
    units = f.indexed_input(case)['units']
    projections, evidence, issues = [], [], []
    try:
        if not isinstance(response, dict) or set(response) != {'items'} or not isinstance(response['items'], list):
            raise ValueError('只能返回 items')
        for index, item in enumerate(response['items']):
            if not isinstance(item, dict) or set(item) != {'unitIds', 'status', 'normal', 'limitations', 'reason', 'anchors'}:
                raise ValueError('命题字段无效')
            ids = item['unitIds']
            if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in units for i in ids)
                    or len(set(ids)) != len(ids) or ids != [i for i in units if i in ids]):
                raise ValueError('命题原文单元无效')
            anchors = item['anchors']
            if not isinstance(anchors, dict) or set(anchors) != {'subject', 'boundary', 'position'}:
                raise ValueError('锚点字段无效')
            position = _spans(anchors['position'], units, ids)
            resolved = {k: resolve(anchors[k], units, ids) if anchors[k] is not None else None
                        for k in ('subject', 'boundary')}
            resolved['position'] = position
            if not isinstance(item['limitations'], list):
                raise ValueError('限定列表无效')
            limits, projected_limits = [], []
            for limit in item['limitations']:
                if not isinstance(limit, dict) or set(limit) != {'kind', 'cue', 'scope', 'premise'}:
                    raise ValueError('限定依据字段无效')
                kind = limit['kind']
                if not isinstance(kind, str) or kind not in f.LIMITATIONS:
                    raise ValueError('限定类型无效')
                cue = resolve(limit['cue'], units, ids)
                scope = _spans(limit['scope'], units, ids)
                premise = _spans(limit['premise'], units, ids, empty=True)
                projected_limits.append(dict(kind=kind, quote=cue['quote']))
                limits.append(dict(kind=kind, cue=cue, scope=scope, premise=premise))

            projected = {k: copy.deepcopy(item[k]) for k in ('unitIds', 'status', 'normal', 'reason')}
            projected['limitations'] = projected_limits
            # Existing format rules remain binding, but no gold label is read here.
            checked = f.assess(dict(items=[projected]), dict(draft=case['draft'], targets=[]), entities)
            if checked['status'] == 'invalid_response':
                raise ValueError(checked['error'])
            projections.append(projected)
            evidence.append(dict(anchors=resolved, limitations=limits))

            def issue(code, kind=None):
                issues.append(dict(itemIndex=index, code=code, kind=kind))

            flags = {x['kind'] for x in limits}
            for role, entity_kind, flag, normal_key in (
                    ('subject', 'scene_person', 'unresolved_subject', 'subject'),
                    ('boundary', 'scene_boundary', 'unresolved_object', 'object')):
                span = resolved[role]
                matches = [key for key, entity in entities.items() if span is not None
                           and entity['kind'] == entity_kind and span['quote'] in entity['mentions']]
                if len(matches) == 1 and flag in flags:
                    issue('registered_entity_marked_unresolved', flag)
                if item['status'] == 'candidate' and (len(matches) != 1 or matches[0] != item['normal'][normal_key]):
                    issue('candidate_entity_anchor_unverified')
                if item['status'] == 'candidate' and (span is None or not _within(span, position)):
                    issue('candidate_entity_outside_position')

            for limit in limits:
                kind, cue = limit['kind'], limit['cue']
                if limit['scope'] != position:
                    issue('scope_does_not_match_position', kind)
                if kind == 'condition':
                    if not limit['premise']:
                        issue('condition_premise_missing', kind)
                    elif not _within(cue, limit['premise']):
                        issue('condition_cue_outside_premise', kind)
                elif limit['premise']:
                    issue('unexpected_premise', kind)
                if kind == 'modality':
                    if cue['quote'].strip('，。；、 ') in {'就', '便', '如果', '只要', '若', '则'}:
                        issue('connective_alone_not_epistemic_evidence', kind)
                    if any(cue == p for p in position):
                        issue('whole_position_is_not_specific_cue', kind)
                    if not _within(cue, position):
                        issue('modality_cue_outside_position', kind)
                    if any(cue == other['cue'] for other in limits if other['kind'] == 'condition'):
                        issue('condition_and_modality_share_cue', kind)
            # Unfamiliar cues, negation and lexical ambiguity remain semantic work.
            # Absence of a diagnostic does NOT certify a qualifier's interpretation.
    except (ValueError, KeyError, TypeError) as error:
        return dict(status='invalid_response', error=str(error), proposedResponse=copy.deepcopy(response),
                    semanticStatus='unverified', reviewRequired=True, productionEnablement=False)
    return dict(status='evidence_issues' if issues else 'structurally_valid',
                proposedResponse=copy.deepcopy(response), projection=dict(items=projections), evidence=evidence, issues=issues,
                semanticStatus='unverified', reviewRequired=True, productionEnablement=False)


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] == 'invalid_response':
        return result
    result['extractionScore'] = f.assess(result['projection'], case, entities)
    result['status'] = ('matched' if result['extractionScore']['status'] == 'matched' and not result['issues']
                        else 'mismatched')
    return result
