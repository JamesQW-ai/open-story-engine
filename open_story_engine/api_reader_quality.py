"""Evidence-bound reading notes; these never grant inventory or world state."""
import re

from .reader_scene_plan import MIN_SCENE_CJK, MAX_SCENE_CJK, cjk_character_count


def scene_pacing(contract, before, after):
    """A writing budget based on authorized work, never a completion score."""
    plan = (contract or {}).get('scenePlan')
    if plan:
        return {'level': 'dynamic', 'targetCjk': list(plan['targetCjk']),
                'reason': plan['lengthReason'], 'hardMinimum': False,
                'minCjk': MIN_SCENE_CJK, 'maxCjk': MAX_SCENE_CJK,
                'lengthPolicy': 'final_visible_prose_dynamic_80_1500'}
    # Legacy paths have no scene plan. Do not infer length from move/step counts.
    return {'level': 'brief' if (contract or {}).get('readingIntent') == 'brief' else 'unplanned',
            'targetCjk': None,
            'hardMinimum': False, 'minCjk': MIN_SCENE_CJK, 'maxCjk': MAX_SCENE_CJK,
            'lengthPolicy': 'final_visible_prose_dynamic_80_1500'}


def expand_scene_paragraphs(body, data, *, max_cjk=None):
    """Insert bounded detail without replacing the draft or its final handoff."""
    paragraphs = body.split('\n\n')
    insertions = data.get('insertions') if isinstance(data, dict) else None
    if not isinstance(insertions, list) or len(insertions) > 12:
        raise ValueError('场景补写缺少有效插入段落')
    additions = {}
    for item in insertions:
        if not isinstance(item, dict):
            raise ValueError('场景补写项格式无效')
        pid, before, after = item.get('paragraphId'), item.get('before', ''), item.get('after', '')
        if not isinstance(pid, str) or not re.fullmatch(r'P[1-9]\d*', pid) or not isinstance(before, str) or not isinstance(after, str):
            raise ValueError('场景补写锚点或正文无效')
        index = int(pid[1:]) - 1
        if index >= len(paragraphs) or index in additions or (index == 0 and before.strip()) or (index == len(paragraphs)-1 and after.strip()):
            raise ValueError('场景补写越过开篇或结尾边界')
        additions[index] = (before.strip(), after.strip())
    result = '\n\n'.join(text for i, original in enumerate(paragraphs)
                           for text in (additions.get(i, ('', ''))[0], original, additions.get(i, ('', ''))[1]) if text)
    cjk = cjk_character_count(result)
    if cjk > MAX_SCENE_CJK or len(result)-len(body) > MAX_SCENE_CJK:
        raise ValueError(f'场景补写后正文不得超过{MAX_SCENE_CJK}个汉字')
    # An oversized last insertion must not erase earlier useful expansion.
    # Remove whole anchor groups from the end, never cut a sentence or modify
    # the original. The accepted candidate still undergoes all scene checks.
    while max_cjk is not None and additions and cjk_character_count(result) > max_cjk:
        del additions[max(additions)]
        result = '\n\n'.join(text for i, original in enumerate(paragraphs)
                               for text in (additions.get(i, ('', ''))[0], original, additions.get(i, ('', ''))[1]) if text)
    return result


def validate_outcome(data, body, action, names):
    outcome = data.get('action')
    if not isinstance(outcome, dict) or outcome.get('status') not in ('performed', 'blocked'):
        raise ValueError('必须核对玩家行动是否实际执行，或尝试后遇到明确阻碍')
    summary, quote = outcome.get('summary'), outcome.get('evidence')
    # Models often add closing quotation marks to an excerpt from the middle
    # of a longer speech. Remove only those wrappers, never invent missing words.
    if isinstance(quote, str) and quote not in body and quote.strip('“”"') in body:
        quote = quote.strip('“”"')
    if not isinstance(summary, str) or not 0 < len(summary) <= 160:
        raise ValueError('行动结果须为简短的实际后果')
    if not isinstance(quote, str) or len(quote) < 8 or quote not in body:
        raise ValueError('行动结果必须引用正文中实际发生的行动，不能引用用户选项')
    if re.search(r'报警|联系警方|拨打110', action) and not re.search(r'报警|警方|110|一一零', body):
        raise ValueError('玩家要求报警，正文必须写出尝试及实际结果，不能用司机自行上报代替')
    result = {'action': {'status': outcome['status'], 'summary': summary, 'evidence': quote},
              'clues': [], 'relationships': []}
    # Optional notes are conservative: malformed/unquoted additions are omitted,
    # rather than regenerating otherwise valid prose to fill a notebook.
    clues = data.get('clues', [])
    for item in clues[:3] if isinstance(clues, list) else []:
        if (isinstance(item, dict) and isinstance(item.get('summary'), str)
                and 0 < len(item['summary']) <= 100 and isinstance(item.get('evidence'), str)
                and len(item['evidence']) >= 8 and item['evidence'] in body):
            result['clues'].append({'summary': item['summary'], 'evidence': item['evidence']})
    relations = data.get('relationships', [])
    for item in relations[:4] if isinstance(relations, list) else []:
        if (isinstance(item, dict) and item.get('source') in names and item.get('target') in names
                and item['source'] != item['target'] and item.get('label') in ('朋友', '同事', '协助', '信任', '质疑', '对立', '配合核查')
                and isinstance(item.get('evidence'), str) and len(item['evidence']) >= 8
                and item['evidence'] in body):
            result['relationships'].append({k: item[k] for k in ('source', 'target', 'label', 'evidence')})
    return result


def continuity_check(body, player, history=""):
    times = r"(?:[零一二三四五六七八九十]{1,3}点[零一二三四五六七八九十]{1,3}分|[0-2]?[0-9]:[0-5][0-9])"
    known = set(re.findall(times, history)) | {"二十二点五十五分", "22:55"}
    if set(re.findall(times, body)) - known:
        raise ValueError("不要补造具体分钟数；按本路线已发生的先后记事，保留已知时刻即可")
    if re.search(r'(?<!门)禁记录', body):
        raise ValueError('“禁记录”缺字，正确名称是“门禁记录”')
    # Bind direct address and past action to the right character, not the narrator
    # alone: NPC dialogue can also use “你”. The semantic review covers other forms.
    narration = re.sub(r'“[^”]*”', '', body)
    if player == '陈砚' and re.search(r'你下午(?:追|翻|查)[^。！？\n]{0,8}(?:维修|旧)?档案', narration):
        raise ValueError('下午追查档案的是唐栖，不能把这段调查经历写成陈砚的行为')
    for match in re.finditer(r'“([^”]+)”', body):
        before, after = body[max(0, match.start()-70):match.start()], body[match.end():match.end()+14]
        if (player == '陈砚' and re.search(r'你下午(?:追|翻|查)[^。！？\n]{0,8}(?:维修|旧)?档案', match[1])
                and re.search(r'(?:看着|问|质问)(?:你|陈砚)[^。！？“”]{0,5}[：:]$', before)):
            raise ValueError('下午追查档案的是唐栖，不能把这段调查经历写成陈砚的行为')
        speaker = re.search(r'(许川|陈砚|姜序|唐栖|你)(?:说|问|回答|承认|解释|补充)', after)
        if not speaker:
            candidates = list(re.finditer(r'(许川|陈砚|姜序|唐栖|你)[^。！？“”\n]{0,25}(?:说|回答|承认|解释)[：:，,]\s*$', before))
            speaker = candidates[-1] if candidates else None
        if not speaker:
            continue
        name = player if speaker[1] == '你' else speaker[1]
        quote = match[1]
        if name != '陈砚' and re.search(r'门禁记录(?:[，,]|是)?我改|我(?:改过|改了|修改)[^。！？]{0,6}门禁记录|防火门[^。！？]{0,15}(?<!不)是我做的', quote):
            raise ValueError('门禁记录和防火门手动设置由陈砚修改，不能把这段操作或自白移给' + name)
        if name != '唐栖' and re.search(r'我下午[^。！？]{0,16}(?:查过|查了|追查|翻过)[^。！？]{0,5}档案', quote):
            raise ValueError('下午调查档案的经历属于唐栖，不得移给' + name)


def reading_history(nodes):
    """Return structured narrative memory without copying any old prose.

    The latest two turns retain enough action/result detail for a handoff;
    older turns are compressed to their committed summaries.  Evidence quotes
    are deliberately excluded: they belong to validation records, never to a
    later prose prompt.
    """
    nodes = list(nodes or [])
    lines = []
    split = max(0, len(nodes) - 2)
    for index, node in enumerate(nodes):
        notes = node.get('readerOutcome') or {}
        action = notes.get('action') or {}
        title = node.get('playerDirection') or (node.get('selectedDirection') or {}).get('title', '')
        summary = action.get('summary') if isinstance(action, dict) else ''
        node_summary = node.get('summary') or ''
        clues = [c.get('summary') for c in (notes.get('clues') or [])
                 if isinstance(c, dict) and isinstance(c.get('summary'), str)]
        parts = [value.strip() for value in (title, node_summary, summary) if isinstance(value, str) and value.strip()]
        if clues:
            parts.append('线索：' + '；'.join(clues[:3]))
        if not parts:
            continue
        text = '；'.join(parts)
        if index < split:
            text = text[:220]
            lines.append('较早回合摘要：' + text)
        else:
            lines.append('最近回合摘要：' + text[:700])
    return '\n'.join(lines)[-5000:]


def action_requirements(text):
    """Preserve every clause, including prohibitions; never drop a compound goal."""
    clauses = [s.strip() for s in re.split(r'[，,；;。\n]+', text) if s.strip()]
    # Bound review size without discarding any request text.
    if len(clauses) > 12:
        clauses = clauses[:11] + ['；'.join(clauses[11:])]
    return {f'A{i+1}': value for i, value in enumerate(clauses)}


def validate_action_requirements(data, requirements, body):
    if not isinstance(data, dict) or not isinstance(data.get('issues'), list):
        raise ValueError('行动核对缺少明确的问题列表')
    if data['issues']:
        raise ValueError('行动或连续性未闭合：' + '；'.join(str(i) for i in data['issues'][:3]))
    actions = data.get('actions')
    if not isinstance(actions, list) or len(actions) != len(requirements):
        raise ValueError('复合行动必须逐项核对，不能遗漏用户要求')
    by_id = {a.get('id'): a for a in actions if isinstance(a, dict)}
    if set(by_id) != set(requirements):
        raise ValueError('行动核对的条目与本回合请求不一致')
    checked = []
    for key, requirement in requirements.items():
        item = by_id[key]
        if item.get('status') not in ('performed', 'blocked'):
            raise ValueError('行动没有实际执行或具体阻碍：' + requirement)
        paragraphs = {f'P{i+1}': p for i, p in enumerate(body.split('\n\n'))}
        evidence = paragraphs.get(item['paragraphId']) if 'paragraphId' in item else item.get('evidence')
        if not isinstance(evidence, str) or len(evidence) < 4 or evidence not in body:
            raise ValueError('行动结果必须逐字引用当前正文：' + requirement)
        summary = item.get('summary')
        if not isinstance(summary, str) or not 0 < len(summary) <= 160:
            raise ValueError('行动核对缺少实际后果：' + requirement)
        checked.append(dict(id=key, requirement=requirement, status=item['status'], summary=summary, evidence=evidence))
    return dict(actions=checked, action=dict(status='blocked' if any(a['status'] == 'blocked' for a in checked) else 'performed',
                summary='；'.join(a['summary'] for a in checked)[:160], evidence=checked[0]['evidence']), clues=[], relationships=[])
