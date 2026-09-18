"""Public compatibility, private leases, cancellation and spend boundaries."""
import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import Mock

from open_story_engine.api_illustrations import IllustrationService
from open_story_engine.scene_library import SceneLibrary
from tests_api.test_story_media import PNG, media_service


class SceneLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'book' / '0.1.0'
        self.base.mkdir(parents=True)
        (self.base / 'scene.png').write_bytes(PNG)
        self.card = dict(id='scene', file='scene.png', sha256=hashlib.sha256(PNG).hexdigest(),
                         review_status='approved', source_evidence=['source quote'], location_id='gate',
                         alt='scene', required_text=['stone'], forbidden_text=['destroyed'], characters={}, required_state={})
        self.node = dict(parentId='parent', narrativeText='stone in rain', canonicalBeatId='beat',
                         branchState=dict(playerLocationId='gate', playerCharacterId='lu',
                                          characterLocationIds={'gu': 'gate'}, characterOutcomeStates={}, itemOwnerCharacterIds={}))
        self.library = SceneLibrary(self.root)
        self.write_manifest()

    def write_manifest(self):
        (self.base / 'manifest.json').write_text(json.dumps(dict(package_id='book', version='0.1.0', assets=[self.card])))

    def test_offline_cross_session_public_asset_and_state_incompatibility(self):
        read, gateway = Mock(), Mock(available=False)
        read.branch_view.return_value = self.node
        service = media_service(read, self.root / 'private', gateway)
        service.library = self.library
        read.session.return_value = {'storyPackageId': 'book', 'storyPackageVersion': '0.1.0'}
        self.addCleanup(service.close)
        a = service.ensure('one', 'b', subscriber='tab')
        b = service.ensure('two', 'other', subscriber='tab')
        self.assertEqual(a['items'][0]['url'], b['items'][0]['url'])
        gateway.generate.assert_not_called()
        self.assertFalse(a['can_generate'])
        read.branch_view.return_value = dict(self.node, narrativeText='stone destroyed')
        self.assertEqual(service.view('one', 'b')['items'], [])
        self.assertIsNone(self.library.resolve('book', '0.1.1', self.node))

    def test_same_beat_does_not_override_unknown_dead_departed_or_injured_character(self):
        self.card['beats'] = ['beat']
        self.card['characters'] = {'gu': {'outcome': {'status': 'alive', 'injury': 'none'}, 'appearance_version': 'gu-v1'}}
        self.card['required_state'] = {'itemOwnerCharacterIds': {'token': 'gu'}}
        original = copy.deepcopy(self.node)
        self.assertFalse(self.library.compatible(self.card, self.node))
        state = self.node['branchState']
        state['characterOutcomeStates']['gu'] = {'status': 'alive', 'injury': 'none'}
        state['characterAppearanceVersions'] = {'gu': 'gu-v1'}
        state['itemOwnerCharacterIds']['token'] = 'gu'
        self.assertTrue(self.library.compatible(self.card, self.node))
        for outcome in ({'status': 'dead'}, {'status': 'departed'}, {'status': 'alive', 'injury': 'arm'}):
            state['characterOutcomeStates']['gu'] = outcome
            self.assertFalse(self.library.compatible(self.card, self.node))
            self.assertEqual(state['characterOutcomeStates']['gu'], outcome)
        state['characterOutcomeStates']['gu'] = {'status': 'alive', 'injury': 'none'}
        state['characterLocationIds']['gu'] = 'elsewhere'
        self.assertFalse(self.library.compatible(self.card, self.node))
        self.assertIsNone(self.library.resolve('book', '0.1.1', original))

    def test_integrity_and_unpublished_assets_fail_closed(self):
        self.assertIsNotNone(self.library.resolve('book', '0.1.0', self.node))
        (self.base / 'scene.png').write_bytes(b'changed')
        self.assertIsNone(self.library.resolve('book', '0.1.0', self.node))
        with self.assertRaises(ValueError): self.library.asset('book', '0.1.0', 'missing')
        self.card['review_status'] = 'draft'
        self.write_manifest()
        self.assertIsNone(self.library.resolve('book', '0.1.0', self.node))

    def test_compatibility_can_use_scene_lineage_without_changing_current_prose(self):
        node = dict(self.node, narrativeText='the current turn only mentions rain',
                    sceneNarrativeText='stone in rain; the established scene remains visible')
        self.assertTrue(self.library.compatible(self.card, node))

    def test_opening_art_uses_official_entry_source_and_state(self):
        from open_story_engine.official_openings import POLICY
        package = dict(id='book', version='0.1.0', sourceAnalysis={'sha256': 'source'},
                       characters=[{'id': 'lu', 'defaultEntryPointId': 'entry'}],
                       story={'entryModel': {'policy': POLICY}})
        entry = dict(id='entry', beatId='beat', sourceCharacterIds=['lu'],
                     openingState=self.node['branchState'], sourceCharacterNarratives={'lu': 'stone in rain'})
        self.card['scene_id'] = 'entry'
        manifest = dict(package_id='book', version='0.1.0', source_sha256='source', assets=[self.card])
        (self.base / 'manifest.json').write_text(json.dumps(manifest))
        self.assertIsNotNone(self.library.opening(package, entry))
        for changed in (dict(entry, id='other'), dict(entry, openingState={'playerLocationId': 'other'}),
                        dict(entry, sourceCharacterNarratives={'lu': 'stone destroyed'})):
            self.assertIsNone(self.library.opening(package, changed))
        package['sourceAnalysis']['sha256'] = 'different'
        self.assertIsNone(self.library.opening(package, entry))

    def test_queue_bound_and_no_art_rule_never_spend(self):
        read, gateway = Mock(), Mock(available=True, model='mock')
        read.branch_view.return_value = self.node
        finish, entered = Event(), Event()
        def generate(prompt):
            entered.set(); finish.wait(3)
            return PNG, 'image/png'
        gateway.generate.side_effect = generate
        service = media_service(read, self.root / 'private', gateway)
        self.addCleanup(service.close)
        try:
            service.library.live_policy.return_value = None
            service.ensure('s', 'unmarked', subscriber='a', draw=True)
            gateway.generate.assert_not_called()
            service.library.live_policy.return_value = {'prompt': 'stone'}
            for i in range(20):
                service.ensure('s', str(i), subscriber='a', draw=True)
            self.assertTrue(entered.wait(1))
            self.assertEqual(len(service._jobs), service.MAX_JOBS)
            for i in range(20): service.release('s', str(i), 'a')
            self.assertFalse(any(j['status'] in ('queued', 'generating') for j in service._jobs.values()))
        finally:
            finish.set(); service._pool.shutdown(wait=True)
        self.assertEqual(gateway.generate.call_count, 1)

    def test_metrics_preserve_unknown_billing_and_unshown_completions(self):
        from open_story_engine.illustration_metrics import report
        (self.root / 'one.json').write_text(json.dumps(dict(provider_calls=1, completed=True, displayed=False,
                                                           cancel_stage='generating', usage={'total_tokens': 10})))
        (self.root / 'two.json').write_text(json.dumps(dict(provider_calls=1, completed=False, usage=None)))
        result = report(self.root)
        self.assertEqual(result['provider_calls'], 2)
        self.assertEqual(result['reported_tokens'], 10)
        self.assertIsNone(result['total_tokens'])
        self.assertIsNone(result['billed_amount'])
        self.assertEqual(result['completed_not_displayed'], 1)

    def test_explicit_draw_only_deduplicates_multitab_and_cancels_queued(self):
        read, gateway = Mock(), Mock(available=True, model='mock', usage=None)
        read.branch_view.return_value = self.node
        entered, finish = Event(), Event()
        def generate(prompt):
            entered.set()
            finish.wait(3)
            gateway.usage = {'total_tokens': 20}
            return PNG, 'image/png'
        gateway.generate.side_effect = generate
        service = media_service(read, self.root / 'private', gateway)
        self.addCleanup(service.close)
        try:
            service.ensure('s', 'first', subscriber='a')
            gateway.generate.assert_not_called()
            service.ensure('s', 'first', subscriber='a', draw=True)
            self.assertTrue(entered.wait(1))
            service.ensure('s', 'first', subscriber='b', draw=True)
            service.release('s', 'first', 'a')
            self.assertEqual(service.view('s', 'first')['items'][0]['status'], 'generating')
            service.ensure('s', 'queued', subscriber='a', draw=True)
            service.release('s', 'queued', 'a')
            self.assertEqual(service.view('s', 'queued')['items'][0]['status'], 'cancelled')
            service.release('s', 'first', 'b')
        finally:
            finish.set()
            service._pool.shutdown(wait=True)
        self.assertEqual(gateway.generate.call_count, 1)
        records = [json.loads(p.read_text()) for p in (self.root / 'private').glob('*.json') if not p.name.startswith('visit-')]
        running = next(r for r in records if r['bid'] == 'first')
        self.assertTrue(running['completed'])
        self.assertFalse(running['displayed'])
        self.assertEqual(running['cancel_stage'], 'generating')
        self.assertEqual(running['usage']['total_tokens'], 20)
        queued = next(r for r in records if r['bid'] == 'queued')
        self.assertEqual(queued['provider_calls'], 0)
        restored = media_service(read, self.root / 'private', gateway)
        self.addCleanup(restored.close)
        restored.ensure('s', 'first', subscriber='new', draw=True, retry=True)
        self.assertEqual(gateway.generate.call_count, 1)

    def test_lease_and_deadline_cancel_without_next_http_poll(self):
        read, gateway = Mock(), Mock(available=True, model='mock')
        read.branch_view.return_value = self.node
        finish, entered = Event(), Event()
        def generate(prompt):
            entered.set(); finish.wait(3)
            return PNG, 'image/png'
        gateway.generate.side_effect = generate
        service = media_service(read, self.root / 'private', gateway)
        self.addCleanup(service.close)
        try:
            service.ensure('s', 'first', subscriber='tab', draw=True)
            self.assertTrue(entered.wait(1))
            service.ensure('s', 'second', subscriber='tab', draw=True)
            with service._lock:
                for job in service._jobs.values():
                    if job['bid'] == 'second': job['subscribers']['tab'] = 0
                    else: job['created'] -= service.DEADLINE_SECONDS + 1
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if all(j['status'] == 'cancelled' for j in service._jobs.values()): break
                time.sleep(.02)
            self.assertEqual({j['reason'] for j in service._jobs.values()}, {'deadline', 'no_subscribers'})
        finally:
            finish.set(); service._pool.shutdown(wait=True)
        self.assertEqual(gateway.generate.call_count, 1)
