"""Expose punctuation boundaries and diagnose malformed proposals without repair."""
import copy

from test_support import context_qualifier_selection_policy as policy

f = policy.f
VERSION = policy.VERSION
segments = policy.segments
ITEM_FIELDS = {'position', 'reason', 'subject', 'boundary', 'core', 'qualified', 'limitations'}


def model_input(case, entities):
    draft = case['draft']
    if '⟦' in draft or '⟧' in draft:
        raise ValueError('原文与片段标记冲突，不猜测或改写')
    table = segments(case, entities)
    blocks, current, cursor = [], '', 0
    for sid, part in table.items():
        gap = draft[cursor:part['start']]
        current += gap
        # Punctuation grouping only; neither clause extraction nor truth inference.
        tail = draft[:cursor].rstrip().rstrip('”’"」』')
        if current and (tail.endswith(tuple('。！？!?；;')) or '\n\n' in gap):
            blocks.append(current)
            current = ''
        current += '⟦'+sid+'⟧'+part['quote']
        cursor = part['end']
    current += draft[cursor:]
    if current:
        blocks.append(current)
    return dict(sourceBlocks=blocks, entities=policy.model_input(case, entities)['entities'])


def inspect(response, case, entities):
    result = policy.inspect(response, case, entities)
    fields, scope = [], []
    table = segments(case, entities)
    items = response.get('items') if isinstance(response, dict) else None
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            missing, extra = ITEM_FIELDS-set(item), set(item)-ITEM_FIELDS
            if missing or extra:
                fields.append(dict(itemIndex=index, missing=sorted(missing), extra=sorted(extra)))
            ids = item.get('qualified')
            if isinstance(ids, list) and ids and all(isinstance(s, str) and s in table for s in ids):
                start, end = table[ids[0]]['start'], table[ids[-1]]['end']
                envelope = case['draft'][start:end].rstrip().rstrip(' 。！？!?；;”’"')
                if any(ch in envelope for ch in '。！？!?；;'):
                    scope.append(dict(itemIndex=index, code='qualified_position_crosses_sentence', kind=None))
    result['fieldDiagnostics'] = fields
    result['scopeDiagnostics'] = scope
    if fields:
        result['error'] = '; '.join(
            f"items[{d['itemIndex']}] missing={d['missing']}, extra={d['extra']}" for d in fields)
    # Diagnostics never fill an anchor, delete a limitation, or promote a result.
    result['proposedResponse'] = copy.deepcopy(response)
    return result


def assess(response, case, entities):
    result = inspect(response, case, entities)
    if result['status'] != 'invalid_response':
        result['extractionScore'] = f.assess(result['projection'], case, entities)
        result['status'] = 'matched' if result['extractionScore']['status'] == 'matched' and not result['issues'] else 'mismatched'
    return result
