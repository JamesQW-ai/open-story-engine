"""Load a process-stable prompt catalog without interpreting template code."""
import hashlib
import json
import re
from functools import lru_cache
from importlib.resources import files


FIELD = re.compile(r'\{\{ ([a-z][a-z0-9_]*) \}\}')


class PromptCatalog:
    def __init__(self, root):
        manifest = json.loads(root.joinpath('manifest.json').read_text(encoding='utf-8'))
        self._templates = {}
        for key, spec in manifest['prompts'].items():
            parts = spec['files']
            if not parts or any(not re.fullmatch(r'[a-z0-9_-]+/[a-z0-9_-]+\.md', p) for p in parts):
                raise ValueError('提示词文件路径无效：' + key)
            text = ''.join(root.joinpath(part).read_text(encoding='utf-8') for part in parts)
            fields = set(FIELD.findall(text))
            if fields != set(spec['fields']) or len(spec['fields']) != len(fields):
                raise ValueError('提示词字段与登记不一致：' + key)
            self._templates[key] = (text, fields)
        fingerprint = json.dumps({key: [text, sorted(fields)] for key, (text, fields) in self._templates.items()},
                                 ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        self.version = manifest['version'] + ':' + hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()

    def render(self, key, **values):
        if key not in self._templates:
            raise ValueError('未登记提示词：' + key)
        text, fields = self._templates[key]
        if set(values) != fields:
            raise ValueError('提示词字段不匹配：' + key + '; missing=' + str(sorted(fields - set(values)))
                             + '; extra=' + str(sorted(set(values) - fields)))
        if any(not isinstance(value, str) for value in values.values()):
            raise TypeError('提示词字段须由调用方显式格式化为字符串：' + key)
        # One pass only: braces or template-looking text in player input stay data.
        return FIELD.sub(lambda match: values[match.group(1)], text)


@lru_cache(maxsize=1)
def _catalog():
    return PromptCatalog(files(__package__))


def render_prompt(key, **values):
    return _catalog().render(key, **values)


def catalog_version():
    return _catalog().version
