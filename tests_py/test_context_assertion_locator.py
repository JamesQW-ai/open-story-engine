import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_support import context_assertion_locator as v
from test_support import context_assertion_locator_replay as replay


class AssertionLocatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture, cls.controls, cls.designs = replay.load_inputs()

    def sample(self, cid):
        c = copy.deepcopy(next(c for c in self.fixture['cases'] if c['id'] == cid))
        row = next(r for r in self.controls if r['caseId'] == cid)
        return c, self.fixture['scenes'][c['sceneKey']], row

    def test_inputs_keep_one_lossless_source_entities_and_tasks_without_gold(self):
        for c in self.fixture['cases']:
            entities = self.fixture['scenes'][c['sceneKey']]
            old = v.previous.model_input(c, entities)
            segments = v.previous.frame.extraction.segments(c, entities)
            for encoding in ('quote', 'offset'):
                data = v.project_input(c, entities, encoding)
                self.assertEqual(''.join(p if isinstance(p, str) else p['text'] for p in data['source']), c['draft'])
                self.assertEqual(data['tasks'], old['tasks'])
                self.assertEqual(set(data), {'source', 'entityRefs', 'tasks'})
                hidden = dict(c, id='SECRET', targets=[], selectionExpectations=[], frameAttributions={})
                self.assertEqual(data, v.project_input(hidden, entities, encoding))
                for rid, old_ref in old['entityRefs'].items():
                    ref = data['entityRefs'][rid]
                    self.assertEqual((ref['kind'], ref['ambiguous']), (old_ref['kind'], old_ref['ambiguous']))
                    segment = segments[old_ref['segmentId']]
                    positions = [i for i in range(len(segment['quote']))
                                 if segment['quote'].startswith(old_ref['quote'], i)]
                    expected = segment['start']+positions[old_ref['occurrence']]
                    resolved = v.resolve(ref['anchor'], v.units(c), v.units(c), encoding)
                    self.assertEqual((resolved['start'], resolved['quote']), (expected, old_ref['quote']))

    def test_controls_preserve_known_intervals_and_full_condition_units(self):
        for c, row in zip(self.fixture['cases'], self.controls):
            entities = self.fixture['scenes'][c['sceneKey']]
            original = copy.deepcopy(row['control'])
            segments = v.previous.frame.extraction.segments(c, entities)
            q = v.project_control(original, c, entities)
            o = v.project_control(original, c, entities, 'offset')
            self.assertEqual(v.evidence_spans(q, c), v.evidence_spans(o, c, 'offset'))
            for old_d, d in zip(original[v.previous.VERSION], q[v.VERSION]):
                for old_item, item in zip(old_d.get('items', []), d.get('items', [])):
                    core, = item['core']
                    resolved = v.resolve(core, v.units(c), v.units(c))
                    self.assertEqual((resolved['start'], resolved['end']),
                        (segments[old_item['core'][0]]['start'], segments[old_item['core'][-1]]['end']))
                    for old_limit, limit in zip(old_item['conditions'], item['conditions']):
                        self.assertEqual(old_limit['units'], limit['units'])
                        for uid in limit['units']:
                            self.assertEqual(v.select_unit(uid, v.units(c), v.units(c))['quote'], v.units(c)[uid]['quote'])
            self.assertEqual(original, row['control'])

    def test_fine_spans_separate_narration_cue_and_quote_without_semantic_claim(self):
        for row in self.designs:
            result = replay.locate_design(row)
            self.assertEqual(result['semanticStatus'], 'unverified')
            self.assertFalse(result['productionEnablement'])
        dual = replay.locate_design(next(d for d in self.designs if d['id'] == 'dual_origin'))
        spans = [s['resolved'] for s in dual['selections']]
        self.assertTrue(all(a['end'] <= b['start'] for a, b in zip(spans, spans[1:])))
        self.assertEqual([s['quote'] for s in spans], ['沈砚秋站在议事殿内', '说', '我在殿外'])

    def test_unit_occurrence_is_recomputed_instead_of_reusing_segment_occurrence(self):
        c, entities, _ = self.sample('control:mixed_people')
        data = v.project_input(c, entities)
        halls = [r['anchor'] for r in data['entityRefs'].values() if r['anchor'][1] == '议事殿']
        self.assertEqual([r[2] for r in halls], [0, 1])
        a, b = [v.resolve(ref, v.units(c), v.units(c)) for ref in halls]
        self.assertLess(a['start'], b['start'])
        row = next(d for d in self.designs if d['id'] == 'repeated_quote')
        spans = replay.locate_design(row)['selections']
        self.assertEqual([s['quoteRef'][2] for s in spans], [0, 1])
        self.assertLess(spans[0]['resolved']['end'], spans[1]['resolved']['start'])

    def test_bad_references_and_cross_unit_quotes_fail_without_repair(self):
        c, _, _ = self.sample('gate:both_paraphrase')
        table = v.units(c)
        uid, unit = next(iter(table.items()))
        for ref in ([uid+'-S1', unit['quote'], 0], [uid, '', 0], [uid, '不存在', 0],
                    [uid, unit['quote'], True], [uid, unit['quote'], -1],
                    [uid, unit['quote'], 1], [uid, unit['quote'], 0, 'extra']):
            with self.assertRaises(ValueError): v.resolve(ref, table, table)
        with self.assertRaises(ValueError): v.resolve([uid, unit['quote'], 0], table, [])
        with self.assertRaises(ValueError): v.resolve([uid, c['draft'], 0], table, table)
        with self.assertRaises(ValueError): v.select_unit(uid+'-S1', table, table)
        with self.assertRaises(ValueError): v.select_unit([uid, unit['quote'], 0], table, table)

    def test_offsets_use_codepoints_and_reject_bool_float_empty_and_overflow(self):
        c = dict(draft='沈砚秋写下“殿内🙂殿内”。')
        table = v.units(c)
        uid = next(iter(table))
        span = v.resolve([uid, '殿内', 1], table, table)
        offset = v._encode(uid, span['start'], span['end'], table, 'offset')
        self.assertEqual(v.resolve(offset, table, table, 'offset'), span)
        for ref in ([uid, True, 3], [uid, 1.0, 3], [uid, -1, 3], [uid, 3, 3], [uid, 0, 999]):
            with self.assertRaises(ValueError): v.resolve(ref, table, table, 'offset')
        # A valid but wrong offset is a different literal selection, not a semantic rejection.
        wrong = v.resolve([uid, 0, 3], table, table, 'offset')
        self.assertEqual(wrong['quote'], '沈砚秋')

    def test_existing_malformed_control_is_not_silently_migrated(self):
        c, entities, row = self.sample('two_people:first_operator')
        bad = copy.deepcopy(row['control'])
        bad[v.previous.VERSION][0]['items'][0]['nonPremiseUnits'] = ['P1-U2-S1']
        with self.assertRaisesRegex(ValueError, '旧控制无效'): v.project_control(bad, c, entities)

    def test_artifact_rebuild_tamper_and_no_gateway(self):
        with patch.object(replay, 'load_inputs', return_value=(self.fixture, self.controls, self.designs)), \
                patch.object(replay.f, '_gateway') as gateway:
            report = replay.build()
            self.assertEqual(report['newModelCalls'], 0)
            self.assertEqual(len(report['originalFailures']), 5)
            self.assertFalse(report['readyForModelEvaluation'])
            with TemporaryDirectory() as d:
                path = Path(d)/'result.json'
                replay.f.save_checkpoint(path, report, create=True)
                self.assertTrue(replay.audit(path)['allRowsReproduced'])
                bad = copy.deepcopy(report)
                bad['summary']['quoteInputChars'] -= 1
                path.write_text(json.dumps(bad))
                with self.assertRaisesRegex(ValueError, '不可复现'): replay.audit(path)
                with self.assertRaises(FileExistsError): replay.f.save_checkpoint(path, report, create=True)
            gateway.assert_not_called()

    def test_pinned_fixture_and_prior_audit_drift_fail_closed(self):
        with TemporaryDirectory() as d:
            path = Path(d)/'bad.json'
            path.write_text('{}')
            with patch.object(replay, 'FIXTURE', path):
                with self.assertRaisesRegex(ValueError, '哈希变化'): replay.load_inputs()
            with patch.object(replay.previous, 'AUDIT', path):
                with self.assertRaisesRegex(ValueError, '哈希变化'): replay._prior()
