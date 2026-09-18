"""Every available long novel, every chapter and every declared identity."""
import hashlib
import json
import unittest

from test_support.longform import CJK, longform_cases
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node
from open_story_engine.package_builder import audit_story_package_modules, read_story_package_modules


class LongformCorpusTests(unittest.TestCase):
    def test_all_books_bind_complete_reader_and_modules_to_frozen_source(self):
        for case in longform_cases():
            with self.subTest(book=case['package_id']):
                directory = case['path'].parent
                package = json.loads(case['path'].read_text())
                analysis = json.loads((directory / 'analysis.json').read_text())
                reader = json.loads((directory / 'reader.json').read_text())
                source = case['source'].read_text()
                self.assertEqual(hashlib.sha256(case['source'].read_bytes()).hexdigest(), reader['source']['sha256'])
                self.assertEqual(package['sourceAnalysis']['sha256'], reader['source']['sha256'])
                self.assertGreaterEqual(case['cjk'], 100000)
                self.assertGreaterEqual(sum(len(CJK.findall(c['text'])) for c in reader['chapters']), 100000)
                lines = source.splitlines()
                for chapter in reader['chapters']:
                    with self.subTest(chapter=chapter['id']):
                        bounds = chapter['lineRange']
                        self.assertEqual(chapter['text'].strip(), '\n'.join(lines[bounds['start']-1:bounds['end']]).strip())
                audit = audit_story_package_modules(case['source'], analysis, package, reader,
                                                     read_story_package_modules(directory / 'modules'))
                self.assertEqual(audit['status'], 'passed', audit['issues'])

    def test_all_books_all_identities_have_independent_public_opening_ledgers(self):
        for case in longform_cases():
            package = load_runtime_story_package(case['path'], lazy=True)
            model = package['story']['entryModel']
            for cid in model['sourceCharacterIds']:
                with self.subTest(book=case['package_id'], character=cid):
                    contract = create_contract(package, 'corpus-check', dict(kind='source_character', sourceCharacterId=cid))
                    root = entry_node(package, contract)
                    entry = next(e for e in model['entryPoints'] if e['id'] == contract['entryPointId'])
                    self.assertEqual([t['title'] for t in root['branchState']['threadLedger']], entry['openingThreads'])
                    self.assertTrue(all(t['status'] == 'open' for t in root['branchState']['threadLedger']))
                    self.assertTrue(root['narrativeText'])
                    original = entry_node(package, contract)['branchState']['threadLedger']
                    root['branchState']['threadLedger'].clear()
                    self.assertEqual(entry_node(package, contract)['branchState']['threadLedger'], original)
