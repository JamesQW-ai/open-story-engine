import copy
import json
import unittest

from test_support import context_assertion_binding_audit as binding
from test_support import context_assertion_quote_eval as source


class BindingAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture,cls.references,_=source.prepare()

    def test_references_never_become_semantic_approval(self):
        for c,r in zip(self.fixture['cases'],self.references):
            result=binding.audit(r['reference'],c,self.fixture['scenes'][c['sceneKey']])
            self.assertEqual(result['status'],'unverified',c['id'])
            self.assertFalse(result['acceptance'])
            self.assertFalse(result['productionEnablement'])

    def test_recorded_named_mismatch_is_reported_without_rewriting(self):
        c=next(c for c in self.fixture['cases'] if c['id']=='frame:other_report')
        e=self.fixture['scenes'][c['sceneKey']]
        for name in ('context-assertion-quote-eval-2026-09-27.json','context-assertion-quote-v2-eval-2026-09-27.json'):
            report=json.loads(source.OUTPUT.with_name(name).read_text())
            response=next(r['proposedResponse'] for r in report['cases'] if r['caseId']==c['id'])
            original=copy.deepcopy(response);result=binding.audit(response,c,e)
            self.assertEqual(response,original)
            self.assertEqual(result['status'],'binding_conflict')
            self.assertEqual(len(result['issues']),1)
            self.assertNotEqual(result['issues'][0]['expectedEntity'],result['issues'][0]['mentionedEntity'])

    def test_invalid_reference_does_not_reach_identity_comparison(self):
        c=self.fixture['cases'][0];e=self.fixture['scenes'][c['sceneKey']]
        result=binding.audit({},c,e)
        self.assertEqual(result['status'],'invalid_source')
