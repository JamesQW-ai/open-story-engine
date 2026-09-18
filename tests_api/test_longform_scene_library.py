"""Optional art fails closed on every eligible official long novel."""
import copy
import hashlib
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from open_story_engine.api import create_app
from open_story_engine.content import load_runtime_story_package
from open_story_engine.scene_library import SceneLibrary, same_value
from test_support.longform import ROOT, longform_cases
from tests_api.test_longform_image_lifecycle import LongformImageLifecycleTests
from tests_api.test_story_media import PNG


class LongformSceneLibraryTests(unittest.TestCase):
    service = LongformImageLifecycleTests.service

    def setUp(self):
        LongformImageLifecycleTests.setUp(self)
        self.node = self.read.branch_view(self.sid, self.parent)
        self.library = SceneLibrary(Path(self.temp) / 'art')
        self.base = self.library.root / self.case['package_id'] / self.case['version']
        self.base.mkdir(parents=True)
        (self.base / 'scene.png').write_bytes(PNG)
        self.manifest_path = self.base / 'manifest.json'
        self.card = dict(id='scene', file='scene.png', sha256=hashlib.sha256(PNG).hexdigest(),
                         alt='长篇场景故障夹具', location_id=self.node['branchState']['playerLocationId'],
                         review_status='approved', source_evidence=[self.node['narrativeText']],
                         required_text=[re.escape(self.node['narrativeText'][:10])], characters={}, required_state={})
        self.manifest = dict(package_id=self.case['package_id'], version=self.case['version'],
                             source_sha256=self.case['sha256'], assets=[self.card], live_rules=[])
        self.save()

    def save(self):
        self.manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False))

    def resolve(self, node=None):
        return self.library.resolve(self.case['package_id'], self.case['version'], node or self.node)

    def policy(self, node):
        return self.library.live_policy(self.case['package_id'], self.case['version'], node)

    def test_bad_manifest_roots_degrade_without_writes_or_provider_calls(self):
        service, gateway = self.service()
        service.library = self.library
        for raw in (b'{broken', b'null', b'[]', b'1', b'\xff'):
            with self.subTest(raw=raw):
                self.manifest_path.write_bytes(raw)
                self.assertIsNone(self.resolve())
                self.assertIsNone(self.policy(dict(self.node, parentId=self.parent)))
                result = service.ensure(self.sid, self.parent, subscriber='reader', draw=True)
                self.assertFalse(result['can_generate'])
                self.assertEqual(result['items'], [])
                with self.assertRaises(ValueError):
                    self.library.asset(self.case['package_id'], self.case['version'], 'scene')
                self.assertEqual(self.manifest_path.read_bytes(), raw)
        gateway.generate.assert_not_called()

    def test_bad_collections_and_invalid_cards_do_not_hide_valid_fallback(self):
        for value in (None, {}, 'scene', 4):
            self.manifest.update(assets=value, live_rules=value)
            self.save()
            self.assertIsNone(self.resolve())
            self.assertIsNone(self.policy(dict(self.node, parentId=self.parent)))
        bad = [None, [], 'bad', {}, dict(self.card, id='bad-priority', priority='first'),
               dict(self.card, id='nan-priority', priority=float('nan')),
               dict(self.card, id='invalid-pattern', forbidden_text=['[']),
               dict(self.card, id='empty-alt', alt=None), dict(self.card, id='missing-file', file=None)]
        self.manifest.update(assets=bad + [self.card], live_rules=[])
        self.save()
        self.assertEqual(self.resolve()['id'], 'scene')

    def test_missing_and_malformed_conditions_never_match_unknown_state(self):
        changes = [dict(location_id=None), dict(source_evidence='quote'), dict(source_evidence=['']),
                   dict(beats='beat'), dict(perspective_ids={}), dict(required_text=''), dict(forbidden_text=['[']),
                   dict(required_text=['a{999999999999999999999999}']),
                   dict(characters=None), dict(characters={self.cid: None}), dict(required_state=[]),
                   dict(required_state={'itemStates': []}), dict(required_state={'itemStates': {}})]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(self.library.compatible(dict(self.card, **change), self.node))
        missing = copy.deepcopy(self.node)
        missing['branchState'].pop('playerLocationId')
        self.assertFalse(self.library.compatible(dict(self.card, location_id=None), missing))
        for node in (None, [], dict(self.node, branchState=[]), dict(self.node, narrativeText=[])):
            self.assertFalse(self.library.compatible(self.card, node))

    def test_character_status_location_and_appearance_require_explicit_evidence(self):
        node = copy.deepcopy(self.node)
        state = node['branchState']
        state['characterLocationIds'][self.cid] = self.card['location_id']
        state['characterOutcomeStates'] = {self.cid: dict(status='alive', permanence='temporary', evidence=node['narrativeText'])}
        state['characterAppearanceVersions'] = {self.cid: 'current'}
        card = dict(self.card, characters={self.cid: dict(outcome={'status': 'alive'}, appearance_version='current')})
        self.assertTrue(self.library.compatible(card, node))
        for status in ('dead', 'departed', 'missing', 'injured', 'unknown', None):
            changed = copy.deepcopy(node)
            changed['branchState']['characterOutcomeStates'][self.cid]['status'] = status
            self.assertFalse(self.library.compatible(card, changed))
        for field in ('characterLocationIds', 'characterOutcomeStates', 'characterAppearanceVersions'):
            for value in (None, [], {}, {self.cid: None}):
                changed = copy.deepcopy(node)
                changed['branchState'][field] = value
                self.assertFalse(self.library.compatible(card, changed))
        for expected in ({'outcome': {'status': 'alive'}}, {'outcome': {'injury': 'none'}, 'appearance_version': 'current'}):
            self.assertFalse(self.library.compatible(dict(card, characters={self.cid: expected}), node))

    def test_item_constraints_require_current_ownership_and_explicit_fields(self):
        node = copy.deepcopy(self.node)
        package = load_runtime_story_package(self.case['path'], lazy=True)
        item = package['items'][0]['id']
        node['branchState']['itemOwnerCharacterIds'] = {item: self.cid}
        card = dict(self.card, required_state={'itemOwnerCharacterIds': {item: self.cid}})
        self.assertTrue(self.library.compatible(card, node))
        for value in (None, {}, [], {item: None}, {item: 'unknown'}, {item: True}):
            changed = copy.deepcopy(node)
            changed['branchState']['itemOwnerCharacterIds'] = value
            self.assertFalse(self.library.compatible(card, changed))
        card['required_state'] = {'itemOwnerCharacterIds': {item: None}}
        node['branchState']['itemOwnerCharacterIds'] = {}
        self.assertFalse(self.library.compatible(card, node))
        self.assertFalse(same_value({'value': [True]}, {'value': [1]}))
        self.assertFalse(same_value({'value': None}, {}))

    def test_official_reviewed_opening_art_remains_available_and_source_bound(self):
        package = load_runtime_story_package(self.case['path'], lazy=True)
        library = SceneLibrary()
        manifest = library.manifest(self.case['package_id'], self.case['version'])
        for entry in package['story']['entryModel']['entryPoints']:
            with self.subTest(entry=entry['id']):
                declared = [c for c in manifest.get('assets', []) if c.get('scene_id') == entry['id']]
                if declared:
                    result = library.opening(package, entry)
                    self.assertIsNotNone(result)
                    self.assertIn(result['id'], [c['id'] for c in declared])
                changed = copy.deepcopy(package)
                changed['sourceAnalysis']['sha256'] = 'different-source'
                self.assertIsNone(library.opening(changed, entry))

    def test_invalid_live_prompts_never_create_image_tasks_and_valid_rule_survives(self):
        node = dict(self.node, parentId=self.parent)
        invalid = [None, {}, dict(self.card, prompt=None), dict(self.card, prompt=[]), dict(self.card, prompt='  '),
                   dict(self.card, prompt='fixture', required_text=['['])]
        self.manifest.update(assets=[], live_rules=invalid)
        self.save()
        service, gateway = self.service()
        service.library = self.library
        with patch.object(self.read, 'branch_view', return_value=node):
            self.assertFalse(service.ensure(self.sid, self.parent, subscriber='reader', draw=True)['can_generate'])
        self.assertEqual(service._jobs, {})
        gateway.generate.assert_not_called()
        valid = dict(self.card, prompt='只用于长篇规则匹配，不调用供应商')
        self.manifest['live_rules'].append(valid)
        self.save()
        self.assertEqual(self.policy(node), valid)
        self.assertIsNone(self.policy(dict(node, parentId=None)))

    def test_asset_integrity_paths_and_duplicate_ids_fail_closed(self):
        outside = self.base.parent / 'outside.png'
        outside.write_bytes(PNG)
        (self.base / 'link.png').symlink_to(outside)
        (self.base / 'loop.png').symlink_to('loop.png')
        for changes in (dict(file='../outside.png'), dict(file=str(outside)), dict(file='link.png'),
                        dict(file='loop.png'), dict(file='missing.png'), dict(sha256='0' * 64), dict(review_status='draft')):
            self.manifest['assets'] = [dict(self.card, **changes)]
            self.save()
            self.assertIsNone(self.resolve())
            with self.assertRaises((ValueError, OSError)):
                self.library.asset(self.case['package_id'], self.case['version'], 'scene')
        self.manifest['assets'] = [self.card, dict(self.card, file='missing.png')]
        self.save()
        self.assertIsNone(self.resolve())
        with self.assertRaises(ValueError):
            self.library.asset(self.case['package_id'], self.case['version'], 'scene')

    def test_http_reading_survives_bad_library_and_assets_return_404(self):
        self.manifest_path.write_text('[]')
        before = self.read.branch_view(self.sid, self.parent)
        with patch('open_story_engine.api_illustrations.SceneLibrary', return_value=self.library):
            with TestClient(create_app(ROOT / 'content/packages', self.read.database_path, play=True)) as client:
                base = f'/api/v1/sessions/{self.sid}/branches/{self.parent}'
                self.assertEqual(client.get(base).status_code, 200)
                result = client.get(base + '/illustrations')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['items'], [])
                self.assertEqual(client.get(f"/api/v1/scene-assets/{self.case['package_id']}/{self.case['version']}/scene").status_code, 404)
        self.assertEqual(self.read.branch_view(self.sid, self.parent), before)
        self.assertEqual(self.manifest_path.read_text(), '[]')


def load_tests(loader, _tests, _pattern):
    suite = unittest.TestSuite()
    for case in longform_cases():
        suite.addTests(loader.loadTestsFromTestCase(type('LongformSceneLibrary_' + case['package_id'],
                                                        (LongformSceneLibraryTests,), {'case': case})))
    return suite
