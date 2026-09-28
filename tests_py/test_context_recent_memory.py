"""Relevant dialogue survives short memory without copying whole history."""
import copy
import unittest

from open_story_engine.context_bundle import ContextBundleBuilder, focused_recent_lineage


class RecentMemoryTests(unittest.TestCase):
    def test_admitting_previous_action_keeps_irreversible_cause_before_keyword_matches(self):
        cause = '你杀死了伤者，伤者的呼吸已经停止。'
        node = dict(id='latest', narrativeText=cause + '\n\n' + '守门弟子在原地等候。' * 35 +
                    '\n\n' + '你留在原地，等待守门弟子回应。' * 30,
                    consequenceUpdate=dict(outcomes=[dict(status='dead', evidence=cause)]))
        before = copy.deepcopy(node)
        text = focused_recent_lineage([dict(id='older', narrativeText='雨落。'), node],
                                      '我承认刚才的行为，留在原地等候守门弟子处置。')
        self.assertIn(cause, text[-1]['summary'])
        self.assertLessEqual(sum(len(n['summary']) for n in text), 1400)
        self.assertEqual(node, before)

    def test_irreversible_priority_does_not_inject_nonliteral_evidence(self):
        node = dict(id='latest', narrativeText='你在原地等候。',
                    consequenceUpdate=dict(outcomes=[dict(status='dead', evidence='虚构的死亡依据')]))
        self.assertNotIn('虚构的死亡依据', focused_recent_lineage([node], '继续')[0]['summary'])

    def test_long_outcome_span_keeps_result_and_cause_before_identification_setup(self):
        setup = '伤者躺在石阶旁。' + '雨滴落在石上。' * 50
        cause = '你按住他的肩，对他动了手。'
        result = '他的身体不再动弹，伤者已经死亡。'
        evidence = '\n\n'.join([setup, cause, result])
        node = dict(id='latest', narrativeText=evidence + '\n\n守门弟子等候你的回答。',
                    consequenceUpdate=dict(outcomes=[dict(status='dead', evidence=evidence)]))
        memory = focused_recent_lineage([dict(id='older', narrativeText='山门已关。'), node],
                                        '我承认刚才的行为，等候守门弟子处置。')
        self.assertIn(cause, memory[-1]['summary'])
        self.assertIn(result, memory[-1]['summary'])
        self.assertLessEqual(sum(len(n['summary']) for n in memory), 1400)

    def test_question_retrieves_actual_answer_and_preserves_attribution(self):
        nodes = [dict(id='old', narrativeText='旧路线秘密', summary='旧路线秘密'),
                 dict(id='one', narrativeText='雨落在屋檐上。\n\n阿渡说：“空灯是我在雪原路上捡的。”\n\n你起身挡住风。',
                      summary='问过空灯，伤者已作答'),
                 dict(id='two', narrativeText='执事收下引荐文书。\n\n他说：“明日到库房查押送记录。”',
                      summary='执事回应了请求')]
        before = copy.deepcopy(nodes)
        memory = focused_recent_lineage(nodes, '追问空灯来历，并核实押送记录')
        text = '\n'.join(n['summary'] for n in memory)
        self.assertIn('空灯是我在雪原路上捡的', text)
        self.assertIn('明日到库房查押送记录', text)
        self.assertIn('不等于已核实事实', text)
        self.assertNotIn('旧路线秘密', text)
        self.assertNotIn('雨落在屋檐上', text)
        self.assertEqual(nodes, before)
        self.assertEqual([n['id'] for n in memory], ['one', 'two'])

    def test_budget_keeps_complete_paragraphs_and_latest_endpoint(self):
        paragraphs = ['空灯' + '甲' * 590, '空灯' + '乙' * 590, '空灯' + '丙' * 590]
        nodes = [dict(id=str(i), narrativeText='\n\n'.join(paragraphs), summary='短摘要') for i in range(4)]
        memory = focused_recent_lineage(nodes, '检查空灯')
        text = '\n'.join(n['summary'] for n in memory)
        self.assertLess(len(text), 1550)
        self.assertEqual([n['id'] for n in memory], ['2', '3'])
        self.assertIn(paragraphs[-1], memory[-1]['summary'])
        self.assertNotIn('甲' * 20, text)

    def test_correction_keeps_adjacent_copy_and_legibility_evidence(self):
        # Actual Taixu turn 19 regression: the old selector dropped P5 and
        # turn 20 invented that no second copy existed.
        paragraphs = [
            '“查完了？”',
            '“查完了。北河城那批，押送人一栏写的是另一个人，日期三月十七。我想再核押送人的名字。”',
            '书办起身，从架子里抽出备查底册。',
            '“底册有两本，”他一边翻一边说，“一本在库房，一本在这儿备查。你抄的那条，我核核。”',
            '他翻到三月十七那一格。押送人一栏写着另一个名字，笔画清楚，不是陆沉舟。',
            '“名字对得上，印也对得上。”',
            '廊下风声渐起。',
        ]
        nodes = [dict(id='earlier', narrativeText='阿渡在门外等候。', summary='等候'),
                 dict(id='ledger', narrativeText='\n\n'.join(paragraphs), summary='已核实姓名')]
        before = copy.deepcopy(nodes)
        memory = focused_recent_lineage(nodes, '请书办把三月十七北河城那一行的押送人全名读出来，当面核对姓名。')
        text = memory[-1]['summary']
        self.assertIn(paragraphs[3], text)
        self.assertIn(paragraphs[4], text)
        self.assertNotIn(paragraphs[-1], text)
        self.assertEqual(nodes, before)
        self.assertLessEqual(sum(len(n['summary']) for n in memory), 1400)

    def test_latest_account_cannot_starve_previous_turn_or_expand_fallback(self):
        old = dict(id='old', narrativeText='“底册有两本，名字清楚。”', summary='旧摘要')
        latest = dict(id='new', narrativeText='\n\n'.join(['名字' + '甲' * 590] * 4), summary='新摘要')
        memory = focused_recent_lineage([old, latest], '核对名字')
        self.assertIn('底册有两本', memory[0]['summary'])
        self.assertLessEqual(sum(len(n['summary']) for n in memory), 1400)
        large = dict(id='oversize', narrativeText='名字' + '甲' * 2000, summary='乙' * 3000)
        memory = focused_recent_lineage([old, large], '核对名字')
        self.assertLessEqual(sum(len(n['summary']) for n in memory), 1400)
        self.assertNotIn('乙', memory[-1]['summary'])

    def test_projection_does_not_restore_an_oversized_fallback_summary(self):
        context = dict(package=dict(id='p', version='1'), playerDirection='核对底册',
                       narrativePolicy='context_only', lineage=[dict(id='n1', summary='甲' * 3000,
                           narrativeText='底册' + '乙' * 3000,
                           readerOutcome=dict(action=dict(summary='丙' * 3000)))])
        bundle = ContextBundleBuilder().build(context=context,
            selected=dict(id='d', title='核对底册', summary='核对底册'),
            state={}, branch=dict(branchId='n1'), state_visibility={}, module_context={})
        projection = bundle.project('chapter')
        self.assertLessEqual(sum(len(i['content']) for i in projection['continuityWindow']), 1400)
        self.assertNotIn('丙', str(projection['continuityWindow']))

    def test_original_paragraph_ids_survive_blank_paragraphs(self):
        nodes = [dict(id='n', narrativeText='风起。\n\n\n\n“底册有两本。”', summary='已回答')]
        text = focused_recent_lineage(nodes, '底册')[0]['summary']
        self.assertIn('P3: “底册有两本。”', text)

    def test_both_stages_receive_selected_answer_once(self):
        answer = '阿渡说：“空灯是路上捡的。”'
        context = dict(package=dict(id='p', version='1'), playerDirection='问清空灯来历',
                       narrativePolicy='context_only', lineage=[dict(id='n1', summary='已回答', narrativeText=answer)])
        bundle = ContextBundleBuilder().build(context=context,
            selected=dict(id='d', title='问清空灯来历', summary='问清空灯来历'),
            state={}, branch=dict(branchId='n1'), state_visibility={}, module_context={})
        for stage in ('result_contract', 'chapter'):
            projection = bundle.project(stage)
            prose = [item['content'] for item in projection.get('allowedEvidence', [])]
            prose += [item['content'] for item in projection.get('continuityWindow', [])]
            self.assertEqual('\n'.join(prose).count(answer), 1)

    def test_free_action_replaces_stale_source_stop_but_controlled_action_keeps_it(self):
        source = dict(actionContract=dict(kind='action', instruction='不扩展到下一节点', stopPoint='仍在山门'))
        before = copy.deepcopy(source)
        context = dict(package=dict(id='p', version='1'), playerDirection='等到执事回应后按指路求助',
                       narrativePolicy='context_only')
        for free in (True, False):
            bundle = ContextBundleBuilder().build(context=context,
                selected=dict(id='d', title='求助', summary='求助', isFreeText=free),
                state={}, branch=dict(branchId='n1'), state_visibility={}, module_context=source)
            for stage in ('result_contract', 'chapter'):
                projection = bundle.project(stage)
                contract = projection['hardConstraints']['actionContract']
                self.assertEqual(contract['kind'], 'player_input' if free else 'action')
                self.assertEqual(projection['hardConstraints']['stopPoint'], None if free else '仍在山门')
            self.assertEqual(bundle.as_dict()['authoritativeState'], {})
        self.assertEqual(source, before)


if __name__ == '__main__':
    unittest.main()
