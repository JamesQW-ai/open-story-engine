import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_entity_refs_v2_eval as v
from tests_py.test_context_qualifier_entity_refs import proposal as refs_control


def proposal(case, entities):
    original = refs_control(case, entities)
    items = original[v.e.refs_v1.VERSION]
    for item in items:
        item['position'] = {key: item['position'][key] for key in ('value','polarity')}
    return {v.e.VERSION: items}


class EntityRefsV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.fixture = v.load_cases()

    def case(self, cid='gate:both'):
        case = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id']==cid))
        return case, self.fixture['scenes'][case['sceneKey']]

    def test_all_controls_derive_identity_without_confirming_facts(self):
        for c in self.fixture['cases']:
            e=self.fixture['scenes'][c['sceneKey']]
            r=proposal(c,e);original=copy.deepcopy(r)
            result=v.e.assess(r,c,e)
            self.assertEqual(result['status'],'matched',(c['id'],result))
            self.assertEqual(r,original)
            self.assertEqual(result['proposedResponse'],r)
            self.assertEqual(result['semanticStatus'],'unverified')
            self.assertFalse(result['productionEnablement'])
            for item, expected in zip(result['decodedResponse']['items'],c['selectionExpectations']):
                self.assertEqual(item['position'],expected['position'])
                if item['limitations']:
                    self.assertIsNone(result['projection']['items'][result['decodedResponse']['items'].index(item)]['normal'])

    def test_old_or_duplicate_identity_fields_are_not_ignored(self):
        c,e=self.case()
        self.assertEqual(v.e.inspect(refs_control(c,e),c,e)['status'],'invalid_response')
        for key in ('subject','object','relation','time','condition'):
            r=proposal(c,e);r[v.e.VERSION][0]['position'][key]='extra'
            result=v.e.inspect(r,c,e)
            self.assertEqual(result['status'],'invalid_response')
            self.assertEqual(result['proposedResponse'],r)

    def test_wrong_side_and_overbroad_premise_still_fail_strict_scoring(self):
        c,e=self.case()
        for mode in ('side','premise','missing'):
            r=proposal(c,e);item=r[v.e.VERSION][0]
            if mode=='side':item['position']['value']='outside'
            elif mode=='premise':item['limitations'][0]['premise']=list(item['qualified'])
            else:item['position']=None
            self.assertEqual(v.e.assess(r,c,e)['status'],'mismatched')

    def test_missing_ambiguous_wrong_role_or_foreign_ref_cannot_derive_identity(self):
        c,e=self.case()
        for mode in ('null','role','foreign','missing'):
            r=proposal(c,e);item=r[v.e.VERSION][0]
            if mode=='null':item['subject']=None
            elif mode=='role':item['subject']=item['boundary']
            elif mode=='foreign':item['subject']='invented'
            else:del item['subject']
            self.assertEqual(v.e.inspect(r,c,e)['status'],'invalid_response')
        c,e=self.case('hall:source_position');e=copy.deepcopy(e)
        e['scene:other_hall']=dict(kind='scene_boundary',mentions=['殿'])
        self.assertIn('歧义',v.e.inspect(proposal(c,e),c,e)['error'])

    def test_wire_envelope_and_input_stay_bound_and_blind(self):
        c,e=self.case();r=proposal(c,e)
        for bad in ({'items':r[v.e.VERSION]}, {}, dict(r,schemaVersion=v.e.VERSION)):
            self.assertEqual(v.e.inspect(bad,c,e)['status'],'invalid_response')
        changed=dict(c,targets=[],selectionExpectations=[],id='hidden')
        self.assertEqual(v.messages(c,e),v.messages(changed,e))
        data=v.model_input(c,e)
        self.assertNotIn('entityIds',json.dumps(data))
        self.assertNotIn('scene:',json.dumps(data))
        self.assertTrue(all(set(ref)=={'kind','ambiguous','segmentId','quote','occurrence'}
                            for ref in data['entityRefs'].values()))
        altered=copy.deepcopy(e)
        altered['scene:ambiguous']=dict(kind='scene_boundary',mentions=['封山线'])
        self.assertTrue(any(ref['ambiguous'] for ref in v.model_input(c,altered)['entityRefs'].values()))
        v.check_prompt_binding()
        content=v.PROMPT.read_text()
        example,_=json.JSONDecoder().raw_decode(content[content.index('{"'+v.e.VERSION+'"'):])
        self.assertEqual(set(example[v.e.VERSION][0]['position']),{'value','polarity'})
        with patch.object(v,'PROMPT',v.previous.PROMPT), patch.object(v.f,'_gateway') as gateway:
            with self.assertRaises(ValueError):v.check_prompt_binding()
            gateway.assert_not_called()

    def test_frozen_v1_failures_are_not_reinterpreted_as_new_protocol(self):
        before=v.f.sha(v.previous.OUTPUT)
        report=json.loads(v.previous.OUTPUT.read_text())
        for c,row in zip(self.fixture['cases'],report['cases']):
            raw=json.loads(row['calls'][0]['content'])
            self.assertEqual(v.e.inspect(raw,c,self.fixture['scenes'][c['sceneKey']])['status'],'invalid_response')
        self.assertEqual(v.f.sha(v.previous.OUTPUT),before)
        self.assertEqual(v.previous.audit()['summary']['matched'],7)

    def test_mock_run_is_auditable_once_only_without_live_gateway(self):
        recorder=SimpleNamespace(calls=[])
        def complete(messages):
            c=self.fixture['cases'][len(recorder.calls)]
            content=json.dumps(proposal(c,self.fixture['scenes'][c['sceneKey']]))
            recorder.calls.append(dict(messages=messages,content=content,rawResponse=json.dumps({'choices':[{'message':{'content':content}}]})))
            return SimpleNamespace(content=content)
        recorder.complete_json=complete
        with TemporaryDirectory() as directory, patch.object(v.f,'writer_config_from_env',return_value=dict(
                base_url='unused',api_key='test',model='test',route='test')),patch.object(v.f,'_gateway',return_value=recorder):
            path=Path(directory)/'report.json';r=v.run(path)
            self.assertEqual(r['summary']['matched'],18)
            self.assertEqual(v.audit(path)['summary'],r['summary'])
            with self.assertRaises(FileExistsError):v.run(path)
            self.assertEqual(len(recorder.calls),18)
