import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_support.export_delivery_demo import build_record, sha, standalone_html
from test_support.longform import ROOT, longform_cases


class DeliveryDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name)
        original = ROOT / 'docs/evidence/delivery-2026-09-27/attempt-04'
        for name in ('report.json', 'opening.json', 'turn-1.json', 'turn-2.json', 'turn-3.json'):
            value = json.loads((original / name).read_text())
            if name == 'report.json':
                value.pop('calls')  # Provider transcripts do not enter public exports.
            (self.evidence / name).write_text(json.dumps(value, ensure_ascii=False))
        self.review = json.loads((original / 'prose-review.json').read_text())

    def change(self, name, mutation):
        path = self.evidence / name
        value = json.loads(path.read_text())
        mutation(value)
        path.write_text(json.dumps(value, ensure_ascii=False))

    def test_published_record_matches_reviewed_longform_run(self):
        result = build_record(self.evidence, self.review)
        published = json.loads((ROOT / 'web/src/data/deliveryDemo.json').read_text())
        result['sourceRun'] = published['sourceRun']
        self.assertEqual(result, published)
        self.assertTrue(any(c['package_id'] == result['packageId'] and c['sha256'] == result['novelSha256']
                            for c in longform_cases()))
        self.assertEqual(len(result['chapters']), 4)
        self.assertEqual(standalone_html(result), (ROOT / 'docs/evidence/delivery-2026-09-27/demo-offline.html').read_text())

    def test_partial_run_cannot_be_presented_as_complete(self):
        self.change('report.json', lambda value: value.update(status='failed'))
        with self.assertRaises(ValueError):
            build_record(self.evidence, self.review)

    def test_rewritten_prose_cannot_reuse_review(self):
        self.change('opening.json', lambda value: value['branch'].update(narrativeText='已被改写'))
        with self.assertRaises(ValueError):
            build_record(self.evidence, self.review)

    def test_manual_approval_cannot_replace_runtime_review(self):
        value = json.loads((self.evidence / 'turn-1.json').read_text())
        node = value['branch']
        node['narrativeText'] += '\n事后新增正文。'
        self.review['narrativeHashes'][node['id']] = sha(node['narrativeText'])
        (self.evidence / 'turn-1.json').write_text(json.dumps(value))
        self.change('report.json', lambda report: report['turns'][0].update(prose=node['narrativeText']))
        with self.assertRaises(ValueError):
            build_record(self.evidence, self.review)

    def test_disconnected_turn_and_legacy_fallback_are_rejected(self):
        for change in ({'parentId': 'unrelated-branch'}, {'fallbackMode': 'candidate_selected_pending_patch'}):
            with self.subTest(change=change):
                original = (self.evidence / 'turn-2.json').read_text()
                self.change('turn-2.json', lambda value: value['branch'].update(change))
                with self.assertRaises(ValueError):
                    build_record(self.evidence, self.review)
                (self.evidence / 'turn-2.json').write_text(original)

    def test_unreviewed_record_cannot_be_exported(self):
        review = copy.deepcopy(self.review)
        review['decision'] = 'pending'
        with self.assertRaises(ValueError):
            build_record(self.evidence, review)


if __name__ == '__main__':
    unittest.main()
