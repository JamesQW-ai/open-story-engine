"""Branch-local public questions, distinct from historical clues and player goals."""
import copy
import hashlib
import re
from . import item_lifecycle

STATE_KEY = 'threadLedger'
PRIORITIES = ('critical', 'high', 'normal', 'low', 'unknown')
RECOVERY_WINDOWS = ('immediate', 'near', 'mid', 'late', 'unknown')


def metadata(item, prior=None):
    """Return explicit scheduling metadata without inferring it from status."""
    prior = prior or {}
    return {
        'priority': item.get('priority', prior.get('priority', 'unknown')),
        'recoveryWindow': item.get('recoveryWindow', prior.get('recoveryWindow', 'unknown')),
    }


def validate_metadata(item):
    values = metadata(item)
    if values['priority'] not in PRIORITIES:
        raise ValueError('剧情问题优先级无效')
    if values['recoveryWindow'] not in RECOVERY_WINDOWS:
        raise ValueError('剧情问题回收窗口无效')
    return values


def initial_threads(package, contract, status='open'):
    entry = next((e for e in package['story'].get('entryModel', {}).get('entryPoints', [])
                  if e['id'] == contract.get('entryPointId')), {})
    return [dict(id='opening-thread-' + str(i + 1), title=title, status=status,
                 source='opening', openedBranchId=None, causeBranchId=None,
                 reason='', evidence='', priority='unknown', recoveryWindow='unknown')
            for i, title in enumerate(entry.get('openingThreads', []))]


def threads_for(package, contract, state):
    # A legacy save lacks lifecycle evidence. Neither old menus nor goal status
    # establish that an opening question remains open or has been answered.
    if STATE_KEY in state:
        return copy.deepcopy(state[STATE_KEY])
    return initial_threads(package, contract, status='unknown')


def validate_updates(plan, context):
    from .reader_actions import registry
    updates = plan.get('threadUpdates', [])
    if not isinstance(updates, list) or len(updates) > 8:
        raise ValueError('剧情问题变化格式无效')
    current = {t['id']: t for t in threads_for(context['package'], context['contract'],
                                             context['parent']['branchState'])}
    steps = {s['id'] for s in plan['steps']}
    state = context['parent']['branchState']
    known = registry(context['package'], state, plan['introductions'])
    seen, titles = set(), {t['title'].strip() for t in current.values()}
    for item in updates:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in seen:
            raise ValueError('剧情问题引用无效或重复')
        tid = item['id']
        seen.add(tid)
        new = re.fullmatch(r'new-[1-8]', tid) is not None
        if not new and tid not in current:
            raise ValueError('剧情问题引用不存在')
        if new and context.get('_closing_phase'):
            raise ValueError('收束中不得建立新的长期剧情问题；只能处理现有账本条目')
        if item.get('status') not in ('open', 'resolved', 'abandoned'):
            raise ValueError('剧情问题状态无效；未确认项应继承 unknown，不得推断结论')
        values = validate_metadata(item)
        for key in ('title', 'reason'):
            if not isinstance(item.get(key), str) or not 0 < len(item[key].strip()) <= 180:
                raise ValueError('剧情问题缺少简短文字或改变原因')
        refs = item.get('stepIds')
        if not isinstance(refs, list) or not refs or any(not isinstance(s, str) or s not in steps for s in refs):
            raise ValueError('剧情问题变化必须引用本回合已授权步骤')
        item_lifecycle.validate_dependencies(item, current.get(tid), plan, known, state, item['status'] == 'open')
        if new:
            if item['status'] != 'open' or item['title'].strip() in titles:
                raise ValueError('新剧情问题必须待处理且不能重复旧问题')
            titles.add(item['title'].strip())
        else:
            old = current[tid]
            if item['title'] != old['title']:
                raise ValueError('不得改写旧剧情问题文字')
            if old['status'] in ('resolved', 'abandoned'):
                raise ValueError('已结束剧情问题不能重置或重复登记')
            old_values = metadata(old)
            if (old['status'] == item['status']
                    and item_lifecycle.dependencies(item, old) == item_lifecycle.dependencies(old)
                    and values == old_values):
                raise ValueError('剧情问题状态未变，不应重复登记')


def commit(package, contract, state, updates, branch_id):
    threads = threads_for(package, contract, state)
    for i, item in enumerate(updates):
        prior = next((t for t in threads if t['id'] == item['id']), None)
        record = {k: copy.deepcopy(item[k]) for k in ('title', 'status', 'reason', 'evidence')}
        record.update(metadata(item, prior))
        if 'itemDependencies' in item or prior and 'itemDependencies' in prior:
            record['itemDependencies'] = copy.deepcopy(item_lifecycle.dependencies(item, prior))
        record['causeBranchId'] = branch_id
        if item['id'].startswith('new-'):
            tid = 'thread-' + hashlib.sha256((branch_id + ':' + str(i)).encode()).hexdigest()[:16]
            threads.append(dict(record, id=tid, source='player_branch', openedBranchId=branch_id))
        else:
            next(t for t in threads if t['id'] == item['id']).update(record)
    return threads
