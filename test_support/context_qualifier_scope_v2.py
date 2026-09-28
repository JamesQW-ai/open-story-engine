"""Versioned qualifier envelopes; valid spans never grant semantic authority."""
import copy

from test_support.context_qualifier_evidence import resolve, _spans, _within, f


def inspect(response, case, entities):
    """Return unchanged proposals, derived spans and issues. Never remove a label."""
    units = f.indexed_input(case)['units']
    projections, evidence, issues = [], [], []
    try:
        if not isinstance(response, dict) or set(response) != {'schemaVersion', 'items'} or response.get('schemaVersion') != 'qualifier-scope/0.2' or not isinstance(response['items'], list):
            raise ValueError('必须返回 qualifier-scope/0.2 与 items')
        for index, item in enumerate(response['items']):
            if not isinstance(item, dict) or set(item) != {'unitIds', 'status', 'normal', 'limitations', 'reason', 'anchors'}:
                raise ValueError('命题字段无效')
            ids = item['unitIds']
            if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in units for i in ids)
                    or len(set(ids)) != len(ids) or ids != [i for i in units if i in ids]):
                raise ValueError('命题原文单元无效')
            anchors = item['anchors']
            if not isinstance(anchors, dict) or set(anchors) != {'subject', 'boundary', 'positionCore', 'qualifiedPosition'}:
                raise ValueError('锚点字段无效')
            position = _spans(anchors['positionCore'], units, ids)
            qualified = _spans(anchors['qualifiedPosition'], units, ids)
            resolved = {k: resolve(anchors[k], units, ids) if anchors[k] is not None else None
                        for k in ('subject', 'boundary')}
            resolved['positionCore'] = position
            resolved['qualifiedPosition'] = qualified
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

            if not all(_within(p, qualified) for p in position):
                issue('core_outside_qualified_position')
            if any(case['draft'][a['end']:b['start']].strip() for a,b in zip(qualified, qualified[1:])):
                issue('qualified_position_skips_text')
            # Conservative sentence-boundary diagnostic, not a semantic parser.
            envelope = case['draft'][qualified[0]['start']:qualified[-1]['end']].rstrip().rstrip(' 。！？!?；;”’"')
            if any(ch in envelope for ch in '。！？!?；;'):
                issue('qualified_position_crosses_sentence')
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
                if not _within(cue, qualified):
                    issue('cue_outside_qualified_position', kind)
                if not all(_within(p, qualified) for p in limit['premise']):
                    issue('premise_outside_qualified_position', kind)
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
                    if any(cue == p for p in position + qualified):
                        issue('whole_position_is_not_specific_cue', kind)
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
