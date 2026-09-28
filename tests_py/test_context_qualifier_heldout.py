import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_support import context_qualifier_heldout as h


def response(case):
    """Hand-built harness controls, separate from saved model responses."""
    units = h.f.indexed_input(case)['units']
    items = []
    for index, target in enumerate(case['targets']):
        uid = target['unitIds'][-1]
        text = units[uid]['quote']
        phrase = text.split('而')[index] if case['id'] == 'two_people' else text
        offset = text.index(phrase)
        def ref(quote, unit=uid, start=offset):
            full = units[unit]['quote']
            occurrence = sum(full.startswith(quote, p) for p in range(start))
            return dict(unitId=unit, quote=quote, occurrence=occurrence)
        subject = next(n for n in ('沈砚秋', '守门长老', '开门长老') if n in phrase)
        boundary = '议事殿' if '议事殿' in phrase else '殿'
        position = [ref(phrase)]
        limits = []
        for kind in target['expectedLimitations']:
            if kind == 'condition':
                premise_uid = target['unitIds'][0]
                cue = ref('若', premise_uid, 0)
                premise = [ref(units[premise_uid]['quote'], premise_uid, 0)]
            else:
                cue = ref(next(word for word in ('想必', '大概', '也许', '没有人敢断定') if word in phrase))
                premise = []
            limits.append(dict(kind=kind, cue=cue, scope=copy.deepcopy(position), premise=premise))
        items.append(dict(unitIds=list(target['unitIds']), status=target['expectedStatus'],
                          normal=copy.deepcopy(target['expectedNormal']), reason='harness control',
                          anchors=dict(subject=ref(subject), boundary=ref(boundary), position=position), limitations=limits))
    return dict(items=items)


class HeldoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = h.load_cases()

    def test_source_scene_and_prompt_are_bound_without_gold_injection(self):
        self.assertGreaterEqual(self.fixture['source']['cjk'], 100000)
        self.assertEqual(sum(len(c['targets']) for c in self.fixture['cases']), 10)
        for c in self.fixture['cases']:
            messages = h.messages(c, self.fixture['entities'])
            self.assertEqual(messages[0]['content'], h.v.PROMPT.read_text())
            payload = json.loads(messages[1]['content'])
            self.assertEqual(set(payload), {'draft', 'units', 'entities'})
            changed = copy.deepcopy(c)
            changed.update(targets=[], labelRationale='private', origin='changed')
            self.assertEqual(messages, h.messages(changed, self.fixture['entities']))
            self.assertEqual(h.e.assess(response(c), c, self.fixture['entities'])['status'], 'matched')

    def test_source_and_freeze_tampering_rejected(self):
        for kind in ('source', 'freeze', 'outside', 'false_excerpt', 'duplicate_gold'):
            data = copy.deepcopy(self.fixture)
            if kind == 'source':
                data['source']['sha256'] = '0'*64
            elif kind == 'freeze':
                key = next(iter(data['frozenImplementationHashes']))
                data['frozenImplementationHashes'][key] = '0'*64
            elif kind == 'outside':
                text = (h.f.ROOT/data['source']['path']).read_text()
                start = text.index('沈砚秋')
                data['entitySources']['scene:shen'] = [dict(start=start, end=start+3, quote='沈砚秋')]
            elif kind == 'false_excerpt':
                data['cases'][0]['draft'] = '不在原文里的假句。'
                data['cases'][0]['draftSha256'] = hashlib.sha256(data['cases'][0]['draft'].encode()).hexdigest()
            else:
                data['cases'][0]['targets'] *= 2
            with TemporaryDirectory() as directory:
                path = Path(directory)/'fixture.json'
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    h.load_cases(path)

    def test_two_people_cannot_share_a_single_result(self):
        c = next(c for c in self.fixture['cases'] if c['id'] == 'two_people')
        r = response(c)
        r['items'].pop()
        score = h.e.assess(r, c, self.fixture['entities'])
        self.assertEqual(score['status'], 'mismatched')
        self.assertEqual(score['extractionScore']['missingTargets'], [1])

    def recorder(self, fail=False):
        recorder = SimpleNamespace(calls=[])
        def complete(messages):
            c = self.fixture['cases'][len(recorder.calls)]
            content = json.dumps(response(c))
            recorder.calls.append(dict(messages=messages, content=content))
            if fail:
                raise h.f.LlmError('HTTP 400', code='transport_error')
            return SimpleNamespace(content=content)
        recorder.complete_json = complete
        return recorder

    def test_bounded_calls_no_overwrite_and_reproducible_audit(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(h.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(h.f, '_gateway', return_value=recorder):
            path = Path(directory)/'report.json'
            report = h.run(path)
            self.assertEqual(report['actualCalls'], 9)
            self.assertEqual(report['summary']['matched'], 9)
            self.assertEqual(h.audit(path)['summary'], report['summary'])
            with self.assertRaises(FileExistsError):
                h.run(path)
            self.assertEqual(len(recorder.calls), 9)
            report['cases'][0]['calls'][0]['messages'][1]['content'] = '{}'
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, '实际输入'):
                h.audit(path)

    def test_transport_failure_stops(self):
        recorder = self.recorder(fail=True)
        with TemporaryDirectory() as directory, patch.object(h.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(h.f, '_gateway', return_value=recorder):
            report = h.run(Path(directory)/'report.json')
            self.assertEqual(report['actualCalls'], 1)
            self.assertEqual(report['summary']['not_run'], 8)

    def test_save_failure_stops(self):
        recorder = self.recorder()
        with TemporaryDirectory() as directory, patch.object(h.f, 'writer_config_from_env', return_value=dict(
                base_url='unused', api_key='test', model='test', route='test')), patch.object(h.f, '_gateway', return_value=recorder), patch.object(
                h.f, 'save_checkpoint', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(h.f.CheckpointError):
                h.run(Path(directory)/'report.json')
            self.assertEqual(len(recorder.calls), 1)
