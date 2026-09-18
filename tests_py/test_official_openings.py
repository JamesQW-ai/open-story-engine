"""Official role selection and spoiler boundaries against the frozen long novel."""
import copy
import json
import unittest
from unittest.mock import patch
from pathlib import Path

from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node, normalize_entry_selection
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.official_openings import apply_opening_review, validate_official_openings
from open_story_engine.package_builder import audit_story_package_modules, read_story_package_modules
from open_story_engine.cli import choose_co_creation_entry

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'content/packages/taixu-relics-part1/0.1.3'
SOURCE = ROOT / 'docs/太虚遗录-第一部/太虚遗录-第一部-合并.txt'


class OfficialOpeningTests(unittest.TestCase):
    def setUp(self):
        self.package = load_runtime_story_package(DIRECTORY / 'package.json', lazy=True)

    def test_all_roles_are_bound_even_when_legacy_bypass_is_requested(self):
        characters = self.package['story']['entryModel']['sourceCharacterIds']
        for character in characters:
            selection = normalize_entry_selection(self.package, {'kind': 'source_character', 'sourceCharacterId': character}, True)
            for other in characters:
                if character != other:
                    with self.assertRaises(ValueError):
                        normalize_entry_selection(self.package, {**selection, 'sourceCharacterId': other}, True)
        for character in ('character_aa4580d38571', 'character_37531636ecf5', 'character_b96c85e62843', 'character_2cf2b083593b'):
            selection = normalize_entry_selection(self.package, {'kind': 'source_character', 'sourceCharacterId': character}, True)
            self.assertEqual(selection['sourceCharacterId'], character)
        with self.assertRaises(ValueError):
            normalize_entry_selection(self.package, {'kind': 'new_character', 'name': '新来者'}, True)

    def test_cli_selects_identity_without_a_second_scene_question(self):
        with patch('builtins.input', return_value='2') as user_input:
            selection = choose_co_creation_entry(self.package)
        self.assertEqual(selection['entryPointId'], 'entry_gu_trial')
        user_input.assert_called_once_with('身份 > ')

    def test_role_snapshots_and_first_turn_context_do_not_use_future_cards(self):
        expected = {
            'character_ae4cb42b9b49': ('location_open_gate', {'item_open_letter'}),
            'character_e663361ab1c7': ('location_open_trial', {'item_open_gu_token', 'item_open_gold_paper'}),
            'character_766c4e16f65d': ('location_open_gallery', set()),
            'character_aa4580d38571': ('location_open_registry', {'item_open_registry_book'}),
            'character_37531636ecf5': ('location_open_inscription', set()),
            'character_b96c85e62843': ('location_open_edict_square', {'item_open_gold_edict'}),
            'character_2cf2b083593b': ('location_open_ancestral_hall', {'item_open_broken_sword'}),
        }
        for character, (location, held) in expected.items():
            selection = {'kind': 'source_character', 'sourceCharacterId': character}
            contract = create_contract(self.package, 'test', selection, True)
            root = entry_node(self.package, contract, True)
            state = root['branchState']
            self.assertEqual(state['playerLocationId'], location)
            self.assertEqual(set(state['itemOwnerCharacterIds']), held)
            self.assertEqual(state['characterOutcomeStates'], {})
            resolver = ModuleContextResolver.for_package(DIRECTORY / 'package.json', self.package)
            prompt = resolver.resolve({'contract': contract, 'parent': root, 'lineage': [root]}, {'statePatch': {}}, state)
            self.assertFalse(any(p.startswith(('reader/', 'beats/', 'characters/')) for p in prompt['modulePaths']))
            self.assertEqual(prompt['characters'][0]['id'], character)
            self.assertEqual(prompt['openingContext'], contract['openingContext'])
            # A chapter-level excerpt must not import another viewpoint's past.
            self.assertEqual(prompt['narrativeBrief'], [])
            state['characterOutcomeStates']['fake'] = {'status': 'dead'}
            self.assertEqual(entry_node(self.package, contract)['branchState']['characterOutcomeStates'], {})

    def test_compiler_rejects_changed_source_or_future_evidence(self):
        package = json.loads((DIRECTORY / 'package.json').read_text())
        review = json.loads((DIRECTORY / 'opening-review.json').read_text())
        review['sourceSha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, '冻结母本'):
            apply_opening_review(package, SOURCE, review)
        review = json.loads((DIRECTORY / 'opening-review.json').read_text())
        review['openings'][0]['context']['evidence'].append('“听过北河城，不认识你。”')
        base = json.loads((DIRECTORY.parent / '0.1.0/package.json').read_text())
        with self.assertRaisesRegex(ValueError, '越过截止点'):
            apply_opening_review(base, SOURCE, review)
        package['story']['entryModel']['entryPoints'][0]['openingState']['itemOwnerCharacterIds']['nonexistent'] = 'character_ae4cb42b9b49'
        with self.assertRaisesRegex(ValueError, '归属'):
            validate_official_openings(package)

    def test_reader_modules_and_audits_are_bound_to_one_source(self):
        package = json.loads((DIRECTORY / 'package.json').read_text())
        analysis = json.loads((DIRECTORY / 'analysis.json').read_text())
        reader = json.loads((DIRECTORY / 'reader.json').read_text())
        modules = read_story_package_modules(DIRECTORY / 'modules')
        audit = audit_story_package_modules(SOURCE, analysis, package, reader, modules)
        self.assertEqual(audit['status'], 'passed', audit['issues'])
        self.assertEqual(len(reader['chapters']), 50)
        corrupted = copy.deepcopy(modules)
        corrupted['files']['entries/entry_gu_trial.json']['entryPoint']['openingState']['playerLocationId'] = 'location_open_gate'
        self.assertEqual(audit_story_package_modules(SOURCE, analysis, package, reader, corrupted)['status'], 'failed')
