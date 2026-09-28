"""Select contiguous source segments; never regenerate a position quotation."""
import copy

from test_support import context_qualifier_scope_v2 as v2

f = v2.f
VERSION = 'qualifier-selection/0.1'


def person_occurrences(text, entities):
    matches = set()
    for entity in entities.values():
        if entity['kind'] == 'scene_person':
            for mention in entity['mentions']:
                if mention:
                    matches.update((i, i+len(mention)) for i in range(len(text)) if text.startswith(mention, i))
    result = []
    for start, end in sorted(matches, key=lambda x: (x[0], -x[1])):
        if not result or start >= result[-1][1]:
            result.append((start, end))
    return result


def segments(case, entities):
    units = f.indexed_input(case)['units']
    result = {}
    for uid, unit in units.items():
        cuts = sorted({0, len(unit['quote'])} | {a for a, _ in person_occurrences(unit['quote'], entities)})
        for number, (a, b) in enumerate(zip(cuts, cuts[1:]), 1):
            result[f'{uid}-S{number}'] = dict(unitId=uid, paragraphId=unit['paragraphId'],
                                            quote=unit['quote'][a:b], start=unit['start']+a, end=unit['start']+b)
    if len(result) > 128 or len(case['draft']) > 12000:
        raise ValueError('原文片段预算超限，不截断正文')
    return result


def model_input(case, entities):
    return dict(segments={sid: {k: x[k] for k in ('unitId', 'paragraphId', 'quote')}
                          for sid, x in segments(case, entities).items()},
                entities={k: {field: v[field] for field in ('kind', 'mentions')} for k, v in entities.items()})


def expand(response, case, entities):
    """Exact decoding of a new contract, not repair of a v2 model response."""
    if not isinstance(response, dict) or set(response) != {'schemaVersion', 'items'} or response['schemaVersion'] != VERSION or not isinstance(response['items'], list):
        raise ValueError('须返回 qualifier-selection/0.1 与 items')
    table = segments(case, entities)
    units = f.indexed_input(case)['units']
    order = list(table)

    def selection(ids, empty=False):
        if not isinstance(ids, list) or (not ids and not empty) or any(not isinstance(i, str) or i not in table for i in ids):
            raise ValueError('未知或缺失的片段标识')
        if ids and ids != order[order.index(ids[0]):order.index(ids[-1])+1]:
            raise ValueError('片段须连续、有序且不重复')
        return [table[i] for i in ids]

    def ref(uid, start, end):
        unit = units[uid]
        quote = case['draft'][start:end]
        positions = [i for i in range(len(unit['quote'])) if unit['quote'].startswith(quote, i)]
        return dict(unitId=uid, quote=quote, occurrence=positions.index(start-unit['start']))

    def spans(ids, empty=False):
        chunks = selection(ids, empty)
        groups = []
        for chunk in chunks:
            if groups and groups[-1]['unitId'] == chunk['unitId'] and groups[-1]['end'] == chunk['start']:
                groups[-1]['end'] = chunk['end']
            else:
                groups.append(dict(chunk))
        return [ref(c['unitId'], c['start'], c['end']) for c in groups]

    def anchor(value, allowed):
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {'segmentId', 'quote', 'occurrence'}:
            raise ValueError('单个引用须为 segmentId/quote/occurrence 对象')
        sid = value['segmentId']
        if not isinstance(sid, str) or sid not in allowed:
            raise ValueError('引用不属于完整命题')
        # Reuse strict occurrence validation against the selected raw segment.
        local = v2.resolve(dict(unitId=sid, quote=value['quote'], occurrence=value['occurrence']), table, allowed)
        return ref(table[sid]['unitId'], local['start'], local['end'])

    items, precision = [], []
    for index, item in enumerate(response['items']):
        if not isinstance(item, dict) or set(item) != {'status', 'normal', 'reason', 'subject', 'boundary', 'core', 'qualified', 'limitations'}:
            raise ValueError('命题字段无效，不接收重写的核心引文')
        core = spans(item['core'])
        qualified = spans(item['qualified'])
        if not set(item['core']) <= set(item['qualified']):
            raise ValueError('核心不属于完整命题')
        if not isinstance(item['limitations'], list):
            raise ValueError('限定列表无效')
        limits = []
        for limit in item['limitations']:
            if not isinstance(limit, dict) or set(limit) != {'kind', 'cue', 'premise'}:
                raise ValueError('限定字段无效；作用对象固定为当前命题 core')
            limits.append(dict(kind=limit['kind'], cue=anchor(limit['cue'], item['qualified']),
                               premise=spans(limit['premise'], empty=True), scope=copy.deepcopy(core)))
        ids = list(dict.fromkeys(table[sid]['unitId'] for sid in item['qualified']))
        items.append(dict(unitIds=ids, status=item['status'], normal=copy.deepcopy(item['normal']),
                          reason=item['reason'], limitations=limits,
                          anchors=dict(subject=anchor(item['subject'], item['qualified']),
                                       boundary=anchor(item['boundary'], item['qualified']),
                                       positionCore=core, qualifiedPosition=qualified)))
        if sum(len(person_occurrences(c['quote'], entities)) for c in core) > 1:
            precision.append(dict(itemIndex=index, code='core_contains_multiple_person_mentions', kind=None))
    return dict(schemaVersion='qualifier-scope/0.2', items=items), precision


def inspect(response, case, entities):
    try:
        expanded, issues = expand(response, case, entities)
        result = v2.inspect(expanded, case, entities)
        result['expandedResponse'] = expanded
        if result['status'] != 'invalid_response':
            result['issues'].extend(issues)
            result['status'] = 'evidence_issues' if result['issues'] else 'structurally_valid'
    except (ValueError, KeyError, TypeError) as error:
        result = dict(status='invalid_response', error=str(error), semanticStatus='unverified',
                      reviewRequired=True, productionEnablement=False)
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] != 'invalid_response':
        result['extractionScore'] = f.assess(result['projection'], case, entities)
        result['status'] = 'matched' if result['extractionScore']['status'] == 'matched' and not result['issues'] else 'mismatched'
    return result
