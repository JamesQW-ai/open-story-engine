"""Select exact registered entity occurrences without generating entity quotations."""
import copy
import hashlib
import json

from test_support import context_qualifier_selection_strict as strict

f = strict.f
VERSION = 'qualifier-entity-refs/0.1'
segments = strict.segments


def entity_refs(case, entities):
    table = segments(case, entities)
    lexicon = {k: {field: v[field] for field in ('kind', 'mentions')} for k, v in entities.items()}
    binding = json.dumps([case['draft'], lexicon], sort_keys=True, ensure_ascii=False)
    prefix = 'E'+hashlib.sha256(binding.encode()).hexdigest()[:12]
    matches = {}
    for sid, part in table.items():
        text = part['quote']
        for eid, entity in sorted(lexicon.items()):
            if entity['kind'] not in ('scene_person', 'scene_boundary'):
                continue
            spans = {(start, start+len(mention)) for mention in entity['mentions'] if mention
                     for start in range(len(text)) if text.startswith(mention, start)}
            # Prefer a complete alias over its own nested shorter alias.
            for start, end in sorted(spans):
                if any(a <= start and end <= b and (a, b) != (start, end) for a, b in spans):
                    continue
                matches.setdefault((sid, start, end), []).append(eid)
    if len(matches) > 256:
        raise ValueError('实体出现引用预算超限，不截断原文')
    result = {}
    ordered = sorted(matches, key=lambda key: (table[key[0]]['start']+key[1], key[2]))
    for number, (sid, start, end) in enumerate(ordered, 1):
        quote = table[sid]['quote'][start:end]
        # Keep all exact-name candidates, including a nested alias pruned above.
        eids = [eid for eid, entity in lexicon.items() if quote in entity['mentions']
                and entity['kind'] in ('scene_person', 'scene_boundary')]
        positions = [i for i in range(len(table[sid]['quote'])) if table[sid]['quote'].startswith(quote, i)]
        result[f'{prefix}-{number}'] = dict(entityIds=sorted(eids),
                                          kinds=sorted({lexicon[eid]['kind'] for eid in eids}),
                                          segmentId=sid, quote=quote, occurrence=positions.index(start))
    return result


def model_input(case, entities):
    source = strict.model_input(case, entities)
    return dict(sourceBlocks=source['sourceBlocks'], entityRefs=entity_refs(case, entities))


def decode(response, case, entities):
    if not isinstance(response, dict) or set(response) != {VERSION} or not isinstance(response[VERSION], list):
        raise ValueError('顶层必须且仅包含 '+VERSION+' 数组；不补版本或迁移旧响应')
    refs = entity_refs(case, entities)
    items = []
    for index, original in enumerate(response[VERSION]):
        fields = strict.scoped.ITEM_FIELDS
        if not isinstance(original, dict):
            raise ValueError(f'items[{index}] 必须是对象')
        if set(original) != fields:
            raise ValueError(f'items[{index}] missing={sorted(fields-set(original))}, extra={sorted(set(original)-fields)}')
        item = copy.deepcopy(original)
        for role, kind in [('subject', 'scene_person'), ('boundary', 'scene_boundary')]:
            rid = original[role]
            if rid is None:
                continue
            if not isinstance(rid, str) or rid not in refs:
                raise ValueError(f'items[{index}].{role} 未知实体出现引用')
            ref = refs[rid]
            if len(ref['entityIds']) != 1:
                raise ValueError(f'items[{index}].{role} 实体出现引用有歧义')
            if ref['kinds'] != [kind]:
                raise ValueError(f'items[{index}].{role} 实体类型不匹配')
            item[role] = {k: ref[k] for k in ('segmentId', 'quote', 'occurrence')}
        items.append(item)
    return dict(schemaVersion=strict.VERSION, items=items)


def _evaluate(response, case, entities, scoring):
    try:
        decoded = decode(response, case, entities)
        result = (strict.assess if scoring else strict.inspect)(decoded, case, entities)
        result['decodedResponse'] = decoded
    except (ValueError, KeyError, TypeError) as error:
        result = dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                      reviewRequired=True, productionEnablement=False)
        if isinstance(response, dict) and isinstance(response.get(VERSION), list):
            diagnostic = strict.scoped.inspect(dict(schemaVersion=strict.scoped.VERSION,
                                                     items=response[VERSION]), case, entities)
            for key in ('fieldDiagnostics', 'scopeDiagnostics'):
                result[key] = diagnostic[key]
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def inspect(response, case, entities):
    return _evaluate(response, case, entities, False)


def assess(response, case, entities):
    return _evaluate(response, case, entities, True)
