import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_attribution_eval as v
from tests_py.test_context_qualifier_frame import control


class AttributionEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def recorder(self, mode=None):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(control(c, self.fixture['scenes'][c['sceneKey']]))
            if mode == 'malformed' and not recorder.calls:
                content = 'not json'
            raw = dict(choices=[dict(message=dict(content=content))],
                       usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            recorder.calls.append(dict(messages=messages, content=content, rawResponse=json.dumps(raw),
                                       observations=[dict(transport=dict(durationMs=20, httpStatus=200))]))
            if mode == 'transport':
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def execute(self, path, recorder):
        with patch.object(v, 'load_cases', return_value=copy.deepcopy(self.fixture)), \
                patch.object(v.f, 'writer_config_from_env', return_value=dict(
                    base_url='unused', api_key='test', model='test', route='test')), \
                patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            result = v.run(path)
            self.assertEqual(gateway.call_args.args[1], 44)
            return result

    def audit(self, path):
        with patch.object(v, 'load_cases', return_value=copy.deepcopy(self.fixture)):
            return v.audit(path)

    def test_only_one_paragraph_changes_input_is_identical_and_gold_is_hidden(self):
        self.assertIs(v.assess_content, v.previous.assess_content)
        self.assertIs(v.metrics, v.previous.metrics)
        old = v.previous.replay.PROMPT.read_text().split('\n\n')
        new = v.prompt_text().split('\n\n')
        self.assertEqual([i for i in range(len(old)) if old[i] != new[i]], [2])
        self.assertLessEqual(len(new[2]), len(old[2]))
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            self.assertEqual(v.messages(c, entities)[1:], v.previous.messages(c, entities)[1:])
            hidden = dict(c, id='SECRET', cohort='SECRET', targets=[], selectionExpectations=[],
                          frameAttributions={}, coverageExpectation={})
            self.assertEqual(v.messages(c, entities), v.messages(hidden, entities))

    def test_previous_real_failures_remain_failures_with_identical_scoring(self):
        report = json.loads(v.previous.OUTPUT.read_text())
        self.assertEqual(len(report['cases']), len(self.fixture['cases']))
        for c, row in zip(self.fixture['cases'], report['cases']):
            result = v.assess_content(row['calls'][0]['content'], c, self.fixture['scenes'][c['sceneKey']])
            self.assertEqual(result, {k:x for k,x in row.items() if k not in ('caseId','cohort','calls')})
        self.assertEqual(report['summary']['mismatched']+report['summary']['invalid_response'], 5)
        self.assertEqual(report['metrics']['wrongClassifications'], 6)

    def test_fixed_budget_audit_and_exclusive_output(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'result.json'; recorder=self.recorder()
            report=self.execute(path, recorder)
            self.assertEqual((report['summary']['matched'], report['summary']['scope_pending']), (40,4))
            self.assertEqual(report['metrics']['costs']['tokens']['total_tokens'],660)
            self.assertEqual(self.audit(path)['metrics'],report['metrics'])
            with self.assertRaises(FileExistsError): self.execute(path,recorder)
            self.assertEqual(len(recorder.calls),44)
            for mode in ('score','raw','input','cost','hash','boundary'):
                changed=copy.deepcopy(report)
                if mode=='score': changed['cases'][0]['status']='mismatched'
                elif mode=='raw': changed['cases'][0]['calls'][0]['content']='{}'
                elif mode=='input': changed['cases'][0]['calls'][0]['messages'][0]['content']='JSON changed'
                elif mode=='cost': changed['metrics']['costs']['tokens']['total_tokens']+=1
                elif mode=='hash': changed['inputHashes'].pop(str(v.PROMPT.relative_to(v.f.ROOT)))
                else: changed['productionEnablement']=True
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError): self.audit(path)

    def test_malformed_response_retained_without_retry(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'result.json'
            report=self.execute(path,self.recorder('malformed'))
            self.assertEqual(report['actualCalls'],44)
            self.assertEqual(report['summary']['invalid_response'],1)
            self.assertEqual(self.audit(path)['summary'],report['summary'])

    def test_transport_failure_stops_at_one_call(self):
        with TemporaryDirectory() as d:
            path=Path(d)/'result.json'
            report=self.execute(path,self.recorder('transport'))
            self.assertEqual(report['actualCalls'],1)
            self.assertEqual(report['summary']['not_run'],43)
            with self.assertRaisesRegex(ValueError,'实验未完成'): self.audit(path)

    def test_raw_checkpoint_precedes_score_and_save_failure_stops_calls(self):
        assess=v.assess_content
        with TemporaryDirectory() as d:
            path=Path(d)/'result.json'
            def checked(content,c,entities):
                row=json.loads(path.read_text())['cases'][-1]
                self.assertEqual(row['status'],'incomplete')
                self.assertEqual(row['calls'][0]['content'],content)
                return assess(content,c,entities)
            with patch.object(v,'assess_content',side_effect=checked): self.execute(path,self.recorder())
            save=v.f.save_checkpoint
            def fail(path,report,**kwargs):
                if report['actualCalls']: raise OSError('disk full')
                return save(path,report,**kwargs)
            recorder=self.recorder()
            with patch.object(v.f,'save_checkpoint',side_effect=fail):
                with self.assertRaises(v.f.CheckpointError): self.execute(Path(d)/'failed.json',recorder)
            self.assertEqual(len(recorder.calls),1)

    def test_prompt_drift_stops_before_output_or_gateway(self):
        with TemporaryDirectory() as d:
            bad=Path(d)/'prompt.md'; bad.write_text('JSON changed')
            out=Path(d)/'result.json'
            with patch.object(v,'PROMPT',bad), patch.object(v.f,'_gateway') as gateway:
                with self.assertRaisesRegex(ValueError,'哈希变化'): v.run(out)
                gateway.assert_not_called()
                self.assertFalse(out.exists())
