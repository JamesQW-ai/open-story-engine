"""Discover every available 100k-CJK novel and its latest official runtime package."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002ebef]')


def longform_cases():
    sources = {}
    for base in (ROOT / 'docs', ROOT / 'content/source'):
        for path in sorted(base.rglob('*.txt')):
            if 'evidence' in path.parts or 'rainy-waiting-room' in path.name:
                continue
            raw = path.read_bytes()
            count = len(CJK.findall(raw.decode('utf-8')))
            if count >= 100000:
                sources[hashlib.sha256(raw).hexdigest()] = (path, count)
    if not sources:
        raise ValueError('没有可验证的十万汉字长篇母本')
    packages = []
    for path in (ROOT / 'content/packages').glob('*/*/package.json'):
        package = json.loads(path.read_text())
        if package.get('story', {}).get('entryModel', {}).get('policy') == 'official_unknown_reader/1':
            packages.append((path, package))
    result = []
    for digest, (source, count) in sources.items():
        candidates = [(p, d) for p, d in packages if d.get('sourceAnalysis', {}).get('sha256') == digest]
        if not candidates:
            raise ValueError('长篇母本缺少匹配的官方运行包：' + str(source))
        path, package = max(candidates, key=lambda pair: tuple(int(v) for v in pair[1]['version'].split('.')))
        result.append(dict(source=source, cjk=count, sha256=digest, path=path,
                           package_id=package['id'], version=package['version'], title=package['metadata']['title']))
    return result
