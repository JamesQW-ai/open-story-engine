"""收集本轮隔离运行结果；不访问或修改正式数据库。"""
import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parent
source = Path('/private/tmp/ose-scene-plan-current').read_text().strip()
destination = root / 'batch-1'
destination.mkdir(exist_ok=True)
for file in Path(source).glob('*.json'):
    shutil.copy2(file, destination / file.name)


def observations(value):
    if isinstance(value, dict):
        if isinstance(value.get('callObservations'), list):
            yield from value['callObservations']
        for key, item in value.items():
            if key not in ('rawResponse', 'callObservations'):
                yield from observations(item)
    elif isinstance(value, list):
        for item in value:
            yield from observations(item)


report = json.loads((destination / 'report.json').read_text())
analysis = []
readable = ['此文件保留实际用户输入与最终正文。生成写入与叙事质量是两个独立判定。', '']
for turn in report['turns']:
    kind = turn['kind']
    record = json.loads((destination / (kind + '.json')).read_text())
    job = json.loads((destination / (kind + '-job.json')).read_text())
    stages = list(observations(job))
    analysis.append({**turn,
        'scenePlans': [o for o in stages if o.get('generationStage') == 'scene_plan'],
        'drafts': [o for o in stages if o.get('generationStage') == 'scene_draft_metrics'],
        'failures': [o for o in stages if o.get('generationStage') == 'validation'],
        'repairs': [o for o in stages if o.get('generationStage') == 'local_repair_record'],
        'stages': [o for o in stages if 'transport' in o],
    })
    body = record['result'].get('branch', {}).get('narrativeText') or record.get('visibleTextOnFailure') or '没有保留正文。'
    readable += ['## ' + kind, '', '用户输入：' + record['input']['text'], '', '状态：' + turn['status'], '', body, '']
(root / 'analysis-final.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + '\n')
(root / 'readable-scenes-final.md').write_text('\n'.join(readable))
print(json.dumps([{k: x[k] for k in ('kind', 'status', 'actualCjk', 'elapsedSeconds', 'metrics')} for x in analysis], ensure_ascii=False))
