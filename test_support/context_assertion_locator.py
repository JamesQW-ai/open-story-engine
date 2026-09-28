"""Offline source-location comparison; no candidate acceptance or semantic migration."""
import copy

from test_support import context_assertion_scope as previous
from test_support.context_qualifier_evidence import resolve as resolve_quote

VERSION = 'assertion-locator-preview/0.1'


def units(case):
    return previous.frame.extraction.f.indexed_input(case)['units']


def resolve(ref, table, allowed, encoding='quote'):
    if not isinstance(ref, list) or len(ref) != 3:
        raise ValueError('定位引用必须是三个元素的数组')
    uid, a, b = ref
    if not isinstance(uid, str) or uid not in table or uid not in allowed:
        raise ValueError('单元 ID 未知或越界；不修正片段 ID')
    if encoding == 'quote':
        return resolve_quote(dict(unitId=uid, quote=a, occurrence=b), table, allowed)
    if encoding != 'offset' or type(a) is not int or type(b) is not int:
        raise ValueError('区间必须使用整数码点位置')
    text = table[uid]['quote']
    if not 0 <= a < b <= len(text) or not text[a:b].strip():
        raise ValueError('字符区间为空或越界')
    return dict(unitId=uid, start=table[uid]['start']+a, end=table[uid]['start']+b, quote=text[a:b])


def select_unit(uid, table, allowed):
    if not isinstance(uid, str) or uid not in table or uid not in allowed:
        raise ValueError('完整单元选择无效；不接受片段或子串替代')
    return dict(unitId=uid, **table[uid])


def _encode(uid, start, end, table, encoding):
    """For offline controls only: encode an already known exact interval."""
    unit = table[uid]
    a, b = start-unit['start'], end-unit['start']
    if not 0 <= a < b <= len(unit['quote']):
        raise ValueError('离线已知范围越过单元')
    if encoding == 'offset':
        ref = [uid, a, b]
    elif encoding == 'quote':
        quote = unit['quote'][a:b]
        occurrences = [i for i in range(len(unit['quote'])) if unit['quote'].startswith(quote, i)]
        ref = [uid, quote, occurrences.index(a)]
    else:
        raise ValueError('未知定位编码')
    if resolve(ref, table, [uid], encoding)['start'] != start:
        raise ValueError('定位编码不能还原原始起点')
    return ref


def project_input(case, entities, encoding='quote'):
    old = previous.model_input(case, entities)
    table = units(case)
    segments = previous.frame.extraction.segments(case, entities)
    source, refs = [], {}
    for part in old['source']:
        source.append(part if isinstance(part, str) else
                      dict(unitId=part['unitId'], text=table[part['unitId']]['quote']))
    for rid, ref in old['entityRefs'].items():
        sid = ref['segmentId']
        located = resolve_quote(dict(unitId=sid, quote=ref['quote'], occurrence=ref['occurrence']),
                                segments, [sid])
        refs[rid] = dict(kind=ref['kind'], ambiguous=ref['ambiguous'],
            anchor=_encode(segments[sid]['unitId'], located['start'], located['end'], table, encoding))
    return dict(source=source, entityRefs=refs, tasks=copy.deepcopy(old['tasks']))


def project_control(control, case, entities, encoding='quote'):
    """Preview existing hand controls only; never reinterpret model answers."""
    checked = previous.inspect(control, case, entities)
    if checked['status'] == 'invalid_response':
        raise ValueError('旧控制无效，不能投影预览')
    table = units(case)
    segments = previous.frame.extraction.segments(case, entities)
    def spans(ids):
        previous.frame.extraction._selection(ids, list(segments))
        groups = []
        for sid in ids:
            part = segments[sid]
            if groups and groups[-1][0] == part['unitId']:
                groups[-1][2] = part['end']
            else:
                groups.append([part['unitId'], part['start'], part['end']])
        return [_encode(uid, start, end, table, encoding) for uid, start, end in groups]
    def cue(ref):
        sid = ref['segmentId']
        located = resolve_quote(dict(unitId=sid, quote=ref['quote'], occurrence=ref['occurrence']),
                                segments, [sid])
        return _encode(segments[sid]['unitId'], located['start'], located['end'], table, encoding)
    decisions = copy.deepcopy(control[previous.VERSION])
    for decision in decisions:
        if decision['resolution'] != 'positions':
            decision['basis'] = spans(decision['basis'])
        for item in decision.get('items', []):
            item['core'] = spans(item['core'])
            item['origin']['scope'] = spans(item['origin']['scope'])
            for field in ('conditions', 'modifiers'):
                for limit in item[field]:
                    limit['cue'] = cue(limit['cue'])
            # Whole premises and dependency partitions remain complete unit IDs.
            for uid in item['nonPremiseUnits']+item['unresolvedUnits']:
                select_unit(uid, table, table)
            for condition in item['conditions']:
                for uid in condition['units']:
                    select_unit(uid, table, table)
    return {VERSION: decisions}


def evidence_spans(preview, case, encoding='quote'):
    """Diagnostic reconstruction, not a validator for the future candidate protocol."""
    table = units(case)
    result = []
    for d in preview[VERSION]:
        refs = list(d.get('basis', []))
        for item in d.get('items', []):
            refs.extend(item['core']+item['origin']['scope'])
            refs.extend(x['cue'] for field in ('conditions', 'modifiers') for x in item[field])
        result.extend(resolve(ref, table, table, encoding) for ref in refs)
    return result
