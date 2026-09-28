"""Report exact registered-name contradictions; never approve semantic bindings."""
from test_support import context_assertion_quote as contract


def audit(response,case,entities):
    checked=contract.inspect(response,case,entities)
    result=dict(schemaVersion='assertion-binding-audit/0.1',issues=[],
        semanticStatus='unverified',acceptance=False,productionEnablement=False)
    if checked['status']=='invalid_response':
        return dict(result,status='invalid_source',error=checked['error'])
    data=contract.model_input(case,entities);table=contract.locator.units(case)

    def identities(quote):
        return {key for key,entity in entities.items()
                if entity['kind']=='scene_person' and quote in entity['mentions']}

    for d in response[contract.VERSION]:
        subject=contract.locator.resolve(data['entityRefs'][d['subject']]['anchor'],table,table)
        expected=identities(subject['quote'])
        for index,item in enumerate(d.get('items',[])):
            if item['origin'] is None:continue
            mention=contract.locator.resolve(item['origin']['subjectMention'],table,table)
            named=identities(mention['quote'])
            # Exact unique names only. Pronouns, containing phrases, and
            # ambiguous aliases retain unverified status, never approval.
            if len(expected)==len(named)==1 and expected!=named:
                result['issues'].append(dict(code='registered_subject_mismatch',
                    subject=d['subject'],itemIndex=index,subjectAnchor=subject,
                    subjectMention=mention,expectedEntity=next(iter(expected)),
                    mentionedEntity=next(iter(named))))
    return dict(result,status='binding_conflict' if result['issues'] else 'unverified')
