"""Fact-review contracts against excerpts from every verified long novel."""
import copy
import json
import re
import unittest

from test_support.longform import longform_cases
from open_story_engine.cocreation import LlmPlanner
from open_story_engine.content import load_runtime_story_package
from open_story_engine.llm import LlmError


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def review(body):
    return {'checks': [dict(paragraphId=p['id'], status='non_factual', evidenceIds=[], reason='待独立复核')
                       for p in LlmPlanner._draft_paragraphs(body)]}


class LongformFactRepairTests(unittest.TestCase):
    def setUp(self):
        reader = json.loads((self.case['path'].parent / 'reader.json').read_text())
        paragraphs = [p.strip() for p in reader['chapters'][0]['text'].splitlines() if 60 <= len(p.strip()) <= 180]
        self.assertGreaterEqual(len(paragraphs), 3, '首章需要三个完整原文段落作为测试素材')
        self.first, self.middle, self.last = paragraphs[:3]
        self.body = '\n\n'.join(paragraphs[:3])
        package = load_runtime_story_package(self.case['path'], lazy=True)
        cid = package['story']['entryModel']['sourceCharacterIds'][0]
        self.name = next(c['name'] for c in package['characters'] if c['id'] == cid)
        # Deliberately injected unsupported history; never part of the frozen novel.
        self.bad = self.name + '说：“昨天我已把所有秘册逐页核对过三遍。”'
        self.fixed = self.name + '说：“我不知道。”'

    def test_support_references_bind_exact_source_and_owner(self):
        text = self.first
        source = {'id': 'e1', 'kind': 'sourceSceneCues', 'value': {self.name: text}}
        claims = LlmPlanner._referenced_fact_spans(text, 'p1')
        spans = LlmPlanner._referenced_fact_spans(source['value'], 'e1')
        supports = [dict(claimRef=c['ref'], evidenceRef=e['ref']) for c, e in zip(claims, spans)]
        check = dict(paragraphId=1, status='supported', evidenceIds=['e1'], reason='逐句对应原文', supports=supports)
        LlmPlanner._check_fact_review(encoded({'checks': [check]}), text, {'e1'}, [source])
        other_owner = LlmPlanner._referenced_fact_spans({'另一人物': text}, 'e1')[0]['ref']
        for field, value in [('claimRef', True), ('claimRef', 'p2:missing'), ('evidenceRef', 'e404:missing'), ('evidenceRef', other_owner)]:
            broken = copy.deepcopy(check)
            broken['supports'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(LlmError):
                LlmPlanner._check_fact_review(encoded({'checks': [broken]}), text, {'e1'}, [source])

    def test_review_requires_all_paragraphs_and_registered_evidence(self):
        valid = review(self.body)
        LlmPlanner._check_fact_review(encoded(valid), self.body, set())
        cases = [{'checks': valid['checks'][:2]}, {'checks': [valid['checks'][0]] * 3}]
        for change in ({'paragraphId': True}, {'status': 'supported', 'evidenceIds': ['e404']},
                       {'status': 'supported', 'evidenceIds': []}, {'status': 'conflict', 'quote': '正文并不存在的引文'}):
            changed = copy.deepcopy(valid)
            changed['checks'][1].update(change)
            cases.append(changed)
        for value in cases:
            with self.subTest(review=value), self.assertRaises(LlmError):
                LlmPlanner._check_fact_review(encoded(value), self.body, set())

    def test_independent_review_catches_first_review_omission(self):
        body = self.first + '\n\n' + self.bad + '\n\n' + self.last
        initial = encoded(review(body))
        # The first review deliberately overlooks the injected assertion.
        LlmPlanner._check_fact_review(initial, body, set())
        checks = [dict(paragraphId=i, status='allowed') for i in (1, 2, 3)]
        checks[1].update(status='conflict', quotes=[self.bad], reason='原文与分支都没有核对秘册的经历')
        with self.assertRaisesRegex(ValueError, '事实核对未通过'):
            LlmPlanner._check_fact_support_review(encoded({'checks': checks}), initial, body, [])
        for value in [checks[:2], [checks[0], checks[0], checks[2]]]:
            with self.assertRaises(LlmError):
                LlmPlanner._check_fact_support_review(encoded({'checks': value}), initial, body, [])

    def test_format_repair_preserves_conflicts_and_only_fills_requested_records(self):
        body = self.first + '\n\n' + self.bad + '\n\n' + self.last
        original = review(body)
        original['checks'][1].update(status='conflict', quotes=[self.bad], reason='无经历依据')
        merged = json.loads(LlmPlanner._merge_fact_review_records(encoded(original), encoded({'checks': [original['checks'][0]]}), [1]))
        self.assertEqual(merged, original)
        for replacement in [[], [original['checks'][2]], [original['checks'][0]] * 2]:
            with self.assertRaises(LlmError):
                LlmPlanner._merge_fact_review_records(encoded(original), encoded({'checks': replacement}), [1])
        with self.assertRaisesRegex(LlmError, '不能撤销'):
            LlmPlanner._merge_fact_review_records(encoded(original), encoded({'checks': [review(body)['checks'][1]]}), [2])
        incomplete = {'checks': [original['checks'][1]]}
        repaired = {'checks': [original['checks'][0], original['checks'][2]]}
        self.assertEqual(json.loads(LlmPlanner._merge_fact_review_records(encoded(incomplete), encoded(repaired), [1, 3])), original)

    def test_local_replacement_preserves_source_neighbors_and_rejects_invalid_edits(self):
        body = self.first + '\n\n' + self.bad + '\n \n' + self.last
        def apply(edits):
            return LlmPlanner._apply_fact_replacements(body, encoded({'replacements': edits}))
        self.assertEqual(apply([dict(paragraphId=2, text=self.fixed)]), self.first + '\n\n' + self.fixed + '\n \n' + self.last)
        for edits in [[dict(paragraphId=i, text=self.fixed)] for i in (True, 0, 4)] + [
            [dict(paragraphId=2, text=t)] for t in ('', self.bad, '⟦REPAIR_GAP_1⟧', '[此处缺少事实依据：测试]',
                                                 '[此处存在审查问题：测试]', self.fixed + '\n\n' + self.last)
        ] + [[dict(paragraphId=2, text=self.fixed)] * 2]:
            with self.subTest(edits=edits), self.assertRaises(LlmError):
                apply(edits)
        for marker in ('⟦REPAIR_GAP_1⟧', '[此处缺少事实依据：测试]', '[此处存在审查问题：测试]'):
            with self.subTest(unedited_marker=marker), self.assertRaises(LlmError):
                LlmPlanner._apply_fact_replacements(body + marker, encoded({'replacements': [dict(paragraphId=2, text=self.fixed)]}))

    def test_fixing_one_issue_cannot_keep_another_or_move_it(self):
        claim = LlmPlanner._fact_claim_text(self.bad)
        conflicts = [dict(paragraphId=2, claims=[claim])]
        good = self.first + '\n\n' + self.fixed + '\n\n' + self.last
        LlmPlanner._validate_fact_repair(good, conflicts)
        for body in (self.first + '\n\n' + self.bad + '\n\n' + self.last,
                     self.first + self.bad + '\n\n' + self.fixed + '\n\n' + self.last):
            with self.assertRaisesRegex(LlmError, '仍保留'):
                LlmPlanner._validate_fact_repair(body, conflicts)

    def test_repair_budget_rejects_whole_chapter_rewrite(self):
        LlmPlanner._check_fact_repair_size(self.body, [2])
        with self.assertRaisesRegex(LlmError, '超出局部修改范围'):
            LlmPlanner._check_fact_repair_size(self.body, [1, 2, 3])

    def test_exact_neighbor_echo_removed_without_changing_original(self):
        body = self.first + '\n\n' + self.bad + '\n\n' + self.last
        result = LlmPlanner._apply_fact_replacements(body, encoded({'replacements': [dict(paragraphId=2, text=self.first + self.fixed)]}))
        self.assertEqual(result, self.first + '\n\n' + self.fixed + '\n\n' + self.last)
        with self.assertRaisesRegex(LlmError, '只重复了相邻'):
            LlmPlanner._apply_fact_replacements(body, encoded({'replacements': [dict(paragraphId=2, text=self.first)]}))

    def test_quote_localization_does_not_drop_negation_or_reorder_source(self):
        sentences = [s for s in re.split('[。！？]', self.first) if len(s) > 3]
        self.assertGreaterEqual(len(sentences), 2)
        quote = sentences[0] + '。' + sentences[1]
        self.assertTrue(LlmPlanner._locate_fact_claims([quote], self.first))
        for bad in ('并没有' + quote, sentences[1] + '。' + sentences[0], quote + '从未登记的秘册经历'):
            with self.subTest(quote=bad), self.assertRaises(LlmError):
                LlmPlanner._locate_fact_claims([bad], self.first)


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformFacts_' + case['package_id'], (LongformFactRepairTests,), {'case': case})))
    return suite
