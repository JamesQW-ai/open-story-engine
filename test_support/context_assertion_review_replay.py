"""Reproduce independent review categories without changing frozen experiment scores."""
import argparse
from collections import Counter
import json
from pathlib import Path

from test_support import context_assertion_iteration_report as iterations
from test_support import context_assertion_review as review

original = iterations.original
f = original.f
OUTPUT = original.OUTPUT.with_name('context-assertion-review-replay-2026-09-27.json')
AUDIT = OUTPUT.with_name('context-assertion-review-replay-audit-2026-09-27.json')


def build():
    # Verify all five frozen sources, audits, prompts and code before triage.
    iterations.build()
    fixture, references, _ = original.prepare()
    cases = {c['id']: c for c in fixture['cases']}
    references = {r['caseId']: r['reference'] for r in references}
    rows = []
    for source in iterations.SOURCES:
        report = json.loads((f.ROOT / source['path']).read_text())
        for row in report['cases']:
            c = cases[row['caseId']]
            result = dict(originalStatus=row['status'], category=row['status'],
                          semanticStatus='unverified', reviewRequired=True,
                          acceptance=False, productionEnablement=False)
            if 'proposedResponse' in row:
                result = review.review(row['proposedResponse'], c,
                                       fixture['scenes'][c['sceneKey']], references[c['id']])
                if result['originalStatus'] != row['status']:
                    raise ValueError('原评分不可复现：' + c['id'])
            elif row['status'] not in ('invalid_json', 'model_error', 'not_run'):
                raise ValueError('缺少可复核响应：' + c['id'])
            rows.append(dict(source=source['name'], caseId=c['id'], review=result))
    inputs = {str(p.relative_to(f.ROOT)): f.sha(p) for p in (
        Path(__file__), Path(review.__file__), Path(review.binding.__file__),
        Path(iterations.__file__))}
    summary = {}
    for source in iterations.SOURCES:
        selected = [r for r in rows if r['source'] == source['name']]
        summary[source['name']] = {
            cohort: dict(Counter(r['review']['category'] for r in selected
                                if cohort == 'all' or r['caseId'].startswith('design:') == (cohort == 'design')))
            for cohort in ('all', 'original', 'design')}
    return dict(schemaVersion='assertion-review-replay/0.1', sources=iterations.SOURCES,
                inputHashes=inputs, cases=rows, summary=summary, newModelCalls=0,
                acceptance=False, productionEnablement=False, historicalScoresChanged=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    args = parser.parse_args()
    result = build()
    if args.audit:
        if json.loads(OUTPUT.read_text()) != result:
            raise ValueError('独立复核不可复现')
        result = dict(sourceSha256=f.sha(OUTPUT), allReviewsReproduced=True,
                      newModelCalls=0, acceptance=False, productionEnablement=False)
    f.save_checkpoint(AUDIT if args.audit else OUTPUT, result, create=True)
    print(json.dumps(result.get('summary', result), ensure_ascii=False))
