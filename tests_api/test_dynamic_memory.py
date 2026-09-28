"""Reviewed longform outcome memory: persistence, selection and invalidation."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from open_story_engine import dynamic_memory as memory
from open_story_engine.context_bundle import ContextBundleBuilder
from open_story_engine.content import expand_state_visibility
from open_story_engine import reader_consequences
from open_story_engine import reader_actions
from open_story_engine.storage import SessionStore
from open_story_engine.api_narrative import PlayerNarrativePlanner
from open_story_engine.api_reader_quality import action_requirements
from open_story_engine.module_context import ModuleContextResolver
from test_support.longform import longform_cases
from tests_api import test_reader_consequences as fixture


class DynamicMemoryTests(unittest.TestCase):
    def setUp(self):
        base = fixture.ConsequenceTests()
        base.setUp()
        self.package, self.base_context = base.package, base.context
        self.assertIn((self.package['id'], self.package['version']),
                      [(c['package_id'], c['version']) for c in longform_cases()])
        self.store = SessionStore(':memory:')
        self.addCleanup(self.store.close)
        self.store.create_session(self.package, 'fixture', initial_state=base.root['branchState'])
        self.root = self.store.create_branch_root('fixture', base.root)
        result = base.reviewed_result()
        state = reader_consequences.commit_consequences(self.base_context, self.root['branchState'], result, 'memory-cause')
        self.node = {**result, 'id': 'memory-cause', 'parentId': self.root['id'],
                     'sourceNodeRef': self.root['sourceNodeRef'], 'branchState': state,
                     'nextDirections': self.root['nextDirections'], 'summary': '已审核后果',
                     'selectedDirectionId': self.root['nextDirections'][0]['id']}
        self.node['contextMemory'] = memory.receipt_for(self.package, self.node)
        self.cause = self.store.append_branch('fixture', self.root['id'], self.node)
        self.module = dict(characters=[dict(id=fixture.LU, name='陆照临')])

    def context(self, parent=None, action='留在原地观察'):
        parent = parent or self.cause
        return {**self.base_context, 'parent': parent,
                'lineage': self.store.lineage('fixture', parent['id']), 'playerDirection': action}

    def select(self, context=None, module=None, state=None):
        context = context or self.context()
        return memory.select(context, state or context['parent']['branchState'], self.module if module is None else module)

    def committed(self, bid, *, parent=None, changes=(), goals=(), threads=(), body='你记录本回合已经确认的变化。'):
        parent = parent or self.root
        context = self.context(parent, action=body)
        plan = fixture.plan(action_requirements(body), goals=list(goals))
        plan['threadUpdates'] = list(threads)
        plan['stateChanges'] = [dict(id='C' + str(i), entityId=entity, attribute=attr,
            before=reader_actions.value_at(parent['branchState'], entity, attr, 'item'),
            value=value, stepId='S1', reason='本回合实际变化', evidence=body)
            for i, (entity, attr, value) in enumerate(changes)]
        for entry in plan['goalUpdates'] + plan['threadUpdates']:
            entry['evidence'] = body
        result = fixture.seal(dict(narrativeText=body, consequenceUpdate=plan,
                                    consequenceReview=reader_consequences.VERSION))
        result['observedEvents'][0]['changes'] = [
            {key: c[key] for key in ('entityId', 'attribute', 'value')} for c in plan['stateChanges']]
        result['eventChecks'][0]['changeIds'] = [c['id'] for c in plan['stateChanges']]
        state = reader_consequences.commit_consequences(context, parent['branchState'], result, bid)
        node = {**result, 'id': bid, 'parentId': parent['id'], 'sourceNodeRef': parent['sourceNodeRef'],
                'branchState': state, 'nextDirections': parent['nextDirections'], 'summary': body,
                'selectedDirectionId': parent['nextDirections'][0]['id'], 'playerDirection': body}
        node['contextMemory'] = memory.receipt_for(self.package, node)
        memory.validate_receipt(self.package, node)
        return self.store.append_branch('fixture', parent['id'], node)

    def test_item_transfer_return_and_destruction_select_only_latest_cause(self):
        item = self.package['items'][0]
        iid = item['id']
        location = self.root['branchState']['playerLocationId']
        first = self.committed('placed', changes=[(iid, 'locationId', location)],
                               body='你将' + item['name'] + '放在地上。')
        second = self.committed('held', parent=first, changes=[(iid, 'ownerCharacterId', fixture.GU)],
                                body='你拾起' + item['name'] + '，拿在手里。')
        third = self.committed('placed-again', parent=second, changes=[(iid, 'locationId', location)],
                               body='你再次将' + item['name'] + '放在原处。')
        selected, _, audit = self.select(self.context(third), module={'items': [item]})
        self.assertEqual(len(selected), 1)
        self.assertIn('memory:item:placed-again:', selected[0]['sourceIds'][0])
        self.assertEqual(audit['omitted']['state_changed'], 2)
        destroyed = self.committed('destroyed', parent=third, changes=[(iid, 'destroyedPermanently', True)],
                                   body='你彻底毁掉' + item['name'] + '，原物无法修复。')
        selected, _, audit = self.select(self.context(destroyed), module={'items': [item]})
        self.assertEqual([m['content'] for m in selected], [item['name'] + '已永久毁坏。'])
        self.assertEqual(audit['omitted']['state_changed'], 3)
        self.assertEqual(self.select(self.context(self.root), module={'items': [item]})[0], [])

    def test_legacy_intermediate_item_changes_cannot_revive_old_memory(self):
        item = self.package['items'][0]
        first = self.committed('first-owner', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        second = self.committed('second-owner', parent=first, changes=[(item['id'], 'ownerCharacterId', fixture.LU)])
        third = self.committed('return-owner', parent=second, changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        context = self.context(third)
        for n in context['lineage'][-2:]:
            n.pop('contextMemory')
        self.assertEqual(self.select(context, module={'items': [item]})[2]['omitted'], {'state_changed': 1})

    def test_cleared_item_position_and_arbitrary_attributes_do_not_invent_facts(self):
        item = self.package['items'][0]
        first = self.committed('item-owner', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        cleared = self.committed('unknown-location', parent=first, changes=[(item['id'], 'ownerCharacterId', None)])
        self.assertIsNone(cleared['contextMemory'])
        self.assertEqual(self.select(self.context(cleared), module={'items': [item]})[0], [])
        custom = self.committed('custom-state', changes=[(item['id'], '备注', '未经分类的属性')])
        self.assertIsNone(custom['contextMemory'])

    def test_goal_transformation_keeps_successor_and_expires_previous_active_record(self):
        title = '检查陆照临的行踪'
        first = self.committed('new-goal', goals=[dict(id='new', title=title, status='active',
                                dependencies=[fixture.LU], reason='明确设定目标')])
        gid = first['branchState']['goalLedger'][-1]['id']
        transformed = self.committed('transform-goal', parent=first, goals=[dict(id=gid, title=title,
            status='transformed', successor='查明陆照临留下的线索', dependencies=[fixture.LU], reason='变更调查方向')])
        selected, _, audit = self.select(self.context(transformed))
        self.assertEqual({m['content'] for m in selected}, {
            '目标「检查陆照临的行踪」：已转化。', '目标「查明陆照临留下的线索」：进行中。'})
        self.assertEqual(audit['omitted']['state_changed'], 1)
        successor = transformed['branchState']['goalLedger'][-1]['id']
        completed = self.committed('complete-goal', parent=transformed, goals=[dict(id=successor,
            title='查明陆照临留下的线索', status='completed', dependencies=[fixture.LU], reason='目标已经完成')])
        selected = self.select(self.context(completed))[0]
        self.assertNotIn('进行中', str(selected))
        self.assertTrue(any('已完成' in m['content'] for m in selected))

    def test_thread_resolution_is_question_status_not_an_invented_answer(self):
        title = '陆照临是否已经出发'
        first = self.committed('open-thread', threads=[dict(id='new-1', title=title, status='open',
                                reason='登记待查问题', stepIds=['S1'])])
        tid = first['branchState']['threadLedger'][-1]['id']
        closed = self.committed('close-thread', parent=first, threads=[dict(id=tid, title=title,
            status='resolved', reason='本回合完成核实', stepIds=['S1'])])
        selected, _, audit = self.select(self.context(closed))
        self.assertEqual([m['content'] for m in selected], ['剧情问题「陆照临是否已经出发」：已解决。'])
        self.assertEqual(audit['omitted']['state_changed'], 1)
        self.assertEqual(self.select(self.context(closed), module={})[0], [])
        self.assertEqual(len(self.select(self.context(closed, action=title), module={})[0]), 1)

    def test_new_domains_require_literal_evidence_and_matching_committed_state(self):
        item = self.package['items'][0]
        nodes = [self.committed('check-item', changes=[(item['id'], 'ownerCharacterId', fixture.GU)]),
                 self.committed('check-goal', goals=[dict(id='new', title='检查陆照临', status='active', dependencies=[], reason='调查')]),
                 self.committed('check-thread', threads=[dict(id='new-1', title='陆照临去向', status='open', reason='待查', stepIds=['S1'])])]
        for original in nodes:
            for corrupt_evidence in (False, True):
                node = copy.deepcopy(original)
                record = node['contextMemory']['records'][0]
                domain = record['domain']
                if corrupt_evidence:
                    field = {'item': 'stateChanges', 'goal': 'goalUpdates', 'thread': 'threadUpdates'}[domain]
                    node['consequenceUpdate'][field][0]['evidence'] = '不在正文中的证据'
                elif domain == 'item':
                    node['branchState']['itemOwnerCharacterIds'][item['id']] = fixture.LU
                else:
                    node['branchState'][domain + 'Ledger'][-1]['status'] = 'unknown'
                with self.subTest(domain=domain, evidence=corrupt_evidence), self.assertRaises(ValueError):
                    memory.receipt_for(self.package, node)

    def test_scene_exit_and_return_loads_only_requested_facts_without_copying_history(self):
        item = self.package['items'][0]
        origin = self.committed('scene-origin', changes=[(item['id'], 'ownerCharacterId', fixture.GU)],
            goals=[dict(id='new', title='检查' + item['name'], status='active', dependencies=[],
                        itemDependencies=[item['id']], reason='检查新取得物品')])
        child = self.committed('other-scene', parent=origin)
        self.assertIsNone(child['contextMemory'])
        context = self.context(child)
        self.assertEqual(self.select(context, module={})[0], [])
        selected, _, _ = self.select(context, module={'items': [item]})
        self.assertEqual(len(selected), 2)
        self.assertTrue(all(m['branchId'] == child['id'] and m['parentMemoryId'] for m in selected))
        by_action = self.select(self.context(child, action='查看' + item['name']), module={})[0]
        self.assertEqual(selected, by_action)
        self.assertEqual(self.store.branch('fixture', origin['id'])['contextMemory'], origin['contextMemory'])

    def test_clearing_one_field_does_not_promote_an_inherited_field(self):
        item = self.package['items'][0]
        node = self.committed('clear-source', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        changed = copy.deepcopy(node)
        # Adversarial restored input: clearing an old location supplies no new
        # evidence for the inherited owner, even if that owner remains in state.
        changed['consequenceUpdate']['stateChanges'] = [dict(id='C0', entityId=item['id'],
            attribute='locationId', before=self.root['branchState']['playerLocationId'], value=None,
            stepId='S1', evidence=node['narrativeText'])]
        self.assertIsNone(memory.receipt_for(self.package, changed))

    def test_lifecycle_audit_distinguishes_supersession_and_missing_confirmation(self):
        item = self.package['items'][0]
        first = self.committed('audit-owner', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        second = self.committed('audit-transfer', parent=first, changes=[(item['id'], 'ownerCharacterId', fixture.LU)])
        context = self.context(second)
        before = copy.deepcopy(context['lineage'])
        # Relevance never decides whether a fact is historically superseded.
        selected, _, audit = self.select(context, module={})
        self.assertEqual(selected, [])
        lifecycle = audit['lifecycle']
        self.assertEqual(lifecycle['counts'], {'superseded': 1})
        self.assertEqual(lifecycle['details'][0]['replacementMemoryId'],
                         second['contextMemory']['records'][0]['memory']['memoryId'])
        self.assertNotIn('content', lifecycle['details'][0])
        self.assertEqual(context['lineage'], before)
        cleared = self.committed('audit-clear', parent=second, changes=[(item['id'], 'ownerCharacterId', None)])
        self.assertEqual(self.select(self.context(cleared), module={})[2]['lifecycle']['counts'], {'state_unverified': 2})
        context['lineage'][-1]['contextMemory']['records'][0]['memory']['content'] = '损坏来源'
        audit = self.select(context, module={})[2]
        self.assertEqual(audit['lifecycle']['counts'], {'source_invalid': 1, 'state_unverified': 1})
        self.assertTrue(all('replacementMemoryId' not in e for e in audit['lifecycle']['details']))

    def test_lifecycle_details_are_bounded_and_excluded_from_writer_bundle(self):
        item = self.package['items'][0]
        first = self.committed('bounded-owner', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        second = self.committed('bounded-transfer', parent=first, changes=[(item['id'], 'ownerCharacterId', fixture.LU)])
        last = self.committed('bounded-return', parent=second, changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        with patch.object(memory, 'MAX_LIFECYCLE_DETAILS', 1):
            selected, evidence, audit = self.select(self.context(last), module={'items': [item]})
        self.assertEqual(audit['lifecycle']['counts'], {'superseded': 2})
        self.assertEqual(len(audit['lifecycle']['details']), 1)
        self.assertEqual(audit['lifecycle']['omittedDetails'], 1)
        state = last['branchState']
        bundle = ContextBundleBuilder().build(context=self.context(last),
            selected=dict(id='wait', title='观察', summary='观察'), state=state,
            branch=dict(parentBranchId=last['id']), module_context={'items': [item]},
            state_visibility=expand_state_visibility(self.package['stateVisibility'], state, self.package),
            dynamic_memory=selected, memory_evidence=evidence)
        for stage in ('chapter', 'repair', 'result_contract', 'grounding_review', 'fact_extract'):
            self.assertNotIn('replacementMemoryId', json.dumps(bundle.project(stage)))
            self.assertNotIn('memory-selection-lifecycle', json.dumps(bundle.project(stage)))

    def test_source_shape_corruption_is_omitted_without_promoting_old_facts(self):
        for field, value in (('narrativeText', 123), ('consequenceUpdate', ['invalid'])):
            context = self.context()
            context['lineage'][-1][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.select(context)[2]['lifecycle']['counts'], {'source_invalid': 1})

    def test_v1_remains_readable_without_promoting_new_domains_or_allowing_downgrade(self):
        node = copy.deepcopy(self.node)
        node['contextMemory'] = memory.receipt_for(self.package, node, version=memory.LEGACY_VERSION)
        memory.validate_receipt(self.package, node, allow_legacy=True)
        with self.assertRaises(ValueError):
            memory.validate_receipt(self.package, node)
        context = self.context()
        context['lineage'][-1]['contextMemory'] = node['contextMemory']
        self.assertEqual(self.select(context)[0][0]['content'], '陆照临已死亡。')
        item = self.package['items'][0]
        item_node = self.committed('v1-item', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        self.assertIsNone(memory.receipt_for(self.package, item_node, version=memory.LEGACY_VERSION))
        context['lineage'][-1]['contextMemory']['schemaVersion'] = 'future/99'
        self.assertEqual(self.select(context)[2]['omitted'], {'source_changed': 1})

    def test_domains_share_budget_relevance_and_writer_deduplication(self):
        item = self.package['items'][0]
        node = self.committed('mixed', changes=[(item['id'], 'ownerCharacterId', fixture.GU)],
            goals=[dict(id='new', title='检查物品', status='active', dependencies=[], itemDependencies=[item['id']], reason='待办')],
            threads=[dict(id='new-' + str(i), title='陆照临的线索' + str(i), status='open', reason='待查', stepIds=['S1']) for i in range(1, 4)])
        context = self.context(node)
        module = dict(characters=self.module['characters'], items=[item])
        selected, evidence, audit = self.select(context, module=module)
        self.assertEqual(len(selected), 4)
        self.assertEqual(audit['omitted'], {'selection_budget': 1})
        self.assertEqual(self.select(context, module={})[0], [])
        state = node['branchState']
        bundle = ContextBundleBuilder().build(context=context,
            selected=dict(id='wait', title='观察', summary='观察'), state=state,
            branch=dict(parentBranchId=node['id']), module_context=module,
            state_visibility=expand_state_visibility(self.package['stateVisibility'], state, self.package),
            dynamic_memory=selected, memory_evidence=evidence)
        for stage in ('chapter', 'repair'):
            self.assertEqual(bundle.project(stage)['dynamicMemory'], selected)
            for proof in evidence:
                self.assertNotIn(proof, bundle.project(stage)['allowedEvidence'])
        for proof in evidence:
            self.assertIn(proof, bundle.project('grounding_review')['allowedEvidence'])

    def test_explicit_old_item_beats_recent_background_memories_with_fixed_budget(self):
        item = self.package['items'][0]
        node = self.committed('old-item', changes=[(item['id'], 'ownerCharacterId', fixture.GU)])
        for index in range(4):
            node = self.committed('background-' + str(index), parent=node, goals=[
                dict(id='new', title='陆照临的待办' + str(index), status='active',
                     dependencies=[fixture.LU], reason='待办')])
        context = self.context(node, action='查看' + item['name'])
        before = copy.deepcopy({k: v for k, v in context.items() if k != 'package'})
        selected, evidence, audit = self.select(context, module=self.module)
        expected = 'memory:item:old-item:' + item['id']
        self.assertIn(expected, [m['sourceIds'][0] for m in selected])
        self.assertEqual(selected[0]['sourceIds'], [expected])
        self.assertEqual(len(selected), memory.MAX_SELECTED)
        self.assertLessEqual(audit['contentChars'], memory.MAX_CONTENT_CHARS)
        self.assertEqual(audit['omitted']['selection_budget'], 1)
        self.assertEqual({k: v for k, v in context.items() if k != 'package'}, before)
        case = next(c for c in longform_cases() if c['package_id'] == self.package['id'])
        gateway = Mock(model='fixture')
        planner = PlayerNarrativePlanner(gateway, require_context_bundle=True,
            context_resolver=ModuleContextResolver.for_package(case['path'], self.package))
        state_patch = {'playerLocationId': node['branchState']['playerLocationId']}
        selected_direction = dict(id='inspect', title=context['playerDirection'],
                                  summary=context['playerDirection'], statePatch=state_patch)
        from open_story_engine.cocreation import apply_branch_patch
        checked_state = apply_branch_patch(self.package, node['branchState'], state_patch, node['sourceNodeRef'])
        context.update(validatedStatePatch=state_patch, validatedStatePatchSource='apply_branch_patch')
        planner._preflight_context_projection(context, selected_direction, checked_state)
        chapter = planner.last_context_bundle.project('chapter')
        self.assertIn(expected, [m['sourceIds'][0] for m in chapter['dynamicMemory']])
        self.assertNotIn(expected, [p['sourceId'] for p in chapter['allowedEvidence']])
        gateway.complete_json.assert_not_called()
        gateway.complete_text.assert_not_called()
        # A generic action still follows recency among equally relevant facts.
        generic = self.context(node)
        self.assertNotIn(expected, [m['sourceIds'][0] for m in self.select(generic)[0]])
        # Source drift cannot be rescued merely because the player names it.
        context['lineage'][1]['narrativeText'] += '来源已改'
        self.assertNotIn(expected, [m['sourceIds'][0] for m in self.select(context)[0]])

    def test_storage_round_trip_preserves_exact_receipt_and_source(self):
        self.assertEqual(self.cause['contextMemory'], self.node['contextMemory'])
        selected, evidence, audit = self.select()
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]['content'], '陆照临已死亡。')
        self.assertEqual(selected[0]['sourceIds'], [evidence[0]['sourceId']])
        self.assertEqual(audit['selectedCount'], 1)
        self.assertNotIn(fixture.BODY, json.dumps(selected, ensure_ascii=False))
        self.assertEqual(self.cause['branchState']['characterOutcomeStates'][fixture.LU]['causeBranchId'], self.cause['id'])

    def test_unreviewed_body_and_state_mismatch_cannot_mint_receipts(self):
        for field, value in (('narrativeText', '改写后的未经审核正文'), ('consequenceReview', None),
                             ('authorityReview', {'decision': 'reject'})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                memory.receipt_for(self.package, {**self.node, field: value})
        changed = copy.deepcopy(self.node)
        changed['branchState']['characterOutcomeStates'][fixture.LU]['status'] = 'alive'
        with self.assertRaisesRegex(ValueError, '状态不符'):
            memory.receipt_for(self.package, changed)

    def test_receipt_forgery_is_rejected_at_commit_boundary(self):
        for key, value in (('status', 'candidate'), ('content', '陆照临已复活。'), ('sequence', 999)):
            changed = copy.deepcopy(self.node)
            changed['contextMemory']['records'][0]['memory'][key] = value
            with self.assertRaises(ValueError):
                memory.validate_receipt(self.package, changed)
        missing = {k: v for k, v in self.node.items() if k != 'contextMemory'}
        with self.assertRaises(ValueError):
            memory.validate_receipt(self.package, missing)

    def test_irrelevant_and_expired_state_are_not_retrieved(self):
        self.assertEqual(self.select(module={})[2]['omitted'], {'irrelevant': 1})
        self.assertEqual(len(self.select(self.context(action='查看陆照临留下的痕迹'), module={})[0]), 1)
        changed = copy.deepcopy(self.cause['branchState'])
        changed['characterOutcomeStates'][fixture.LU]['status'] = 'alive'
        self.assertEqual(self.select(state=changed)[2]['omitted'], {'state_changed': 1})

    def test_prose_review_and_package_drift_invalidate_stored_memory(self):
        for field in ('narrativeText', 'eventChecks', 'playerDirection', 'selectedDirectionId'):
            context = self.context()
            context['lineage'][-1][field] = 'changed'
            self.assertEqual(self.select(context)[2]['omitted'], {'source_changed': 1})
        context = self.context()
        context['package'] = {**self.package, 'version': 'changed'}
        self.assertEqual(self.select(context)[2]['omitted'], {'source_changed': 1})

    def test_sibling_and_disconnected_ancestry_cannot_inherit(self):
        self.assertEqual(self.select(self.context(self.root))[0], [])
        context = self.context()
        context['lineage'][-1]['parentId'] = 'other-branch'
        with self.assertRaisesRegex(ValueError, '完整祖先链'):
            self.select(context)
        context = self.context()
        context['lineage'][0]['sessionId'] = 'other-session'
        with self.assertRaises(ValueError):
            self.select(context)

    def test_ancestor_rebases_identity_without_copying_into_each_branch(self):
        child = self.store.append_branch('fixture', self.cause['id'], {
            'id': 'memory-child', 'sourceNodeRef': self.cause['sourceNodeRef'],
            'branchState': self.cause['branchState'], 'summary': self.cause['summary'],
            'narrativeText': self.cause['narrativeText'], 'nextDirections': self.cause['nextDirections'],
            'selectedDirectionId': self.cause['nextDirections'][0]['id'],
        })
        selected, _, _ = self.select(self.context(child))
        original = self.cause['contextMemory']['records'][0]['memory']
        self.assertEqual(selected[0]['parentMemoryId'], original['memoryId'])
        self.assertEqual(selected[0]['branchId'], child['id'])
        self.assertEqual(selected[0]['content'], original['content'])
        self.assertNotIn('contextMemory', child)
        self.assertEqual(self.select(self.context(child))[0], selected)

    def test_selection_budget_omits_instead_of_truncating_fact(self):
        with patch.object(memory, 'MAX_CONTENT_CHARS', 1):
            selected, proof, audit = self.select()
        self.assertEqual((selected, proof), ([], []))
        self.assertEqual(audit['omitted'], {'selection_budget': 1})

    def test_atomic_rollback_removes_branch_and_memory_together(self):
        before = self.store.branches('fixture')
        with self.assertRaises(RuntimeError), self.store.connection:
            self.store.connection.execute('BEGIN IMMEDIATE')
            self.store.append_branch('fixture', self.root['id'], {**self.node, 'id': 'rolled-back'})
            raise RuntimeError('后续事务步骤失败')
        self.assertEqual(self.store.branches('fixture'), before)
        self.assertEqual(self.store.connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_legacy_summaries_and_fallback_do_not_become_memories(self):
        self.assertIsNone(memory.receipt_for(self.package, self.root))
        self.assertIsNone(memory.receipt_for(self.package, {**self.node, 'fallbackMode': 'conservative_approved'}))
        context = self.context()
        context['lineage'][-1].pop('contextMemory')
        self.assertEqual(self.select(context)[0], [])

    def test_writer_sees_fact_once_and_review_keeps_full_provenance(self):
        selected, evidence, _ = self.select()
        state = self.cause['branchState']
        bundle = ContextBundleBuilder().build(
            context=self.context(), selected=dict(id='wait', title='观察', summary='观察当前情况'),
            state=state, branch=dict(parentBranchId=self.cause['id']),
            module_context=self.module, state_visibility=expand_state_visibility(self.package['stateVisibility'], state, self.package),
            dynamic_memory=selected, memory_evidence=evidence)
        for stage in ('chapter', 'repair'):
            projection = bundle.project(stage)
            self.assertEqual(projection['dynamicMemory'], selected)
            self.assertNotIn(evidence[0], projection['allowedEvidence'])
        self.assertIn(evidence[0], bundle.project('grounding_review')['allowedEvidence'])
        self.assertIn(evidence[0]['sourceId'], bundle.as_dict()['provenance']['sourceIds'])
        planner = PlayerNarrativePlanner(Mock(model='fixture'))
        planner.last_context_bundle = bundle
        repair_evidence = planner._public_review_evidence(self.context(), stage='repair')
        self.assertEqual(repair_evidence[evidence[0]['sourceId']], evidence[0]['content'])
        from open_story_engine.api_narrative import select_repair_evidence
        selected_evidence, _ = select_repair_evidence(self.context(),
            {'issues': [{'type': 'state', 'claim': selected[0]['content']}]}, evidence=repair_evidence)
        self.assertEqual(selected_evidence[evidence[0]['sourceId']], evidence[0]['content'])


    def test_real_writer_prompt_loads_memory_once_even_without_continuity_text(self):
        package_case = next(c for c in longform_cases() if c['package_id'] == self.package['id'])
        planner = PlayerNarrativePlanner(Mock(model='fixture'),
            context_resolver=ModuleContextResolver.for_package(package_case['path'], self.package),
            context_projection={'stateVisibilityMode': 'formal_required'})
        context = self.context(action='查看陆照临留下的痕迹')
        state = self.cause['branchState']
        state_patch = {'playerLocationId': state['playerLocationId']}
        context.update(validatedStatePatch=state_patch, validatedStatePatchSource='apply_branch_patch')
        selected = dict(id='inspect', title='观察痕迹', summary=context['playerDirection'], statePatch=state_patch)
        with patch('open_story_engine.api_narrative.chapter_continuity_text', return_value=('', {'source': 'legacy'})):
            prompt = planner._prompt(context, selected, state, None)
        self.assertEqual(planner.last_prompt_context['contextBundle']['memorySelection']['selectedCount'], 1)
        selected_memory = planner.last_context_bundle.project('chapter')['dynamicMemory'][0]
        self.assertEqual(prompt.count(selected_memory['memoryId']), 1)
        self.assertEqual(prompt.count('已确认动态记忆'), 1)
