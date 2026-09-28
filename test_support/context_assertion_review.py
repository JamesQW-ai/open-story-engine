"""Independent triage of frozen quote scores; never a semantic approval gate."""
from collections import Counter
import copy

from test_support import context_assertion_binding_audit as binding
from test_support import context_assertion_quote as contract


def _canonical(response, case):
    table = contract.locator.units(case)

    def span(ref, *, terminal=False):
        resolved = contract.locator.resolve(ref, table, table)
        text = resolved['quote']
        left = len(text) - len(text.lstrip())
        right = len(text.rstrip())
        # Only core/basis punctuation at the actual source-unit end is
        # presentation. Never erase internal punctuation, operators or quotes.
        unit = table[resolved['unitId']]
        unit_end = unit['start'] + len(unit['quote'].rstrip())
        if (terminal and resolved['start'] + right == unit_end
                and text[:right].endswith(('，', '。'))):
            right -= 1
        return [resolved['unitId'], resolved['start'] + left, resolved['start'] + right]

    decisions = Counter()
    for decision in response[contract.VERSION]:
        value = copy.deepcopy(decision)
        if value['resolution'] != 'positions':
            value['basis'] = span(value['basis'], terminal=True)
        else:
            for item in value['items']:
                item['core'] = span(item['core'], terminal=True)
                if item['origin']:
                    for key in ('cue', 'scope', 'subjectMention'):
                        item['origin'][key] = span(item['origin'][key])
                for key in ('conditions', 'modifiers'):
                    for qualifier in item[key]:
                        qualifier['cue'] = span(qualifier['cue'])
                    item[key].sort(key=contract._key)
            # Compare entire items jointly, preserving duplicate multiplicity.
            value['items'].sort(key=contract._key)
        decisions[contract._key(value)] += 1
    return decisions


def review(response, case, entities, reference):
    scored = contract.assess(response, case, entities, reference)
    identity = binding.audit(response, case, entities)
    status = scored['status']
    differences = [key for key, value in scored.get('dimensions', {}).items()
                   if value['missing'] or value['extra']]
    category = status
    if identity['status'] == 'binding_conflict':
        category = 'binding_conflict'
    elif status == 'reference_mismatch':
        if differences == ['ranges']:
            category = ('terminal_punctuation_only' if _canonical(response, case) == _canonical(reference, case)
                        else 'range_review_required')
        elif differences == ['decisions'] and all(
                d['resolution'] in ('absent', 'unresolved')
                for value in (response, reference) for d in value[contract.VERSION]):
            category = 'abstention_policy_review_required'
        else:
            category = 'semantic_review_required'
    return dict(schemaVersion='assertion-review/0.1', originalStatus=status,
                category=category, differingDimensions=differences,
                bindingIssues=identity.get('issues', []), semanticStatus='unverified',
                comparisonScope='frozen_reference_not_semantic_truth', reviewRequired=True,
                acceptance=False, productionEnablement=False)
