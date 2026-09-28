"""Additional offline diagnostics; retain the frozen extraction score unchanged."""
import argparse
import copy
import json
from pathlib import Path

from test_support import context_qualifier_selection_scoped_eval as experiment

OUTPUT = experiment.OUTPUT.with_name('context-qualifier-selection-scoped-scope-audit-2026-09-26.json')


def diagnostics(response):
    """Conservative review triggers, not semantic proof or automatic repair."""
    issues = []
    items = response.get('items') if isinstance(response, dict) else None
    if not isinstance(items, list):
        return issues
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get('position') is None:
            issues.append(dict(itemIndex=index, code='position_interpretation_missing'))
        core = item.get('core')
        limits = item.get('limitations')
        if not isinstance(core, list) or not isinstance(limits, list):
            continue
        for number, limit in enumerate(limits):
            if not isinstance(limit, dict) or limit.get('kind') != 'condition':
                continue
            premise = limit.get('premise')
            if isinstance(premise, list):
                overlap = [sid for sid in core if isinstance(sid, str) and sid in premise]
                if overlap:
                    issues.append(dict(itemIndex=index, limitationIndex=number,
                                       code='condition_premise_overlaps_core', segmentIds=overlap))
    return issues


def audit(source=experiment.OUTPUT):
    # The frozen audit verifies actual messages, raw responses and all input hashes.
    verified = experiment.audit(source)
    report = json.loads(source.read_text())
    rows = []
    for row in report['cases']:
        response = experiment.f.parse_json_content(row['calls'][0]['content'])
        issues = diagnostics(response)
        state = ('prior_failure' if row['status'] != 'matched' else
                 'review_required' if issues else 'no_additional_issue_detected')
        rows.append(dict(caseId=row['caseId'], frozenStatus=row['status'], reviewStatus=state,
                         diagnostics=issues, proposedResponse=copy.deepcopy(response)))
    return dict(schemaVersion='context-qualifier-scope-diagnostics/0.1',
                sourceArtifact=verified['sourceArtifact'], sourceSha256=verified['sourceSha256'],
                implementationSha256=experiment.f.sha(Path(__file__)),
                frozenSummary=verified['summary'], allScoresReproduced=verified['allScoresReproduced'],
                summary={s: sum(r['reviewStatus'] == s for r in rows) for s in
                         ('prior_failure', 'review_required', 'no_additional_issue_detected')},
                cases=rows, newModelCalls=0, semanticStatus='unverified',
                acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    result = audit()
    experiment.f.save_checkpoint(args.output, result, create=True)
    print(json.dumps(result['summary']))
