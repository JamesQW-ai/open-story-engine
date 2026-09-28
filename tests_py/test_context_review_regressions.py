"""Fixed longform counterexamples must not weaken the existing rejection gate."""
import copy
import hashlib
import json
import unittest
from open_story_engine.reader_actions import ActionEvidenceError

from open_story_engine.reader_scene_review import (SceneReviewError, grounding_claims, grounding_input_evidence,
    public_scene_evidence, reject_review_issues, validate_grounding, validate_knowledge_access, validate_scope,
    validate_scene_boundaries)
from test_support.context_review_regressions import FIXTURE, ROOT, assess


class ContextReviewRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE.read_text())
        cls.cases = {c['id']: c for c in cls.fixture['cases']}

    def clean_review(self, case):
        return {'issues': [], 'sceneChecks': [
            {'paragraphId': f'P{i+1}', 'playerDecision': 'none', 'background': 'none',
             'sources': [], 'quote': '', 'issue': ''}
            for i, _ in enumerate(case['draft'].split('\n\n'))]}

    def test_original_failure_and_review_remain_exactly_traceable(self):
        source = ROOT / self.fixture['sourceArtifact']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), self.fixture['sourceSha256'])
        call = json.loads(source.read_text())['variants']['production']['calls'][8]
        payload = json.loads(call['messages'][-1]['content'])
        case = self.cases['recorded_mixed_failure']
        self.assertEqual(case['draft'], '\n\n'.join(payload['draft'].values()))
        self.assertEqual(self.fixture['recordedReview'], json.loads(call['content']))
        result = assess(self.fixture['recordedReview'], case, self.fixture['sceneEvidence'])
        self.assertEqual(result['extraIssues'], [('P4', 'action')])
        with self.assertRaises(SceneReviewError):
            reject_review_issues(self.fixture['recordedReview'], case['draft'])

    def test_review_cannot_pass_by_omitting_known_deadline_violation(self):
        case = self.cases['unsupported_deadline']
        result = assess(self.clean_review(case), case, self.fixture['sceneEvidence'])
        self.assertEqual(result['status'], 'mismatch')
        self.assertEqual(result['missedIssues'], [('P2', 'background')])

    def test_wrong_action_rejection_on_legal_wait_is_counted_as_extra(self):
        case = self.cases['request_then_wait']
        review = self.clean_review(case)
        review['issues'] = [{'paragraphId': 'P2', 'type': 'action', 'quote': '你愿意担责吗',
                             'reason': '未替玩家回答，选择被悬置'}]
        self.assertEqual(assess(review, case, self.fixture['sceneEvidence'])['extraIssues'], [('P2', 'action')])

    def test_empty_or_incomplete_checks_cannot_count_as_correct_rejection(self):
        case = self.cases['unsupported_deadline']
        review = self.clean_review(case)
        review['issues'] = [{'paragraphId': 'P2', 'type': 'background', 'quote': '撑不到天亮', 'reason': '缺证'}]
        review['sceneChecks'].pop()
        self.assertEqual(assess(review, case, self.fixture['sceneEvidence'])['status'], 'invalid_review')

    def test_legitimate_wait_requires_complete_review_and_valid_sources(self):
        case = self.cases['request_then_wait']
        review = self.clean_review(case)
        self.assertEqual(assess(review, case, self.fixture['sceneEvidence'])['status'], 'matched')
        invalid = copy.deepcopy(review)
        invalid['sceneChecks'][0].update(background='supported', sources=[{'id': 'missing', 'quote': '他还活着'}])
        self.assertEqual(assess(invalid, case, self.fixture['sceneEvidence'])['status'], 'invalid_review')

    def test_new_dialogue_is_not_hidden_by_non_factual_narration(self):
        for opening, closing in (('“', '”'), ('「', '」'), ('『', '』')):
            with self.subTest(quotes=opening):
                narration = '你把手从怀里抽出来，文书没有递出去，'
                speech = opening + '再拖，他就真没气了。' + closing
                body = narration + speech
                claims = grounding_claims(body)
                self.assertEqual([c['claim'] for c in claims.values()], [narration, speech])
                incomplete = {'checks': [{'id': 'P1-C1', 'kind': 'current',
                    'verdict': 'supported', 'sources': [], 'reason': '没有递出文书'}]}
                with self.assertRaises(ValueError):
                    validate_grounding(incomplete, claims, evidence={})
                rejected = copy.deepcopy(incomplete)
                rejected['checks'].append({'id': 'P1-C2', 'kind': 'inference',
                    'verdict': 'unsupported', 'sources': [], 'reason': '缺少延误导致死亡的依据'})
                with self.assertRaises(SceneReviewError):
                    validate_grounding(rejected, claims, evidence={})

    def recorded_grounding(self, case_id):
        path = ROOT / 'docs/evidence/context-management-2026-09-22/context-final-guard-validation-2026-09-25.json'
        report = json.loads(path.read_text())
        source = ROOT / report['sourceArtifact']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), report['sourceSha256'])
        case = next(c for c in report['cases'] if c['caseId'] == case_id)
        payload = json.loads(case['call']['messages'][1]['content'])
        review = json.loads(case['call']['content'])
        self.assertEqual('\n\n'.join(payload['draft'].values()), self.cases[case_id]['draft'])
        return payload, review

    def test_recorded_legal_wait_accepts_exact_projected_source_ids(self):
        payload, review = self.recorded_grounding('request_then_wait')
        before = copy.deepcopy((payload, review))
        with self.assertRaisesRegex(ValueError, '来源不在公开资料'):
            validate_grounding(review, payload['paragraphs'], evidence=self.fixture['sceneEvidence'])
        evidence = grounding_input_evidence(payload)
        body = '\n\n'.join(payload['draft'].values())
        validate_grounding(review, payload['paragraphs'], evidence=evidence)
        validate_knowledge_access(review, body, evidence, payload['people'], payload['playerId'])
        validate_scope(review, payload['requirements'], body)
        validate_scene_boundaries(review, body, payload['boundaries'])
        self.assertEqual((payload, review), before)
        self.assertNotIn('opening-3', evidence)

    def test_recorded_deadline_is_still_rejected_with_correct_source_ids(self):
        payload, review = self.recorded_grounding('recorded_mixed_failure')
        with self.assertRaises(SceneReviewError) as caught:
            validate_grounding(review, payload['paragraphs'], evidence=grounding_input_evidence(payload))
        self.assertTrue(any(v['paragraphId'] == 'P2' and '撑不到天亮' in v['claim']
                            for v in caught.exception.violations))

    def test_grounding_reference_does_not_fall_back_to_hidden_or_legacy_sources(self):
        payload, review = self.recorded_grounding('request_then_wait')
        ref = review['checks'][0]['sources'][0]
        for changes in ({'visibility': 'author_truth'}, {'visibility': 'character_known'},
                        {'validity': 'unknown'}, {'authority': 'style_only'}):
            with self.subTest(changes=changes):
                modified = copy.deepcopy(payload)
                item = next(e for e in modified['contextProjection']['allowedEvidence'] if e['sourceId'] == ref['id'])
                item.update(changes)
                modified['sceneEvidence'] = {ref['id']: ref['quote']}
                evidence = grounding_input_evidence(modified)
                self.assertNotIn(ref['id'], evidence)
                with self.assertRaisesRegex(ValueError, '来源不在公开资料'):
                    validate_grounding(review, payload['paragraphs'], evidence=evidence)

    def test_matching_text_cannot_rescue_wrong_or_missing_reference(self):
        payload, review = self.recorded_grounding('request_then_wait')
        evidence = grounding_input_evidence(payload)
        for ref_id in ('opening-3', 'module:opening:knownFacts:999'):
            modified = copy.deepcopy(review)
            modified['checks'][0]['sources'][0]['id'] = ref_id
            with self.assertRaisesRegex(ValueError, '来源不在公开资料'):
                validate_grounding(modified, payload['paragraphs'], evidence=evidence)

    def test_malformed_projection_cannot_enable_legacy_fallback(self):
        for projection in (None, {}, {'stage': 'chapter', 'allowedEvidence': []}):
            with self.assertRaises(ValueError):
                grounding_input_evidence({'contextProjection': projection, 'sceneEvidence': self.fixture['sceneEvidence']})
        payload, _ = self.recorded_grounding('request_then_wait')
        payload['contextProjection']['allowedEvidence'].append(payload['contextProjection']['allowedEvidence'][0])
        with self.assertRaisesRegex(ValueError, '编号重复'):
            grounding_input_evidence(payload)

    def test_legacy_non_projection_evidence_keeps_its_original_ids(self):
        payload = {'sceneEvidence': self.fixture['sceneEvidence']}
        result = grounding_input_evidence(payload)
        self.assertEqual(result, payload['sceneEvidence'])
        self.assertIsNot(result, payload['sceneEvidence'])

    def test_stale_review_quote_is_not_a_whole_paragraph_repair_target(self):
        path = ROOT / 'docs/evidence/context-management-2026-09-22/context-planner-smoke-2026-09-25-followup-v8.json'
        calls = json.loads(path.read_text())['variants']['production']['calls']
        payload = json.loads(calls[8]['messages'][1]['content'])
        review = json.loads(calls[8]['content'])
        stale = next(issue for issue in review['issues'] if '门落了' in issue['quote'])
        body = '\n\n'.join(payload['draft'].values())
        self.assertNotIn(stale['quote'], body)
        with self.assertRaisesRegex(ActionEvidenceError, '不能沿用旧稿问题'):
            reject_review_issues({'issues': [stale]}, body)

    def test_valid_current_rejection_survives_a_stale_quote_in_same_review(self):
        body = '你决定立即越线。'
        review = {'issues': [
            {'paragraphId': 'P1', 'type': 'background', 'quote': '已被删掉的旧句', 'reason': '旧问题'},
            {'paragraphId': 'P1', 'type': 'action', 'quote': '决定立即越线', 'reason': '未获授权'},
        ]}
        with self.assertRaises(SceneReviewError) as caught:
            reject_review_issues(review, body)
        self.assertEqual(len(caught.exception.violations), 1)
        self.assertEqual(caught.exception.violations[0]['claim'], '决定立即越线')

    def test_public_scene_evidence_excludes_prior_narrative_paragraphs(self):
        context = {
            'contract': {'openingContext': {
                'knownFacts': ['门将闭。'],
                'identity': '玩家在门外。',
            }},
            'lineage': [{
                'id': 'node-1',
                'summary': '玩家停在门外。',
                'playerDirection': '等待回应。',
                'narrativeText': '模型曾写出的具体但未确认细节。',
                'readerOutcome': {'action': {'summary': '已完成等待。'}},
            }],
        }
        evidence = public_scene_evidence(context)
        self.assertIn('opening-1', evidence)
        self.assertIn('history-node-1-P1', evidence)
        self.assertNotIn('模型曾写出的具体但未确认细节。', evidence.values())
        self.assertNotIn('等待回应。', ''.join(evidence.values()))
        self.assertEqual(
            [key for key in evidence if key.startswith('history-node-1-')],
            ['history-node-1-P1'],
        )


if __name__ == '__main__':
    unittest.main()
