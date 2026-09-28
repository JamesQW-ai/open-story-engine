import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_mention_contrast_eval as v


def control(case, entities):
    refs = v.e.previous.entity_refs(case, entities)
    items = []
    for expected in case['selectionExpectations']:
        position, span = expected['position'], expected['spans'][0]
        roles = {}
        for role, key in (('subject', 'subject'), ('boundary', 'object')):
            roles[role] = next(rid for rid, ref in refs.items()
                               if ref['entityIds'] == [position[key]] and ref['segmentId'] in span['core'])
        cue = expected['controlCue']
        items.append(dict(position={k: position[k] for k in ('value', 'polarity')}, reason='offline control',
                          **roles, core=copy.deepcopy(span['core']), limitations=[
                              dict(kind='modality', cue=copy.deepcopy(cue), premiseUnits=[])] if cue else []))
    return {v.e.VERSION: items}


class MentionContrastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = v.load_cases()

    def case(self, cid):
        c = next(c for c in self.fixture['cases'] if c['id'] == cid)
        return copy.deepcopy(c), self.fixture['scenes']['hall']

    def test_fourteen_labeled_positions_and_alternative_spans(self):
        self.assertEqual(sum(len(c['targets']) for c in self.fixture['cases']), 14)
        for c in self.fixture['cases']:
            entities = self.fixture['scenes']['hall']
            r = control(c, entities)
            result = v.e.assess(r, c, entities)
            self.assertEqual(result['status'], 'matched', (c['id'], result))
            self.assertFalse(result['productionEnablement'])
            self.assertEqual(result['semanticStatus'], 'unverified')
            for n, expected in enumerate(c['selectionExpectations']):
                for span in expected['spans'][1:]:
                    altered = copy.deepcopy(r)
                    altered[v.e.VERSION][n]['core'] = span['core']
                    self.assertEqual(v.e.assess(altered, c, entities)['status'], 'matched')

    def test_frozen_messages_exclude_pairing_labels_and_expected_cues(self):
        v.check_prompt_binding()
        for c in self.fixture['cases']:
            entities = self.fixture['scenes']['hall']
            self.assertEqual(v.messages(c, entities), v.previous.messages(c, entities))
            hidden = dict(c, id='secret', pairId='secret', variant='secret', targets=[], selectionExpectations=[])
            self.assertEqual(v.messages(c, entities), v.messages(hidden, entities))
            data = json.loads(v.messages(c, entities)[1]['content'])
            self.assertEqual(set(data), {'source', 'entityRefs'})
            source = ''.join(x if isinstance(x, str) else ''.join(x['segments'].values()) for x in data['source'])
            self.assertEqual(source, c['draft'])

    def test_latest_real_false_positive_remains_mismatched(self):
        report = json.loads(v.previous.OUTPUT.read_text())
        row = next(r for r in report['cases'] if r['caseId'] == 'hall:word_mention')
        case = next(c for c in v.previous.load_cases()['cases'] if c['id'] == row['caseId'])
        raw = json.loads(row['calls'][0]['content'])
        result = v.e.assess(raw, case, self.fixture['scenes']['hall'])
        self.assertEqual(result['status'], 'mismatched')
        self.assertEqual(result['proposedResponse'], raw)
        self.assertEqual(v.previous.audit(), json.loads(v.previous.AUDIT.read_text()))

    def test_mention_inside_core_is_still_not_a_modality(self):
        c, entities = self.case('in_core:mention')
        r = control(c, entities)
        r[v.e.VERSION][0]['limitations'] = [dict(kind='modality',
            cue=dict(segmentId='P1-U1-S1', quote='可能', occurrence=0), premiseUnits=[])]
        self.assertEqual(v.e.assess(r, c, entities)['status'], 'mismatched')

    def test_genuine_external_cue_cannot_be_dropped(self):
        c, entities = self.case('denial:operator')
        r = control(c, entities)
        item = r[v.e.VERSION][0]
        self.assertNotIn(item['limitations'][0]['cue']['segmentId'], item['core'])
        self.assertEqual(v.e.assess(r, c, entities)['status'], 'matched')
        item['limitations'] = []
        self.assertEqual(v.e.assess(r, c, entities)['status'], 'mismatched')

    def test_fixture_drift_stops_before_gateway_or_file_creation(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'fixture.json'
            data = json.loads(v.FIXTURE.read_text())
            data['cases'][0]['targets'][0]['expectedLimitations'] = ['modality']
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, '夹具或标签'):
                v.load_cases(path)
            with patch.object(v, 'PROMPT', v.previous.previous.PROMPT), patch.object(v.f, '_gateway') as gateway:
                output = Path(d)/'output.json'
                with self.assertRaises(ValueError):
                    v.run(output)
                gateway.assert_not_called()
                self.assertFalse(output.exists())

    def recorder(self, fail=False):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(control(c, self.fixture['scenes']['hall']))
            recorder.calls.append(dict(messages=messages, content=content,
                                       rawResponse=json.dumps({'choices': [{'message': {'content': content}}]})))
            if fail:
                raise v.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_mock_twelve_once_only_calls_audit_and_tamper_detection(self):
        recorder = self.recorder()
        with TemporaryDirectory() as d, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder) as gateway:
            path = Path(d)/'output.json'
            report = v.run(path)
            self.assertEqual(report['summary']['matched'], 12)
            self.assertEqual(report['actualCalls'], 12)
            self.assertEqual(gateway.call_args.args[1], 12)
            self.assertEqual(v.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                v.run(path)
            self.assertEqual(len(recorder.calls), 12)
            report['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '实际输入'):
                v.audit(path)

    def test_first_transport_failure_stops_diagnostic(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as d, patch.object(v.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(v.f, '_gateway', return_value=recorder):
            report = v.run(Path(d)/'output.json')
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 11)
