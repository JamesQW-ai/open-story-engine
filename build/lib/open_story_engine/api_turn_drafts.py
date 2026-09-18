"""Private, bounded turn preparation. Only selection commits to the story store."""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

from .api_read import ReadError
from .storage import prepared_branch
from .prompts import catalog_version

RULES_VERSION = 'prepared-player-turn/11+' + catalog_version()
TERMINAL = {'ready', 'failed', 'expired'}
LEASE_SECONDS = 45
TTL_SECONDS = 600
MAX_JOBS = 128


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reported_usage(completion):
    """Use provider receipts, never estimate billed tokens from prose length."""
    usages = []
    try:
        objects = [json.loads(completion.raw_response)]
    except (ValueError, TypeError):
        objects = []
        for line in (completion.raw_response or '').splitlines():
            try:
                objects.append(json.loads(line))
            except ValueError:
                pass
    for value in objects:
        usage = value.get('usage') if isinstance(value, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get('total_tokens'), int):
            usages.append(usage)
    # Streaming receipts are cumulative per response; count the last one once.
    usage = usages[-1] if usages else None
    incomplete = usage is None or any(o.get('outcome') == 'failed' for o in completion.observations)
    return (usage.get('total_tokens', 0) if usage else 0), incomplete


def visible_choices(parent, package):
    """One authoritative menu for UI, preparation and click validation."""
    actions = parent.get('openingActions', [])
    if parent['kind'] == 'source_entry' and len(actions) >= 2:
        return [dict(id=f'opening-{i}', title=a['title'], summary=a['summary'],
                     payload={'text': a['title'] + '。' + a['summary']}) for i, a in enumerate(actions[:2])]
    choices = []
    for d in parent['nextDirections']:
        if d['title'] in ('继续当前目标', '继续故事', '继续推进'):
            continue
        if any(c['title'] == d['title'] for c in choices):
            continue
        title = d['title']
        if title.startswith(('推进：', '推进:')) and d.get('summary'):
            title = '继续：' + d['summary'].split('。')[0].split('；')[0][:28]
        choices.append(dict(id=d['id'], title=title, summary=d.get('summary') or '沿着这一方向行动，看看眼前的局面如何变化。', payload={'direction_id': d['id']}))
        if len(choices) == 2:
            return choices
    state = parent['branchState']
    place_id = state.get('playerLocationId') or state.get('currentLocationId')
    place = next((p['name'] for p in package['locations'] if p['id'] == place_id), '当前场景')
    extras = [
        (f'留在{place}，观察周围动静', '先看清出入口与在场人物的反应，留意刚才忽略的细节，再决定是否行动。'),
        ('重新梳理线索，确认下一步要追问的事', '回想刚才的对话和已经知道的线索，找出说法中的疑点，暂不离开当前位置。'),
    ]
    for i, (title, summary) in enumerate(extras):
        if len(choices) == 2:
            break
        if any(c['title'] == title for c in choices):
            continue
        choices.append(dict(id=f'free-observe-{i}', title=title, summary=summary, payload={'text': title + '。' + summary}))
    return choices


class TurnSnapshot:
    """The core sees frozen reads and deferred writes, never a writable live DB."""
    def __init__(self, package, session, contract, lineage, derived):
        self.package = package
        self.session = session
        self.story_contract = contract
        self.history = lineage
        self.derived_package = derived
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
                job.update(subscribers={}, selected=False, text='', revision=0, events=[])
                self.jobs[key] = job
        self.initialized = True

    def _save(self, job):
        if job.get('discarded'):
            return
        body = {k: v for k, v in job.items() if k not in ('snapshot', 'subscribers', 'text', 'selected', 'revision', 'events')}
        with self._db() as db:
            db.execute('INSERT OR REPLACE INTO turn_drafts VALUES (?,?,?)', (job['key'], time.time(), json.dumps(body, ensure_ascii=False)))

    def _sweep(self):
        now = time.time()
        for job in self.jobs.values():
            job['subscribers'] = {s: t for s, t in job['subscribers'].items() if t > now}
            if job['status'] != 'expired' and not job['selected'] and ((job['status'] == 'queued' and not job['subscribers']) or (not job['subscribers'] and now - job['created'] > TTL_SECONDS)):
                job['status'] = 'expired'
                job.pop('artifact', None)
                job.pop('snapshot', None)
                self._save(job)
        # Bounded retention, including the durable copy.
        while len(self.jobs) >= MAX_JOBS:
            old = next((k for k, j in self.jobs.items() if j['status'] in TERMINAL and not j['selected'] and not j['subscribers']), None)
            if old is None:
                raise ReadError(503, 'draft_capacity', '正在准备的故事较多，请稍后再试。')
            del self.jobs[old]
            with self._db() as db:
                db.execute('DELETE FROM turn_drafts WHERE key=?', (old,))

    def ensure(self, binding, payload, snapshot, subscriber=None, foreground=False, retry=False):
        with self.condition:
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
                job = dict(key=key, binding=binding, payload=payload, status='queued', created=time.time(),
                           subscribers={}, selected=False, text='', revision=0, events=[], metrics={'first_text_ms': None}, snapshot=snapshot)
                self.jobs[key] = job
                self._save(job)
            if subscriber:
                job['subscribers'][subscriber] = time.time() + LEASE_SECONDS
            if foreground:
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
                    check()
                    with self.condition:
                        if job['metrics']['first_text_ms'] is None:
                            job['metrics']['first_text_ms'] = round((time.monotonic() - started) * 1000)
                        job['status'] = 'generating'
                        job['text'] += text
                        job['events'].append(('delta', text))
                        self.condition.notify_all()
                def reset(_reason):
                    check()
                    with self.condition:
                        job['text'] = ''
                        job['revision'] += 1
                        job['events'].append(('reset', ''))
                        self.condition.notify_all()
                def validating():
                    check()
                    with self.condition:
                        job['status'] = 'validating'
                        self.condition.notify_all()
                try:
                    check()
                    artifact = self.generate(job['snapshot'], job['payload'], delta, reset, validating, check)
                    check()
                    with self.condition:
                        job['artifact'] = artifact
                        job['status'] = 'ready'
                except Exception as error:
                    with self.condition:
                        job['status'] = 'expired' if isinstance(error, ReadError) and error.code == 'draft_cancelled' else 'failed'
                        job['usage'] = getattr(error, 'draft_usage', {})
                        job['failure_audits'] = getattr(error, 'draft_audits', [])
                        job['error'] = {'code': error.code if isinstance(error, ReadError) else ('play_rejected' if isinstance(error, ValueError) else 'generation_failed'),
                                        'message': '这次续写未能完成，请重试或调整行动。',
                                        'status': error.status if isinstance(error, ReadError) else (409 if isinstance(error, ValueError) else 503)}
                        logging.getLogger(__name__).warning('Prepared turn failed: %s', type(error).__name__)
                finally:
                    with self.condition:
                        job.pop('snapshot', None)
                        job['metrics']['complete_ms'] = round((time.monotonic() - started) * 1000)
                        usage = job.get('artifact', {}).get('usage', job.get('usage', {}))
                        job['metrics'].update(usage)
                        job['metrics']['tokens'] = usage.get('reported_tokens') if usage.get('calls') and not usage.get('unreported_calls') else None
                        self._save(job)
                        self.condition.notify_all()
        finally:
            with self.condition:
                self.workers[lane] -= 1
                if not self.closed and any(j['status'] == 'queued' and (j['selected'] or (lane == 0 and j['subscribers'])) for j in self.jobs.values()):
                    self.workers[lane] += 1
                    threading.Thread(target=self._worker, args=(lane,), daemon=True).start()

    def wait(self, job, stream=None, reset=None):
        with self.condition:
            if job['status'] == 'ready' and job['metrics'].get('ready_hit'):
                return job['artifact']
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
                raise ReadError(job.get('error', {}).get('status', 503), job.get('error', {}).get('code', 'generation_failed'), '这次续写未能完成，请重试或调整行动。')

    def release(self, sid, parent_id, subscriber):
        with self.condition:
            for job in self.jobs.values():
                if job['binding']['session_id'] == sid and job['binding']['parent_branch_id'] == parent_id:
                    job['subscribers'].pop(subscriber, None)
            self._sweep()
            self.condition.notify_all()

    def view(self, job):
        with self.condition:
            return dict(draft_id=job['key'], status=job['status'], metrics=copy.deepcopy(job['metrics']))

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

    def close(self):
        with self.condition:
            self.closed = True
            for job in self.jobs.values():
                if job['status'] not in TERMINAL:
                    job['status'] = 'expired'
                    self._save(job)
            self.condition.notify_all()
