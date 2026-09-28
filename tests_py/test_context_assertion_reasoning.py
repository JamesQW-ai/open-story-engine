import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_assertion_reasoning_eval as run


class ReasoningProbeTests(unittest.TestCase):
    expected_cap=2048
    expected_cases=12
    @classmethod
    def setUpClass(cls):
        cls.fixture,cls.refs,cls.prior=run.prepare()

    def recorder(self,fail=False):
        rec=SimpleNamespace(calls=[],gateway=SimpleNamespace(max_tokens=8192,reasoning_effort='none'))
        def complete(messages):
            content=json.dumps(self.refs[len(rec.calls)]['reference'],ensure_ascii=False)
            call=dict(messages=messages,content=content,rawResponse=json.dumps(dict(choices=[dict(message=dict(content=content))])))
            if fail:call.update(error='unsupported reasoning profile',errorType='LlmError')
            rec.calls.append(call)
            if fail:raise run.f.LlmError(call['error'],code='transport_error')
            return SimpleNamespace(content=content)
        rec.complete_json=complete
        return rec

    def test_same_prompt_and_inputs_for_declared_subset(self):
        self.assertEqual([c['id'] for c in self.fixture['cases']],run.CASE_IDS)
        self.assertEqual(len(self.refs),self.expected_cases)
        for c in self.fixture['cases']:
            e=self.fixture['scenes'][c['sceneKey']]
            self.assertEqual(run.messages(c,e),run.baseline.messages(c,e))

    def test_budget_profile_raw_audit_and_fail_stop(self):
        for fail in (False,True):
            with TemporaryDirectory() as d,patch.object(run,'prepare',return_value=(self.fixture,self.refs,self.prior)), \
                    patch.object(run.f,'writer_config_from_env',return_value=dict(base_url='unused',api_key='test',model='test',route='test')):
                p=Path(d)/'report.json';rec=self.recorder(fail)
                with patch.object(run.f,'_gateway',return_value=rec):report=run.run(p)
                self.assertEqual(rec.gateway.max_tokens,self.expected_cap)
                self.assertEqual(rec.gateway.reasoning_effort,'low')
                self.assertEqual(report['actualCalls'],1 if fail else self.expected_cases)
                self.assertTrue(run.audit(p)['allScoresReproduced'])
                bad=copy.deepcopy(report);bad['requestedReasoningEffort']='none';p.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):run.audit(p)
                with patch.object(run.f,'_gateway') as factory:
                    with self.assertRaises(FileExistsError):run.run(p)
                    factory.assert_not_called()
