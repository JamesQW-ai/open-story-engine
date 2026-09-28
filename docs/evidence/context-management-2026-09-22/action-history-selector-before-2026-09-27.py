"""Action-review history references drawn from the writer's frozen context."""
import json

from .context_bundle import ContextBundleError, context_fact_terms


def action_review_history(context, action, bundle, evidence):
    """Select references, never copy or truncate another chronological history."""
    parent_id = context['parent']['id']
    references = []
    omitted = []
    missing = []
    context_id = context_hash = None
    if bundle is None:
        # Compatibility has no frozen selection/provenance. Do not silently
        # promote the whole old history to equivalent bundle evidence.
        source = f'history-{parent_id}-P1'
        if source in evidence:
            references.append({'sourceId': source, 'reason': 'parent_summary'})
        else:
            missing.append('parent_summary')
        mode = 'legacy_parent_only'
    else:
        projection = bundle.project('chapter')
        branch = projection['branch']
        if (branch.get('parentBranchId') or branch.get('branchId')) != parent_id:
            raise ContextBundleError('行动审核历史与当前父分支不一致。')
        context_id = projection['contextId']
        context_hash = projection['contextSha256']
        window = projection['continuityWindow']
        parent_source = 'branch:lineage:' + parent_id
        # Parent context protects pronouns and immediate continuity, even when
        # the player's instruction contains no named entity or lexical match.
        parent_text = next((item['content'] for item in window
                            if item['sourceId'] == parent_source), '')
        terms = context_fact_terms(action) | context_fact_terms(parent_text)
        contract = context.get('resultContract') or {}
        explicit_sources = {
            ref['id'] for item in (contract.get('scenePlan') or {}).get('knowledge', [])
            for ref in item.get('sources', [])
        }
        for item in window:
            source = item['sourceId']
            text = item['content']
            if source not in evidence or evidence[source] != text:
                raise ContextBundleError('行动审核连续性来源缺失或内容不一致：' + source)
            if source == parent_source:
                reason = 'parent_continuity'
            elif source in explicit_sources:
                reason = 'scene_plan_source'
            elif any(term in text for term in terms):
                reason = 'action_or_parent_overlap'
            else:
                omitted.append(source)
                continue
            references.append({'sourceId': source, 'reason': reason})
        if not parent_text:
            missing.append('parent_summary')
        # Memory has already been selected for this turn and validated for
        # lifecycle, branch, visibility and source authority by ContextBundle.
        # Re-filtering it lexically here could drop an unresolved obligation.
        seen = {item['sourceId'] for item in references}
        for memory in projection['dynamicMemory']:
            for source in memory['sourceIds']:
                if source not in evidence:
                    raise ContextBundleError('行动审核动态记忆缺少公开来源：' + source)
                if source not in seen:
                    references.append({'sourceId': source, 'reason': 'selected_dynamic_memory'})
                    seen.add(source)
        mode = 'context_bundle'
    history = {
        'schemaVersion': 'action-review-history/1',
        'contextId': context_id, 'contextSha256': context_hash,
        'sourceReferences': references,
        'coverage': mode, 'missingContext': missing,
        'interpretation': 'sourceReferences 引用 sceneEvidence 中的历史依据，不代表完整历史。'
                          '未选入或缺少依据不表示事件未发生；当前状态以 authoritativeState 为准。',
    }
    audit = {
        'source': mode, 'contextId': context_id, 'contextSha256': context_hash,
        'selectedSourceIds': [item['sourceId'] for item in references],
        'selectionReasons': references,
        'omittedContinuitySourceIds': omitted, 'missingContext': missing,
        'serializedCharacters': len(json.dumps(history, ensure_ascii=False)),
        'duplicatedEvidenceCharacters': 0,
    }
    return history, audit
