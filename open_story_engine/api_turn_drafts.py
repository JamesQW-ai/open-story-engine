"""Private, bounded turn preparation. Only selection commits to the story store."""
from __future__ import annotations

import copy
from contextlib import closing, contextmanager
import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .api_read import ReadError
from .storage import prepared_branch
from .prompts import catalog_version

RULES_VERSION = 'prepared-player-turn/19+' + catalog_version()
TERMINAL = {'ready', 'failed', 'expired'}
LEASE_SECONDS = 45
TTL_SECONDS = 600
MAX_JOBS = 128
MAX_RELEASED_SUBSCRIPTIONS = 4096


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reported_usage(completion):
    """Use provider receipts, never estimate billed tokens from prose length."""
    usages = []
    raw = getattr(completion, 'raw_response', None)
    try:
        objects = [json.loads(raw)]
    except (ValueError, TypeError):
        objects = []
        for line in (raw if isinstance(raw, str) else '').splitlines():
            try:
                objects.append(json.loads(line))
            except ValueError:
                pass
    for value in objects:
        usage = value.get('usage') if isinstance(value, dict) else None
        if isinstance(usage, dict) and nonnegative_int(usage.get('total_tokens')):
            usages.append(usage)
    # Streaming receipts are cumulative per response; count the last one once.
    usage = usages[-1] if usages else None
    incomplete = usage is None or any(o.get('outcome') == 'failed' for o in getattr(completion, 'observations', []))
    return (usage.get('total_tokens', 0) if usage else 0), incomplete


def nonnegative_int(value):
    return type(value) is int and value >= 0


def cumulative_metrics(job):
    """Known receipts are lower bounds; missing old measurements stay unknown."""
    attempts = [a.get('metrics', {}) for a in job.get('previous_attempts', [])] + [job['metrics']]
    fields = ('calls', 'reported_tokens', 'unreported_calls')
    measured = [m for m in attempts if all(nonnegative_int(m.get(k)) for k in fields)
                and m['unreported_calls'] <= m['calls']]
    result = {k: sum(m[k] for m in measured) for k in fields}
    result.update(attempts=len(attempts), unmeasured_attempts=len(attempts) - len(measured))
    result['tokens'] = result['reported_tokens'] if not result['unmeasured_attempts'] and not result['unreported_calls'] else None
    for key in ('complete_ms', 'queue_ms'):
        result[key] = sum(m[key] for m in attempts) if all(nonnegative_int(m.get(key)) for m in attempts) else None
    return result


def visible_choices(parent, package, contract=None, history=None):
    """One authoritative menu for UI, preparation and click validation."""
    actions = parent.get('openingActions', [])
    if parent['kind'] == 'source_entry' and actions:
        return [dict(id=f'opening-{i}', title=a['title'], summary=a['summary'],
                     payload={'text': a['title'] + '。' + a['summary']}) for i, a in enumerate(actions)]
    if 'readerChoices' in parent:
        from .reader_choices import choice_context, restored_choices
        if contract is None or not history:
            return []
        context = choice_context(package, contract, history, parent)
        return [dict(id=c['id'], title=c['title'], summary=c['summary'], payload={'text': c['summary']})
                for c in restored_choices(parent['readerChoices'], context, package)]
    from .reader_consequences import filter_directions
    choices = []
    for d in filter_directions(parent['nextDirections'], package, parent.get('branchState', {})):
        if d['title'] in ('继续当前目标', '继续故事', '继续推进'):
            continue
        if any(c['title'] == d['title'] for c in choices):
            continue
        title = d['title']
        if title.startswith(('推进：', '推进:')) and d.get('summary'):
            title = '继续：' + d['summary'].split('。')[0].split('；')[0][:28]
        choices.append(dict(id=d['id'], title=title, summary=d.get('summary') or '沿着这一方向行动，看看眼前的局面如何变化。', payload={'direction_id': d['id']}))
    return choices


class TurnSnapshot:
    """The core sees frozen reads and deferred writes, never a writable live DB."""
    def __init__(self, package, session, contract, lineage, derived, closing_intent=None):
        self.package = package
        self.session = session
        self.story_contract = contract
        self.history = lineage
        self.derived_package = derived
        self.frozen_closing_intent = copy.deepcopy(closing_intent)
        self.audits = []
        self.evaluation = None
        self.node = None
        self.derived_update = None
        self.on_validating = lambda: None
        self.usage = dict(reported_tokens=0, calls=0, unreported_calls=0)

    def assert_session_package(self, sid, package):
        if sid != self.session['id'] or (package['id'], package['version']) != (self.session['storyPackageId'], self.session['storyPackageVersion']):
            raise ValueError('草稿故事包绑定不一致')

    def contract(self, sid):
        return copy.deepcopy(self.story_contract)

    def branch(self, sid, bid):
        return copy.deepcopy(next(b for b in self.history if b['id'] == bid and b['sessionId'] == sid))

    def lineage(self, sid, bid):
        return copy.deepcopy(self.history)

    def closing_intent(self, sid, bid):
        if sid != self.session['id'] or bid != self.history[-1]['id']:
            raise ValueError('收束意图与冻结分支不一致')
        return copy.deepcopy(self.frozen_closing_intent)

    def find_branch_request(self, *args):
        return None

    def find_direction_request(self, *args):
        return None

    def save_audit(self, sid, audit, parent_id=None):
        self.audits.append([copy.deepcopy(audit), parent_id])

    def save_direction_evaluation(self, sid, parent_id, text, evaluation, request_id=None):
        self.evaluation = dict(parent_id=parent_id, text=text, evaluation=copy.deepcopy(evaluation))

    def append_branch(self, sid, parent_id, node):
        self.on_validating()
        self.node = prepared_branch(self.branch(sid, parent_id), parent_id, node)
        return dict(self.node, sessionId=sid, sequence=-1)

    def derived(self, sid):
        return copy.deepcopy(self.derived_package)

    def update_derived(self, package):
        self.derived_update = copy.deepcopy(package)

    def artifact(self, outcome):
        return dict(outcome=outcome, node=self.node, evaluation=self.evaluation,
                    audits=self.audits, derived=self.derived_update, usage=self.usage)


class TurnDrafts:
    """Two speculative slots plus one reserved foreground slot; no network lock.

    SQLite stores only drafts, not story branches. Subscriptions expire after a
    missed heartbeat. Cancellation closes streaming calls at the next callback;
    requests already accepted by a provider may still consume tokens.
    """
    def __init__(self, path: Path, generate):
        self.path = path
        self.generate = generate
        self.condition = threading.Condition(threading.RLock())
        self.jobs = {}
        self.workers = [0, 0]  # background, foreground
        self.closed = False
        self.initialized = False
        self.released_subscriptions = {}

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS turn_drafts (key TEXT PRIMARY KEY, updated REAL NOT NULL, body TEXT NOT NULL)')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _initialize(self):
        if self.initialized:
            return
        with self._db() as db:
            db.execute('DELETE FROM turn_drafts WHERE updated < ?', (time.time() - TTL_SECONDS,))
            for key, _, body in db.execute('SELECT * FROM turn_drafts ORDER BY updated DESC LIMIT ?', (MAX_JOBS,)):
                job = json.loads(body)
                if job['status'] not in TERMINAL:
                    job['status'] = 'expired'
                job.update(subscribers={}, selected=False, selected_waiters=0, text='', revision=0, events=[])
                self.jobs[key] = job
        self.initialized = True

    def _save(self, job):
        if job.get('discarded') or self.jobs.get(job['key']) is not job:
            return
        body = {k: v for k, v in job.items() if k not in ('snapshot', 'subscribers', 'text', 'selected', 'selected_waiters', 'revision', 'events')}
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO turn_drafts VALUES (?,?,?)', (job['key'], time.time(), json.dumps(body, ensure_ascii=False)))

    def _expire(self, job):
        if job['status'] == 'queued':
            # The scheduler never entered generation: zero calls is known.
            job['metrics'].update(calls=0, reported_tokens=0, unreported_calls=0,
                                  tokens=0, complete_ms=0,
                                  queue_ms=max(0, round((time.time() - job['created']) * 1000)))
            job.pop('snapshot', None)
        job['status'] = 'expired'

    def _sweep(self, reserve=0):
        now = time.time()
        for job in self.jobs.values():
            job['subscribers'] = {s: t for s, t in job['subscribers'].items() if t > now}
            if job['status'] != 'expired' and not job['selected'] and ((job['status'] == 'queued' and not job['subscribers']) or (not job['subscribers'] and now - job['created'] > TTL_SECONDS)):
                self._expire(job)
                job.pop('artifact', None)
                job.pop('snapshot', None)
                self._save(job)
        # Bounded retention, including the durable copy.
        while len(self.jobs) > MAX_JOBS - reserve:
            old = next((k for k, j in self.jobs.items() if j['status'] in TERMINAL and not j['selected'] and not j['subscribers']), None)
            if old is None:
                raise ReadError(503, 'draft_capacity', '正在准备的故事较多，请稍后再试。')
            del self.jobs[old]
            with self._db() as db:
                db.execute('DELETE FROM turn_drafts WHERE key=?', (old,))

    def ensure(self, binding, payload, snapshot, subscriber=None, foreground=False, retry=False):
        with self.condition:
            if self.closed:
                raise ReadError(503, 'draft_unavailable', '故事准备服务已关闭，请稍后重试。')
            self._prune_releases()
            if subscriber and (binding['session_id'], binding['parent_branch_id'], subscriber) in self.released_subscriptions:
                raise ReadError(409, 'subscription_released', '此页面订阅已结束，请重新打开阅读页。')
            self._initialize()
            self._sweep()
            if binding.get('request_id'):
                for existing in self.jobs.values():
                    prior = existing['binding']
                    if prior.get('session_id') == binding['session_id'] and prior.get('request_id') == binding['request_id'] and prior != binding:
                        raise ReadError(409, 'request_conflict', '同一请求不能用于不同的行动')
            key = digest(binding)
            job = self.jobs.get(key)
            if job is None or job['status'] == 'expired' or (retry and job['status'] == 'failed'):
                if job is None:
                    self._sweep(reserve=1)
                # Retrying a failed shared draft replaces the attempt, not its
                # live readers. _sweep already removed expired leases; retain
                # their original deadlines so a retry is not a heartbeat.
                subscribers = dict(job['subscribers']) if job and job['status'] == 'failed' else {}
                previous_attempts = copy.deepcopy(job.get('previous_attempts', [])) if job else []
                if job:
                    previous_attempts.append(dict(created=job['created'], status=job['status'], attempt_id=job.get('attempt_id'),
                        metrics=copy.deepcopy(job['metrics']),
                        error=copy.deepcopy(job.get('error', {})),
                        failure_audits=copy.deepcopy(job.get('failure_audits', []))))
                job = dict(key=key, attempt_id=uuid.uuid4().hex, binding=binding, payload=payload, status='queued', created=time.time(),
                           subscribers=subscribers, selected=False, selected_waiters=0, text='', revision=0, events=[], metrics={'first_text_ms': None}, snapshot=snapshot,
                           previous_attempts=previous_attempts)
                self.jobs[key] = job
                self._save(job)
            if not job.get('attempt_id'):
                job['attempt_id'] = uuid.uuid4().hex
                self._save(job)
            if subscriber:
                job['subscribers'][subscriber] = time.time() + LEASE_SECONDS
            if foreground:
                job['selected_waiters'] += 1
                job['selected'] = True
                job['metrics']['ready_hit'] = job['status'] == 'ready'
                job['metrics']['selection_count'] = job['metrics'].get('selection_count', 0) + 1
                job['metrics']['ready_hits'] = job['metrics'].get('ready_hits', 0) + int(job['status'] == 'ready')
                self._save(job)
            if not self.closed:
                for lane, limit in ((0, 2), (1, 1)):
                    if lane == 1 and not foreground:
                        continue
                    while self.workers[lane] < limit:
                        self.workers[lane] += 1
                        threading.Thread(target=self._worker, args=(lane,), daemon=True).start()
            self.condition.notify_all()
            return job

    def _worker(self, lane):
        try:
            while True:
                with self.condition:
                    if self.closed:
                        return
                    self._sweep()
                    candidates = [j for j in self.jobs.values() if j['status'] == 'queued' and (j['selected'] or (lane == 0 and j['subscribers']))]
                    if not candidates:
                        return
                    job = min(candidates, key=lambda j: (not j['selected'], j['created']))
                    job['status'] = 'generating'
                    job['metrics']['queue_ms'] = round((time.time() - job['created']) * 1000)
                    self._save(job)
                started = time.monotonic()
                def check():
                    # No subscriber: stop before another call/delta; never commit.
                    with self.condition:
                        live = any(t > time.time() for t in job['subscribers'].values())
                        if self.closed or job['status'] == 'expired' or (not job['selected'] and not live):
                            raise ReadError(409, 'draft_cancelled', '已离开这个方向')
                def delta(text):
                    with self.condition:
                        check()
                        if job['metrics']['first_text_ms'] is None:
                            job['metrics']['first_text_ms'] = round((time.monotonic() - started) * 1000)
                        job['status'] = 'generating'
                        job['text'] += text
                        job['events'].append(('delta', text))
                        self.condition.notify_all()
                def reset(_reason):
                    with self.condition:
                        check()
                        job['text'] = ''
                        job['revision'] += 1
                        job['events'].append(('reset', ''))
                        self.condition.notify_all()
                def validating():
                    with self.condition:
                        check()
                        job['status'] = 'validating'
                        self.condition.notify_all()
                artifact = None
                try:
                    check()
                    artifact = self.generate(job['snapshot'], job['payload'], delta, reset, validating, check)
                    check()
                    with self.condition:
                        check()
                        job['artifact'] = artifact
                        job['status'] = 'ready'
                        self._finish(job, started)
                except Exception as error:
                    with self.condition:
                        job['status'] = 'expired' if job['status'] == 'expired' or isinstance(error, ReadError) and error.code == 'draft_cancelled' else 'failed'
                        job['usage'] = getattr(error, 'draft_usage', (artifact or {}).get('usage', {}))
                        job['failure_audits'] = getattr(error, 'draft_audits', (artifact or {}).get('audits', []))
                        retained = getattr(error, 'retained_body', '') or (
                            (artifact or {}).get('audits', [{}])[-1].get('retainedDraft', {}).get('text', '')
                            if (artifact or {}).get('audits') else ''
                        )
                        if job['status'] == 'failed' and retained:
                            job['retained_draft'] = {'text': retained, 'status': 'unconfirmed'}
                            # Repair resets may have cleared the last complete
                            # draft. Restore it before the terminal error event.
                            job['text'] = retained
                            job['events'].extend([('reset', ''), ('delta', retained)])
                        job['error'] = {'code': error.code if isinstance(error, ReadError) else ('play_rejected' if isinstance(error, ValueError) else 'generation_failed'),
                                        'message': '这次续写未能完成，请重试或调整行动。',
                                        'status': error.status if isinstance(error, ReadError) else (409 if isinstance(error, ValueError) else 503)}
                        logging.getLogger(__name__).warning('Prepared turn failed: %s', type(error).__name__)
                        self._finish(job, started)
        finally:
            with self.condition:
                self.workers[lane] -= 1
                if not self.closed and any(j['status'] == 'queued' and (j['selected'] or (lane == 0 and j['subscribers'])) for j in self.jobs.values()):
                    self.workers[lane] += 1
                    threading.Thread(target=self._worker, args=(lane,), daemon=True).start()

    def _finish(self, job, started):
        # Publish terminal state, receipts and durable data under the same lock.
        job.pop('snapshot', None)
        job['metrics']['complete_ms'] = round((time.monotonic() - started) * 1000)
        usage = job.get('artifact', {}).get('usage', job.get('usage', {}))
        job['metrics'].update(usage)
        job['metrics']['tokens'] = cumulative_metrics({'metrics': usage})['tokens']
        current = self.jobs.get(job['key'])
        if current is not None and current is not job:
            # A cancelled provider may return after its replacement started.
            # Complete that historical attempt, never overwrite the new job.
            for attempt in current.get('previous_attempts', []):
                if attempt['created'] == job['created']:
                    attempt['metrics'] = copy.deepcopy(job['metrics'])
                    attempt['failure_audits'] = copy.deepcopy(job.get('failure_audits', []))
                    attempt['error'] = copy.deepcopy(job.get('error', {}))
                    self._save(current)
                    break
        self._save(job)
        self.condition.notify_all()

    def wait(self, job, stream=None, reset=None):
        offset = 0
        while True:
            with self.condition:
                events = job['events'][offset:]
                offset += len(events)
                status = job['status']
                if status not in TERMINAL and not events:
                    self.condition.wait(timeout=0.25)
            for event, text in events:
                if event == 'reset' and reset:
                    reset('draft_repair')
                elif event == 'delta' and stream:
                    stream(text)
            if status == 'ready':
                return job['artifact']
            if status in ('failed', 'expired'):
                if status == 'failed' and not events and offset == 0 and stream and reset:
                    retained = job.get('retained_draft', {}).get('text')
                    if retained:
                        reset('unconfirmed_draft')
                        stream(retained)
                raise ReadError(job.get('error', {}).get('status', 503), job.get('error', {}).get('code', 'generation_failed'), '这次续写未能完成，请重试或调整行动。')

    def release_selection(self, job):
        with self.condition:
            job['selected_waiters'] = max(0, job['selected_waiters'] - 1)
            job['selected'] = job['selected_waiters'] > 0
            self._save(job)
            self.condition.notify_all()

    def _prune_releases(self):
        now = time.monotonic()
        self.released_subscriptions = {key: expiry for key, expiry in self.released_subscriptions.items() if expiry > now}

    def release(self, sid, parent_id, subscriber, *, retire=False):
        with self.condition:
            if retire:
                self._prune_releases()
                key = (sid, parent_id, subscriber)
                if key not in self.released_subscriptions and len(self.released_subscriptions) >= MAX_RELEASED_SUBSCRIPTIONS:
                    raise ReadError(503, 'draft_capacity', '页面订阅较多，请稍后再试。')
                # Remember release even before the delayed prepare has arrived.
                # Internal foreground handoff is not a page retirement.
                self.released_subscriptions[key] = time.monotonic() + TTL_SECONDS
            for job in self.jobs.values():
                if job['binding']['session_id'] == sid and job['binding']['parent_branch_id'] == parent_id:
                    job['subscribers'].pop(subscriber, None)
            self._sweep()
            self.condition.notify_all()

    def view(self, job):
        with self.condition:
            metrics = copy.deepcopy(job['metrics'])
            metrics['cumulative'] = cumulative_metrics(job)
            return dict(draft_id=job['key'], status=job['status'], metrics=metrics)

    def record_display(self, sid, branch_id, parent_id, key, attempt_id=None):
        with self.condition:
            self._initialize()
            job = self.jobs.get(key)
            if (not job or job['binding'].get('session_id') != sid
                    or job['binding'].get('parent_branch_id') != parent_id):
                return dict(recorded=False, reason='measurement_unavailable')
            attempt = next((a for a in [job] + job.get('previous_attempts', [])
                if a.get('metrics', {}).get('committed_branch_id') in (None, branch_id)
                and ((attempt_id and a.get('attempt_id') == attempt_id)
                    or (not attempt_id and a.get('metrics', {}).get('committed_branch_id') == branch_id))), None)
            if attempt is None:
                return dict(recorded=False, reason='measurement_unavailable')
            metrics = attempt['metrics']
            # The saved branch proves the commit even after a crash before metrics saved.
            metrics.update(committed=True, committed_branch_id=branch_id)
            first = metrics.get('first_display_receipt_at')
            if first is None:
                metrics['first_display_receipt_at'] = time.time()
                self._save(job)
            return dict(recorded=True, deduplicated=first is not None)

    def usage_summary(self, sid):
        # This diagnostic read must not initialize/migrate either database.
        with self.condition:
            if self.initialized:
                jobs = list(self.jobs.values())
            elif self.path.is_file():
                with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                    db.execute('PRAGMA query_only=ON')
                    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_drafts'").fetchone():
                        jobs = []
                    else:
                        jobs = [json.loads(row[0]) for row in db.execute(
                            'SELECT body FROM turn_drafts WHERE updated >= ? ORDER BY updated DESC LIMIT ?',
                            (time.time() - TTL_SECONDS, MAX_JOBS))]
            else:
                jobs = []
            groups = {'display_confirmed': [], 'display_unconfirmed': []}
            for job in jobs:
                if job['binding'].get('session_id') != sid:
                    continue
                for attempt in job.get('previous_attempts', []) + [job]:
                    metrics = attempt.get('metrics', {})
                    confirmed = metrics.get('first_display_receipt_at') is not None and metrics.get('committed_branch_id')
                    groups['display_confirmed' if confirmed else 'display_unconfirmed'].append(metrics)
            def total(items):
                if not items:
                    return dict(attempts=0, calls=0, reported_tokens=0, unreported_calls=0,
                                unmeasured_attempts=0, tokens=0, queue_ms=0, complete_ms=0)
                return cumulative_metrics(dict(metrics=items[-1],
                    previous_attempts=[{'metrics': m} for m in items[:-1]]))
            return dict(scope='retained_turn_draft_attempts',
                        **{name: total(items) for name, items in groups.items()})

    def discard_session(self, sid):
        with self.condition:
            self._initialize()
            for key, job in list(self.jobs.items()):
                if job['binding'].get('session_id') == sid:
                    job.update(status='expired', discarded=True, subscribers={})
                    self.jobs.pop(key)
                    with self._db() as db:
                        db.execute('DELETE FROM turn_drafts WHERE key=?', (key,))
            self.condition.notify_all()

    def discard_parent(self, sid, parent_id, *, closing_digest=None):
        with self.condition:
            self._initialize()
            for job in self.jobs.values():
                if job['binding'].get('session_id') == sid and job['binding'].get('parent_branch_id') == parent_id:
                    if closing_digest is not None and job['binding'].get('closing_digest') == closing_digest:
                        continue
                    self._expire(job)
                    job.update(subscribers={}, error={
                        'status': 409, 'code': 'draft_expired' if closing_digest is not None else 'route_ended',
                        'message': '收束意图已变化，请重新选择方向。' if closing_digest is not None else '这条路线已收尾。'})
                    job.pop('artifact', None)
                    job.pop('retained_draft', None)
                    self._save(job)
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            for job in self.jobs.values():
                if job['status'] not in TERMINAL:
                    self._expire(job)
                    self._save(job)
            self.condition.notify_all()
