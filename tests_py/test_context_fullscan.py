import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_fullscan as f


class FullscanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=f.load_cases()

    def case(self,cid):
        return copy.deepcopy(next(c for c in self.fixture['cases'] if c['id']==cid))

    def response(self,case):
        units=f.indexed_input(case)['units']
        return dict(items=[dict(unitIds=list(t['unitIds']),status=t['expectedStatus'],normal=copy.deepcopy(t['expectedNormal']),
                                limitations=[dict(kind=k,quote=units[t['unitIds'][0]]['quote']) for k in t['expectedLimitations']],
                                reason='对应原文') for t in case['targets']])

    def assess(self,r,c):return f.assess(r,c,self.fixture['entities'])

    def test_input_covers_full_draft_and_excludes_preselected_targets(self):
        c=self.case('original_v11');p=f.model_input(c,self.fixture['entities'])
        self.assertEqual(set(p),{'draft','units','entities'})
        covered=set()
        for u in p['units'].values():
            self.assertEqual(c['draft'][u['start']:u['end']],u['quote'])
            covered.update(range(u['start'],u['end']))
        self.assertTrue(all(ch.isspace() or i in covered for i,ch in enumerate(c['draft'])))
        c['targets']=[];c['id']='changed'
        self.assertEqual(p,f.model_input(c,self.fixture['entities']))

    def test_all_occurrences_match_and_empty_draft_scope_is_valid(self):
        self.assertEqual(sum(len(c['targets']) for c in self.fixture['cases']),11)
        for c in self.fixture['cases']:self.assertEqual(self.assess(self.response(c),c)['status'],'matched')

    def test_same_unit_two_subjects_cannot_mask_one_missing(self):
        c=self.case('two_subjects');r=self.response(c);r['items'].pop()
        score=self.assess(r,c)
        self.assertEqual(score['matchedTargets'],[0]);self.assertEqual(score['missingTargets'],[1])

    def test_duplicate_does_not_satisfy_second_subject(self):
        c=self.case('two_subjects');r=self.response(c)
        r['items'][1]=copy.deepcopy(r['items'][0]);score=self.assess(r,c)
        self.assertEqual(score['missingTargets'],[1]);self.assertEqual(score['extraItems'],[1]);self.assertEqual(score['duplicateItems'],[1])

    def test_repeated_position_at_distinct_spans_is_not_duplicate(self):
        c=self.case('repeated_mentions');score=self.assess(self.response(c),c)
        self.assertEqual(score['matchedTargets'],[0,1]);self.assertEqual(score['duplicateItems'],[])

    def test_missing_condition_span_cannot_match(self):
        c=self.case('conditional');r=self.response(c)
        r['items'][0]['unitIds'].pop(0)
        r['items'][0]['limitations'][0]['quote']='伤者'
        score=self.assess(r,c);self.assertEqual(score['status'],'mismatched')
        self.assertEqual(score['missingTargets'],[0])

    def test_extra_ordinary_action_is_reported_even_with_empty_gold(self):
        c=self.case('no_position');r=self.response(self.case('two_subjects'))
        r['items']=r['items'][:1]
        score=self.assess(r,c);self.assertEqual(score['extraItems'],[0]);self.assertEqual(score['status'],'mismatched')

    def test_invalid_ids_order_and_response_fields_fail_closed(self):
        c=self.case('conditional')
        for ids in ([],['missing'],['P1-U1','P1-U1'],['P1-U2','P1-U1']):
            r=self.response(c);r['items'][0]['unitIds']=ids
            self.assertEqual(self.assess(r,c)['status'],'invalid_response')
        self.assertEqual(self.assess(dict(items=[],authority=True),c)['status'],'invalid_response')

    def test_fixture_rejects_changed_source_original_or_gold(self):
        for kind in ('binding','original','gold'):
            data=copy.deepcopy(self.fixture)
            if kind=='binding':data['bindings'][next(iter(data['bindings']))]='0'*64
            elif kind=='original':data['cases'][0]['draft']='changed'
            else:data['cases'][0]['targets'][0]['expectedNormal']['time']='past'
            with TemporaryDirectory() as directory:
                p=Path(directory)/'fixture.json';p.write_text(json.dumps(data))
                with self.assertRaises(ValueError):f.load_cases(p)

    def recorder(self,fail=False):
        recorder=SimpleNamespace(calls=[])
        def complete(messages):
            c=self.fixture['cases'][len(recorder.calls)];content=json.dumps(self.response(c))
            recorder.calls.append(dict(messages=messages,content=content))
            if fail:raise f.LlmError('HTTP 400',code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json=complete
        return recorder

    def test_one_request_each_and_existing_evidence_protected(self):
        recorder=self.recorder()
        with TemporaryDirectory() as directory,patch.object(f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')),patch.object(f,'_gateway',return_value=recorder):
            path=Path(directory)/'report.json';r=f.run(path)
            self.assertEqual(r['actualCalls'],9);self.assertEqual(r['summary']['matched'],9)
            self.assertFalse(r['acceptance'])
            with self.assertRaises(FileExistsError):f.run(path)

    def test_transport_and_save_failures_stop_calls(self):
        for disk in (False,True):
            recorder=self.recorder(fail=not disk)
            with TemporaryDirectory() as directory,patch.object(f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')),patch.object(f,'_gateway',return_value=recorder):
                if disk:
                    with patch.object(f,'save_checkpoint',side_effect=[None,OSError('disk full')]):
                        with self.assertRaises(f.CheckpointError):f.run(Path(directory)/'report.json')
                else:
                    r=f.run(Path(directory)/'report.json');self.assertEqual(r['summary']['not_run'],8)
            self.assertEqual(len(recorder.calls),1)
