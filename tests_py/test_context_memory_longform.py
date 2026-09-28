"""Storage-backed longform memory boundaries, not live prose acceptance."""
import json
import sqlite3
import unittest

from open_story_engine.context_bundle import (
    ContextBundleBuilder, ContextBundleError, validate_dynamic_memory_transition,
)
from open_story_engine.content import expand_state_visibility, load_runtime_story_package
from open_story_engine.cocreation import apply_branch_patch, create_contract, entry_node
from open_story_engine.module_context import ModuleContextResolver
from open_story_engine.storage import SessionStore
from test_support.longform import longform_cases


class LongformMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = []
        for case in longform_cases():
            package = load_runtime_story_package(case['path'], lazy=True)
            entry = next(iter(package['story']['entryModel']['entryPoints']))
            session = 'memory-boundary-replay-' + case['package_id']
            contract = create_contract(package, session, {
                'kind': 'source_character', 'sourceCharacterId': entry['sourceCharacterIds'][0],
            })
            root = entry_node(package, contract)
            store = SessionStore(':memory:')
            cls.addClassCleanup(store.close)
            store.create_session(package, session, initial_state=root['branchState'])
            root = store.create_branch_root(session, root)
            fixture = dict(package=package, resolver=ModuleContextResolver.for_package(case['path'], package),
                           contract=contract, session=session, store=store, root=root, nodes=[])
            parent = root
            for index in range(3):
                parent = cls.append(fixture, parent, 'main-' + str(index))
                fixture['nodes'].append(parent)
            fixture['sibling'] = cls.append(fixture, root, 'sibling')
            cls.fixtures.append(fixture)

    @staticmethod
    def append(fixture, parent, request):
        # A no-op state path with published opening text is a storage fixture;
        # it is not a model-generated continuation or semantic review receipt.
        patch = {'playerLocationId': parent['branchState']['playerLocationId']}
        state = apply_branch_patch(fixture['package'], parent['branchState'], patch, parent['sourceNodeRef'])
        return fixture['store'].append_branch(fixture['session'], parent['id'], {
            'requestId': request, 'sourceNodeRef': parent['sourceNodeRef'], 'branchState': state,
            'summary': fixture['root']['summary'], 'narrativeText': fixture['root']['narrativeText'],
            'nextDirections': fixture['root']['nextDirections'], 'factDeltas': [],
            'selectedDirectionId': parent['nextDirections'][0]['id'],
            'canonicalRelation': 'diverged',
        })

    @staticmethod
    def bundle(fixture, parent=None, memory=()):
        parent = parent or fixture['nodes'][-1]
        package, state = fixture['package'], parent['branchState']
        patch = {'playerLocationId': state['playerLocationId']}
        state = apply_branch_patch(package, state, patch, parent['sourceNodeRef'])
        selected = {**fixture['root']['nextDirections'][0], 'statePatch': patch}
        context = dict(package=package, contract=fixture['contract'], parent=parent,
                       lineage=fixture['store'].lineage(fixture['session'], parent['id']),
                       playerDirection=selected['summary'], characterDetails=[])
        return ContextBundleBuilder(fixture['resolver']).build(
            context=context, selected=selected, state=state, context_id='memory-replay',
            branch=dict(sessionId=fixture['session'], parentBranchId=parent['id'], lineageHead=parent['id']),
            state_visibility=expand_state_visibility(package['stateVisibility'], state, package),
            validated_state_patch=patch, dynamic_memory=memory, state_visibility_mode='formal_required',
        )

    @staticmethod
    def memory(bundle):
        source = next(x for x in bundle.as_dict()['allowedEvidence'] if
                      x['kind'] in ('public_fact', 'confirmed_event', 'current_beat')
                      and x['validity'] == 'confirmed' and x['visibility'] in ('player_known', 'public_world_fact')
                      and x['authority'] != 'style_only')
        # Explicit test snapshot; transition validation is not semantic confirmation.
        content = source['content'] if isinstance(source['content'], str) else json.dumps(source['content'], ensure_ascii=False)
        return dict(memoryId='memory-fixed-source', kind='event', sourceIds=[source['sourceId']],
                    branchId=bundle.branch['parentBranchId'], visibility=source['visibility'],
                    authority='confirmed_evidence', validity='unknown', status='candidate',
                    sequence=1, content=content)

    def test_expired_nodes_leave_continuity_but_remain_in_storage(self):
        for fixture in self.fixtures:
            bundle = self.bundle(fixture)
            self.assertEqual([x['sourceId'] for x in bundle.continuity_window],
                             ['branch:lineage:' + n['id'] for n in fixture['nodes'][-2:]])
            lineage = fixture['store'].lineage(fixture['session'], fixture['nodes'][-1]['id'])
            self.assertEqual(len(lineage), 4)
            self.assertEqual(lineage[0]['id'], fixture['root']['id'])

    def test_branch_switch_reads_own_lineage_only(self):
        for fixture in self.fixtures:
            bundle = self.bundle(fixture, fixture['sibling'])
            self.assertEqual([x['sourceId'] for x in bundle.continuity_window],
                             ['branch:lineage:' + n['id'] for n in (fixture['root'], fixture['sibling'])])
            main_ids = {'branch:lineage:' + n['id'] for n in fixture['nodes']}
            self.assertFalse(main_ids.intersection(x['sourceId'] for x in bundle.allowed_evidence))

    def test_rebuild_and_duplicate_request_do_not_advance_window(self):
        for fixture in self.fixtures:
            before = self.bundle(fixture)
            with self.assertRaises(sqlite3.IntegrityError):
                self.append(fixture, fixture['nodes'][-1], 'main-2')
            after = self.bundle(fixture)
            self.assertEqual(before.context_sha256, after.context_sha256)
            self.assertEqual(len(fixture['store'].lineage(fixture['session'], fixture['nodes'][-1]['id'])), 4)

    def test_candidate_unknown_and_rejected_memory_cannot_enter_writer(self):
        for fixture in self.fixtures:
            memory = self.memory(self.bundle(fixture))
            for status, validity in (('candidate', 'unknown'), ('unknown', 'unknown'), ('rejected', 'rejected')):
                bundle = self.bundle(fixture, memory=[{**memory, 'status': status, 'validity': validity}])
                self.assertEqual(bundle.project('chapter')['dynamicMemory'], [])
                self.assertEqual(bundle.project('repair')['dynamicMemory'], [])
                self.assertEqual(len(bundle.project('grounding_review')['dynamicMemory']), 1)

    def test_confirmed_snapshot_needs_traceable_source_and_own_branch(self):
        for fixture in self.fixtures:
            candidate = self.memory(self.bundle(fixture))
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            validate_dynamic_memory_transition(candidate, confirmed)
            bundle = self.bundle(fixture, memory=[confirmed])
            self.assertEqual(bundle.project('chapter')['dynamicMemory'], [confirmed])
            for changed in ({**confirmed, 'sourceIds': ['missing-source']},
                            {**confirmed, 'branchId': fixture['sibling']['id']}):
                with self.assertRaises(ContextBundleError):
                    self.bundle(fixture, memory=[changed])

    def test_summary_cannot_be_promoted_to_confirmed_fact(self):
        for fixture in self.fixtures:
            bundle = self.bundle(fixture)
            memory = {**self.memory(bundle), 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2,
                      'sourceIds': [bundle.continuity_window[-1]['sourceId']]}
            with self.assertRaisesRegex(ContextBundleError, '来源类型不能支持事实'):
                self.bundle(fixture, memory=[memory])

    def test_snapshot_is_immutable_and_rejection_cannot_resurrect(self):
        for fixture in self.fixtures:
            candidate = self.memory(self.bundle(fixture))
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            bundle = self.bundle(fixture, memory=[confirmed])
            before = bundle.as_dict()
            confirmed['content'] = '外部对象被修改'
            self.assertEqual(bundle.as_dict(), before)
            rejected = {**candidate, 'status': 'rejected', 'validity': 'rejected', 'sequence': 3}
            with self.assertRaisesRegex(ContextBundleError, '不允许从 rejected'):
                validate_dynamic_memory_transition(rejected, {**candidate, 'sequence': 4})

    def test_inheritance_and_confirmation_cannot_rewrite_fact_or_evidence(self):
        for fixture in self.fixtures:
            candidate = self.memory(self.bundle(fixture))
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            inherited = {**confirmed, 'memoryId': 'inherited-memory', 'branchId': 'descendant',
                         'parentMemoryId': confirmed['memoryId'], 'inheritanceReason': '来源仍有效', 'sequence': 3}
            validate_dynamic_memory_transition(confirmed, inherited)
            changed_visibility = 'player_known' if candidate['visibility'] != 'player_known' else 'public_world_fact'
            for field, value in (('content', '偷偷改写的事实'), ('sourceIds', ['another-source']),
                                 ('visibility', changed_visibility), ('authority', 'authoritative'),
                                 ('kind', 'relationship'), ('extraFact', '新增的事实')):
                for before, after in ((candidate, confirmed), (confirmed, inherited)):
                    with self.subTest(field=field), self.assertRaises(ContextBundleError):
                        validate_dynamic_memory_transition(before, {**after, field: value})

    def test_inherited_memory_can_be_rejected_without_erasing_its_history(self):
        for fixture in self.fixtures:
            candidate = self.memory(self.bundle(fixture))
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            inherited = {**confirmed, 'memoryId': 'inherited-memory', 'branchId': 'descendant',
                         'parentMemoryId': confirmed['memoryId'], 'inheritanceReason': '已审核继承', 'sequence': 3}
            rejected = {**inherited, 'status': 'rejected', 'validity': 'rejected', 'sequence': 4,
                        'conflictWith': ['verified-conflict']}
            validate_dynamic_memory_transition(inherited, rejected)
            self.assertEqual(rejected['parentMemoryId'], confirmed['memoryId'])
            self.assertEqual(rejected['content'], confirmed['content'])
            with self.assertRaises(ContextBundleError):
                validate_dynamic_memory_transition(inherited, {**rejected, 'content': '用改写掩盖原事实'})
            with self.assertRaisesRegex(ContextBundleError, '不允许从 rejected'):
                validate_dynamic_memory_transition(rejected, {**inherited, 'sequence': 5})

    def test_json_boolean_and_number_are_distinct_memory_facts(self):
        for fixture in self.fixtures:
            candidate = {**self.memory(self.bundle(fixture)), 'content': {'confirmed': True}}
            confirmed = {**candidate, 'status': 'confirmed', 'validity': 'confirmed', 'sequence': 2}
            validate_dynamic_memory_transition(candidate, confirmed)
            with self.assertRaisesRegex(ContextBundleError, '不得改写'):
                validate_dynamic_memory_transition(candidate, {**confirmed, 'content': {'confirmed': 1}})
