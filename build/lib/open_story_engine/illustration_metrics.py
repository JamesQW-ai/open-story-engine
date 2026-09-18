"""Read local image receipts without exposing private prose or prompts."""
import argparse
import json
from pathlib import Path


def report(directory):
    records = []
    for path in Path(directory).glob('*.json'):
        try:
            records.append((path.name, json.loads(path.read_text())))
        except (OSError, ValueError):
            continue
    jobs = [r for _, r in records if 'provider_calls' in r]
    visits = [r for name, r in records if name.startswith('visit-')]
    displays = [r for name, r in records if name.startswith('views-')]
    calls = sum(j.get('provider_calls') or 0 for j in jobs)
    reported = [j for j in jobs if j.get('provider_calls') and isinstance(j.get('usage'), dict)]
    tokens = [j['usage'].get('total_tokens') for j in reported]
    known_tokens = [t for t in tokens if isinstance(t, (int, float)) and not isinstance(t, bool)]
    latencies = sorted(r['display_ms'] for r in displays)
    return dict(visits=len(visits), published_hits=sum(r['published_hit'] for r in visits),
                published_hit_rate=sum(r['published_hit'] for r in visits) / len(visits) if visits else None,
                displayed=len(displays), display_ms_samples=latencies,
                queued_cancelled=sum(j.get('cancel_stage') == 'queued' for j in jobs),
                inflight_cancelled=sum(j.get('cancel_stage') == 'generating' for j in jobs),
                completed_not_displayed=sum(bool(j.get('completed')) and not j.get('displayed') for j in jobs),
                provider_calls=calls, usage_reported_calls=len(reported),
                reported_tokens=sum(known_tokens), total_tokens=sum(known_tokens) if len(known_tokens) == calls else None,
                billed_amount=None, cost_note='Use provider invoices for currency charges; no inferred price or refunds.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Read illustration cache and cancellation metrics')
    parser.add_argument('directory', nargs='?', default='data/illustrations')
    args = parser.parse_args()
    print(json.dumps(report(args.directory), ensure_ascii=False, indent=2))
