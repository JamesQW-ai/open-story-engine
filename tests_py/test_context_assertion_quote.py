import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_assertion_quote as v
from test_support import context_assertion_quote_eval as run


class QuoteContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.refs, cls.prior = run.prepare()

    def sample(self, cid='frame:self_report'):
        c = next(c for c in self.fixture['cases'] if c['id']==cid)
        ref = next(r['reference'] for r in self.refs if r['caseId']==cid)
        return c,self.fixture['scenes'][c['sceneKey']],copy.deepcopy(ref)

    def test_all_references_and_no_acceptance(self):
        from collections import Counter
        statuses=Counter()
        for c,ref in zip(self.fixture['cases'],self.refs):
            result=v.assess(ref['reference'],c,self.fixture['scenes'][c['sceneKey']],ref['reference'])
            statuses[result['status']]+=1
            self.assertEqual(result['semanticStatus'],'unverified')
            self.assertFalse(result['productionEnablement'])
            self.assertNotIn('decodedProposal',result)
        self.assertEqual(statuses,dict(reference_match=46,out_of_scope=2))

    def test_reported_external_subject_has_local_mention_and_is_withheld(self):
        c,e,r=self.sample()
        result=v.inspect(r,c,e)
        self.assertEqual(result['status'],'scope_pending')
        self.assertEqual(len(result['reportedSubjects']),1)
        for mode in ('null_origin','unknown_speaker','cue_in_core','foreign_mention','missing_scope'):
            bad=copy.deepcopy(r);item=bad[v.VERSION][0]['items'][0]
            if mode=='null_origin':item['origin']=None
            elif mode=='unknown_speaker':item['origin']['speaker']='unknown'
            elif mode=='cue_in_core':item['origin']['cue']=item['core'][:]
            elif mode=='foreign_mention':item['origin']['subjectMention']=['P1-U1','沈砚秋',0]
            else:item['origin']['scope']=item['core'][:]
            self.assertEqual(v.inspect(bad,c,e)['status'],'invalid_response',mode)

    def test_reference_scoring_detects_semantic_errors_that_structure_allows(self):
        c,e,r=self.sample('frame:other_report')
        bad=copy.deepcopy(r);item=bad[v.VERSION][1]['items'][0];item['origin']=None
        self.assertNotEqual(v.inspect(bad,c,e)['status'],'invalid_response')
        result=v.assess(bad,c,e,r)
        self.assertEqual(result['status'],'reference_mismatch')
        self.assertEqual(result['dimensions']['origins']['missing'],1)
        c,e,r=self.sample('gate:both_paraphrase');bad=copy.deepcopy(r)
        item=bad[v.VERSION][0]['items'][0]
        item['nonPremiseUnits']=item['conditions'][0]['units'][:];item['conditions']=[]
        self.assertEqual(v.assess(bad,c,e,r)['dimensions']['qualifiers']['missing'],1)
        c,e,r=self.sample('design:nested_pending');self.assertTrue(all(d['resolution']=='unresolved' for d in r[v.VERSION]))
        c,e,r=self.sample('design:unknown_speaker_pending');self.assertTrue(all(d['resolution']=='unresolved' for d in r[v.VERSION]))

    def test_scope_ids_dependencies_and_budgets_remain_strict(self):
        c,e,r=self.sample('gate:both_paraphrase')
        for mode in ('segment','overlap','omit','foreign','empty','many'):
            bad=copy.deepcopy(r);item=bad[v.VERSION][0]['items'][0]
            if mode=='segment':item['core'][0]+='-S1'
            elif mode=='overlap':item['nonPremiseUnits']=item['conditions'][0]['units'][:]
            elif mode=='omit':item['conditions']=[]
            elif mode=='foreign':item['conditions'][0]['units']=['P3-U1']
            elif mode=='empty':bad[v.VERSION]=[]
            else:bad[v.VERSION][0]['items']*=65
            self.assertEqual(v.inspect(bad,c,e)['status'],'invalid_response',mode)

    def test_range_equivalence_only_ignores_outer_space_and_terminal_stop(self):
        c,e,r=self.sample('frame:other_report');a=copy.deepcopy(r)
        item=a[v.VERSION][1]['items'][0];item['core'][1]+='。'
        self.assertEqual(v.assess(a,c,e,r)['status'],'reference_match')
        c,e,r=self.sample('control:negative_position');a=copy.deepcopy(r)
        a[v.VERSION][0]['items'][0]['core'][1]=a[v.VERSION][0]['items'][0]['core'][1].replace('不','')
        self.assertEqual(v.inspect(a,c,e)['status'],'invalid_response')
        c,e,r=self.sample('design:dual_origin');a=copy.deepcopy(r)
        a[v.VERSION][0]['items'].reverse()
        self.assertEqual(v.assess(a,c,e,r)['status'],'reference_match')

    def test_input_excludes_labels_and_prompt_budget_is_frozen(self):
        self.assertLessEqual(len(run.PROMPT.read_text()),1537)
        for c in self.fixture['cases']:
            e=self.fixture['scenes'][c['sceneKey']]
            hidden=dict(c,id='SECRET',frameAttributions={},selectionExpectations=[],reference='SECRET')
            self.assertEqual(run.messages(c,e),run.messages(hidden,e))
        with TemporaryDirectory() as d:
            p=Path(d)/'prompt';p.write_text('JSON changed')
            with patch.object(run,'PROMPT',p):
                with self.assertRaises(ValueError):run.prepare()

    def recorder(self, fail=False):
        rec=SimpleNamespace(calls=[],gateway=SimpleNamespace(max_tokens=8192))
        def complete(messages):
            content=json.dumps(self.refs[len(rec.calls)]['reference'],ensure_ascii=False)
            call=dict(messages=messages,content=content,rawResponse=json.dumps(dict(choices=[dict(message=dict(content=content))])))
            if fail:call.update(error='HTTP 503',errorType='LlmError')
            rec.calls.append(call)
            if fail:raise run.f.LlmError('HTTP 503',code='transport_error')
            return SimpleNamespace(content=content)
        rec.complete_json=complete
        return rec

    def test_fixed_budget_audit_tamper_and_exclusive_output(self):
        with TemporaryDirectory() as d, patch.object(run,'prepare',return_value=(self.fixture,self.refs,self.prior)), \
                patch.object(run.f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')):
            p=Path(d)/'report.json';rec=self.recorder()
            with patch.object(run.f,'_gateway',return_value=rec):report=run.run(p)
            self.assertEqual(rec.gateway.max_tokens,1024)
            self.assertEqual(report['actualCalls'],48)
            self.assertTrue(run.audit(p)['allScoresReproduced'])
            for mode in ('score','raw','input','calls','boundary','schema','scope'):
                bad=copy.deepcopy(report)
                if mode=='score':bad['cases'][0]['status']='reference_mismatch'
                elif mode=='raw':bad['cases'][0]['calls'][0]['rawResponse']='{}'
                elif mode=='input':bad['cases'][0]['calls'][0]['messages'][0]['content']='JSON changed'
                elif mode=='calls':bad['actualCalls']+=1
                elif mode=='schema':bad['schemaVersion']='other'
                elif mode=='scope':bad['comparisonScope']='semantic_truth'
                else:bad['acceptance']=True
                p.write_text(json.dumps(bad))
                with self.assertRaises((ValueError,KeyError)):run.audit(p)
            with patch.object(run.f,'_gateway') as gateway:
                with self.assertRaises(FileExistsError):run.run(p)
                gateway.assert_not_called()

    def test_transport_failure_stops_first_and_audits_blocked_run(self):
        with TemporaryDirectory() as d, patch.object(run,'prepare',return_value=(self.fixture,self.refs,self.prior)), \
                patch.object(run.f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')):
            p=Path(d)/'report.json';rec=self.recorder(True)
            with patch.object(run.f,'_gateway',return_value=rec):report=run.run(p)
            self.assertEqual(report['actualCalls'],1)
            self.assertEqual(report['summary']['all'],dict(model_error=1,not_run=47))
            self.assertTrue(run.audit(p)['actualInputsVerified'])
            report['cases'][0]['error']='different error'
            p.write_text(json.dumps(report))
            with self.assertRaises(ValueError):run.audit(p)

    def test_checkpoint_failure_stops_before_next_call(self):
        with TemporaryDirectory() as d, patch.object(run,'prepare',return_value=(self.fixture,self.refs,self.prior)), \
                patch.object(run.f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')):
            rec=self.recorder()
            original_save=run.f.save_checkpoint
            def save(path,report,**kwargs):
                if not kwargs.get('create'):raise OSError('disk unavailable')
                return original_save(path,report,**kwargs)
            with patch.object(run.f,'_gateway',return_value=rec),patch.object(run.f,'save_checkpoint',side_effect=save):
                with self.assertRaises(run.f.CheckpointError):run.run(Path(d)/'report.json')
            self.assertEqual(len(rec.calls),1)

    def test_invalid_json_retains_raw_content_without_repair(self):
        c,e,r=self.sample()
        result=run.assess_content('{broken',c,e,r)
        self.assertEqual(result['status'],'invalid_json')
        self.assertEqual(result['rawContent'],'{broken')
        self.assertFalse(result['acceptance'])
