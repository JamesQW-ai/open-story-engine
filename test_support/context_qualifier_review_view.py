"""Evidence-only review projection; model explanations never become facts."""
import copy

from test_support import context_qualifier_unit_premises as extraction


def build(response, case, entities):
    # Revalidate the raw response, not a caller-supplied score or approval.
    checked = extraction.inspect(response, case, entities)
    view = dict(schemaVersion='qualifier-review-view/0.1',
                validationStatus=checked['status'], semanticStatus='unverified',
                reviewRequired=True, productionEnablement=False,
                explanationSource='validated_source_spans', items=[],
                issues=copy.deepcopy(checked.get('issues', [])))
    if checked['status'] != 'structurally_valid':
        view['error'] = checked.get('error', '原文引用或结构校验未通过')
        return view
    for item, evidence in zip(checked['decodedResponse']['items'], checked['evidence']):
        view['items'].append(dict(candidatePosition=copy.deepcopy(item['position']),
                                  sourceEvidence=copy.deepcopy(evidence)))
    return view
