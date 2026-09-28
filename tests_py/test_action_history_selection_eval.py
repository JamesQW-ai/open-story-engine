"""Check scoring and gold isolation, in addition to the frozen controls."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_support.action_history_selection_eval import FIXTURE, aggregate, evaluate, score, validate_labels


class ActionHistoryEvaluationTests(unittest.TestCase):
    def test_score_counts_missed_and_extra_sources_separately(self):
        result = score(['required-a', 'required-b'], ['required-a', 'extra'])
        self.assertEqual((result['truePositive'], result['falsePositive'], result['falseNegative']), (1, 1, 1))
        self.assertEqual(result['missing'], ['required-b'])
        self.assertEqual(result['unexpected'], ['extra'])
        self.assertFalse(result['exactMatch'])
        combined = aggregate([result, score([], [])])
        self.assertEqual(combined['precision'], .5)
        self.assertEqual(combined['recall'], .5)
        self.assertEqual(combined['matched'], 1)
        self.assertIsNone(aggregate([])['recall'])
        self.assertIsNone(aggregate([])['precision'])

    def test_fixed_controls_reproduce_without_semantic_acceptance(self):
        report = evaluate()
        self.assertEqual(report, evaluate())
        self.assertEqual(report['summary']['history']['matched'], 12)
        self.assertEqual(report['summary']['memory']['matched'], 8)
        self.assertTrue(all(row['parentRetained'] for row in report['history']))
        self.assertEqual(report['newModelCalls'], 0)
        self.assertEqual(report['formalSessionWrites'], 0)
        self.assertFalse(report['semanticAcceptance'])

    def test_changing_gold_does_not_change_selected_sources_or_context(self):
        before = evaluate()
        labels = json.loads(FIXTURE.read_text())
        for row in labels['historyCases']:
            row['expectedPrior'] = not row['expectedPrior']
        for row in labels['memoryCases']:
            row['expectedCurrent'] = not row['expectedCurrent']
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'reversed-labels.json'
            path.write_text(json.dumps(labels, ensure_ascii=False))
            after = evaluate(path)
        for section in ('history', 'memory'):
            self.assertEqual([r['selectedSources'] for r in before[section]],
                             [r['selectedSources'] for r in after[section]])
            self.assertEqual([r['audit'] for r in before[section]], [r['audit'] for r in after[section]])
            self.assertEqual(after['summary'][section]['matched'], 0)

    def test_labels_reject_empty_duplicate_and_nonboolean_expectations(self):
        original = json.loads(FIXTURE.read_text())
        variants = []
        empty = copy.deepcopy(original)
        empty['historyCases'] = []
        variants.append(empty)
        duplicate = copy.deepcopy(original)
        duplicate['memoryCases'][0]['id'] = duplicate['historyCases'][0]['id']
        variants.append(duplicate)
        bad_bool = copy.deepcopy(original)
        bad_bool['historyCases'][0]['expectedPrior'] = 'false'
        variants.append(bad_bool)
        for labels in variants:
            with self.assertRaises(ValueError):
                validate_labels(labels)

    def test_changed_official_quote_fails_instead_of_silently_relabeling(self):
        labels = json.loads(FIXTURE.read_text())
        labels['historyCases'][0]['quote'] = '此句不在官方开场'
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'wrong-quote.json'
            path.write_text(json.dumps(labels, ensure_ascii=False))
            with self.assertRaisesRegex(ValueError, '引用已不在官方开场'):
                evaluate(path)

    def test_stress_controls_distinguish_reference_omission_from_absent_evidence(self):
        report = evaluate(FIXTURE.with_name('action-history-stress-2026-09-27.json'))
        rows = {row['caseId']: row for row in report['history']}
        for name in ('module-explicit-document', 'module-explicit-father'):
            self.assertTrue(rows[name]['exactMatch'])
            self.assertEqual(rows[name]['missingFromFullPublicEvidence'], [])
        self.assertEqual(rows['paraphrase-document']['falseNegative'], 1)
        self.assertEqual(rows['paraphrase-document']['missingFromFullPublicEvidence'], [])
        self.assertEqual(rows['gap-one-document']['missingFromFullPublicEvidence'],
                         ['branch:lineage:control-prior'])
        self.assertEqual(rows['homonym-clerk-document']['falsePositive'], 1)
        self.assertEqual(report['summary']['history']['cases'], 14)
        # Current known misses are reported as failures, not silently counted
        # as semantic acceptance because the harness itself ran successfully.
        self.assertFalse(report['semanticAcceptance'])
        self.assertLess(report['summary']['history']['matched'], 14)

    def test_stress_gold_isolation_and_intervening_page_validation(self):
        fixture = FIXTURE.with_name('action-history-stress-2026-09-27.json')
        labels = json.loads(fixture.read_text())
        before = evaluate(fixture)
        for row in labels['historyCases']:
            row['expectedPrior'] = not row['expectedPrior']
            row['reason'] = '反转标签控制，不传入运行时。'
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'reversed-stress.json'
            path.write_text(json.dumps(labels, ensure_ascii=False))
            after = evaluate(path)
        self.assertEqual([r['audit'] for r in before['history']], [r['audit'] for r in after['history']])
        for invalid in ('not-a-list', [''], [False], ['雨势未变。'] * 9):
            labels['historyCases'][0]['between'] = invalid
            with self.assertRaisesRegex(ValueError, '中间页摘要'):
                validate_labels(labels)
