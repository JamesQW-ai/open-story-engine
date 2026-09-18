"""Read local image receipts without exposing private prose or prompts."""
import argparse
import json
import math
from pathlib import Path


def nonnegative_int(value):
    return type(value) is int and value >= 0


def usage_group(items):
    calls = measured = tokens = unknown = 0
    for job in items:
        count = job.get('provider_calls')
        usage = job.get('usage')
        receipt = usage.get('total_tokens') if isinstance(usage, dict) else None
        if not nonnegative_int(count):
            unknown += 1
            continue
        calls += count
        if count == 0:
            # A positive or malformed receipt contradicts a zero-call record.
            if usage is not None and not (nonnegative_int(receipt) and receipt == 0):
                unknown += 1
        elif nonnegative_int(receipt):
            tokens += receipt
            # This schema retains one response receipt, not an aggregate of
            # multiple requests. Earlier calls without receipts stay unknown.
            measured += 1
    return dict(attempts=len(items), provider_calls=calls, usage_reported_calls=measured,
                reported_tokens=tokens, unmeasured_calls=calls-measured,
                unmeasured_attempts=unknown,
                total_tokens=tokens if calls == measured and not unknown else None,
                billed_amount=None)


def display_ms_summary(values):
    """Return stable latency aggregates for valid display receipts."""
    if not values:
        return dict(count=0, average_ms=None, p50_ms=None, p95_ms=None)
    ordered = sorted(values)

    def percentile(fraction):
        # Nearest-rank keeps the result deterministic for small samples and
        # never invents a latency between two observed receipts.
        index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    return dict(count=len(ordered), average_ms=sum(ordered) / len(ordered),
                p50_ms=percentile(0.50), p95_ms=percentile(0.95))


def report(directory):
    jobs, visits, displays = [], [], []
    unreadable = invalid_visits = invalid_displays = 0
    for path in Path(directory).glob('*.json'):
        record = None
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if not isinstance(record, dict):
            unreadable += 1
            record = {}
        if path.name.startswith('visit-'):
            if type(record.get('published_hit')) is bool:
                visits.append(record)
            else:
                invalid_visits += 1
        elif path.name.startswith('views-'):
            latency = record.get('display_ms')
            if nonnegative_int(latency) or (type(latency) is float and math.isfinite(latency) and latency >= 0):
                displays.append(record)
            else:
                invalid_displays += 1
        else:
            # Keep unreadable/missing-call job records as unknown attempts.
            # Silently skipping them would make a partial total look complete.
            jobs.append(record)
    total = usage_group(jobs)
    display_samples = sorted(r['display_ms'] for r in displays)
    private_visits = [r for r in visits if r['published_hit'] is False]
    private_cache_hits = sum(type(r.get('private_cache_hit')) is bool and r['private_cache_hit']
                             for r in private_visits)
    private_cache_misses = sum(type(r.get('private_cache_hit')) is bool and not r['private_cache_hit']
                               for r in private_visits)
    private_cache_unknown = len(private_visits) - private_cache_hits - private_cache_misses
    private_cache_known = private_cache_hits + private_cache_misses
    return dict(**total, visits=len(visits), published_hits=sum(r['published_hit'] for r in visits),
                published_hit_rate=sum(r['published_hit'] for r in visits) / len(visits) if visits else None,
                private_visits=len(private_visits), private_cache_hits=private_cache_hits,
                private_cache_misses=private_cache_misses, private_cache_unknown=private_cache_unknown,
                private_cache_hit_rate=(private_cache_hits / private_cache_known
                                        if private_cache_known else None),
                displayed=len(displays), display_ms_samples=display_samples,
                display_ms_summary=display_ms_summary(display_samples),
                unreadable_records=unreadable, invalid_visit_records=invalid_visits,
                invalid_display_records=invalid_displays,
                queued_cancelled=sum(j.get('cancel_stage') == 'queued' for j in jobs),
                inflight_cancelled=sum(j.get('cancel_stage') == 'generating' for j in jobs),
                completed_not_displayed=sum(j.get('completed') is True and j.get('displayed') is not True for j in jobs),
                display_confirmed=usage_group([j for j in jobs if j.get('displayed') is True]),
                display_unconfirmed=usage_group([j for j in jobs if j.get('displayed') is not True]),
                cost_note='Use provider invoices for currency charges; no inferred price or refunds.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read illustration cache and cancellation metrics')
    parser.add_argument('directory', nargs='?', default='data/illustrations')
    args = parser.parse_args()
    print(json.dumps(report(args.directory), ensure_ascii=False, indent=2, allow_nan=False))
