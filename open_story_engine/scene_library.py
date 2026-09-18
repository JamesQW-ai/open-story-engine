"""Reviewed public art, bound to a frozen package and explicit scene conditions."""
import hashlib
import json
import math
import re
from pathlib import Path
from . import item_lifecycle

DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'content' / 'illustrations'


class AssetValidationError(ValueError):
    def __init__(self, field, code, message):
        super().__init__(message)
        self.field, self.code = field, code


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip())


def same_value(actual, expected):
    # JSON true is not the number 1. Missing keys must not match explicit null.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(same_value(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(same_value(a, b) for a, b in zip(actual, expected))
    return actual == expected


class SceneLibrary:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_LIBRARY)

    def manifest(self, package_id, version):
        if (not isinstance(package_id, str) or not isinstance(version, str)
                or not re.fullmatch(r'[\w-]{1,100}', package_id) or not re.fullmatch(r'\d+\.\d+\.\d+', version)):
            return {}
        try:
            data = json.loads((self.root / package_id / version / 'manifest.json').read_text())
            if isinstance(data, dict) and data.get('package_id') == package_id and data.get('version') == version:
                return data
        except (OSError, ValueError, KeyError):
            pass
        return {}

    @staticmethod
    def condition_issues(card, path=''):
        issues = []
        def add(field, code, message):
            issues.append(dict(path=(path + '/' + field if field else path or '/'), code=code, message=message))
        if not isinstance(card, dict):
            add('', 'invalid_object', '条目必须为对象')
            return issues
        if card.get('review_status') != 'approved':
            add('review_status', 'not_approved', '条目未标记为已审校')
        if not nonempty_text(card.get('location_id')):
            add('location_id', 'invalid_location', '必须声明非空地点 ID')
        evidence = card.get('source_evidence')
        if not isinstance(evidence, list) or not evidence or not all(nonempty_text(v) for v in evidence):
            add('source_evidence', 'invalid_evidence', '来源证据必须为非空文本列表')
        for field in ('beats', 'perspective_ids', 'required_text', 'forbidden_text'):
            values = card.get(field, [])
            if not isinstance(values, list):
                add(field, 'invalid_list', '条件必须为文本列表')
                continue
            for index, value in enumerate(values):
                if not nonempty_text(value):
                    add(f'{field}/{index}', 'invalid_text', '条件必须为非空文本')
                elif field in ('required_text', 'forbidden_text'):
                    try:
                        re.compile(value)
                    except (re.error, OverflowError):
                        add(f'{field}/{index}', 'invalid_regex', '正则语法无效或重复次数超限')
        characters = card.get('characters', {})
        if not isinstance(characters, dict):
            add('characters', 'invalid_object', '人物条件必须为对象')
        else:
            for person, expected in characters.items():
                field = 'characters/' + str(person).replace('~', '~0').replace('/', '~1')
                if not nonempty_text(person) or not isinstance(expected, dict):
                    add(field, 'invalid_character', '人物 ID 必须非空，条件必须为对象')
                    continue
                if not nonempty_text(expected.get('appearance_version')):
                    add(field + '/appearance_version', 'invalid_appearance', '必须明确外观版本')
                outcome = expected.get('outcome')
                if (not isinstance(outcome, dict)
                        or outcome.get('status') not in ('alive', 'dead', 'departed', 'missing', 'injured')):
                    add(field + '/outcome/status', 'invalid_outcome', '必须明确有效人物状态，不能以缺失或 unknown 代替')
        requirements = card.get('required_state', {})
        if not isinstance(requirements, dict):
            add('required_state', 'invalid_object', '状态条件必须为对象')
        else:
            for field, facts in requirements.items():
                if (not nonempty_text(field) or not isinstance(facts, dict) or not facts
                        or not all(nonempty_text(k) for k in facts)):
                    add('required_state/' + str(field).replace('~', '~0').replace('/', '~1'),
                        'invalid_state_requirements', '状态条件必须为非空字段映射')
        return issues

    @classmethod
    def _valid_conditions(cls, card):
        return not cls.condition_issues(card)

    @classmethod
    def compatible(cls, card, node):
        if not cls._valid_conditions(card) or not isinstance(node, dict):
            return False
        state = node.get('branchState')
        text = node.get('sceneNarrativeText', node.get('narrativeText'))
        if not isinstance(state, dict) or not isinstance(text, str):
            return False
        if not item_lifecycle.compatible_art(card, state):
            return False
        if state.get('playerLocationId') != card.get('location_id'):
            return False
        if card.get('beats') and node.get('canonicalBeatId') not in card['beats']:
            return False
        if card.get('perspective_ids') and state.get('playerCharacterId') not in card['perspective_ids']:
            return False
        if not all(re.search(pattern, text) for pattern in card.get('required_text', [])):
            return False
        if any(re.search(pattern, text) for pattern in card.get('forbidden_text', [])):
            return False
        # Missing knowledge is not evidence of being alive/uninjured/in costume.
        for person, expected in card.get('characters', {}).items():
            locations, outcomes = state.get('characterLocationIds'), state.get('characterOutcomeStates')
            appearances = state.get('characterAppearanceVersions')
            if not all(isinstance(v, dict) for v in (locations, outcomes, appearances)):
                return False
            if locations.get(person) != card['location_id']:
                return False
            outcome, required = outcomes.get(person), expected['outcome']
            if not isinstance(outcome, dict) or any(k not in outcome or not same_value(outcome[k], v) for k, v in required.items()):
                return False
            if appearances.get(person) != expected['appearance_version']:
                return False
        for field, facts in card.get('required_state', {}).items():
            actual = state.get(field, {})
            if not isinstance(actual, dict) or any(k not in actual or not same_value(actual[k], v) for k, v in facts.items()):
                return False
        return True

    @classmethod
    def asset_issues(cls, card, duplicate_ids=(), path=''):
        issues = cls.condition_issues(card, path)
        if not isinstance(card, dict):
            return issues
        def add(field, code, message):
            issues.append(dict(path=path + '/' + field, code=code, message=message))
        identifier, priority = card.get('id'), card.get('priority', 0)
        if not isinstance(identifier, str) or not re.fullmatch(r'[\w-]{1,100}', identifier):
            add('id', 'invalid_id', '素材 ID 必须为 1～100 位字母、数字、下划线或连字符')
        elif identifier in duplicate_ids:
            add('id', 'duplicate_id', '素材 ID 重复，所有同 ID 条目均不发布')
        for field in ('file', 'alt'):
            if not nonempty_text(card.get(field)):
                add(field, 'invalid_text', '必须为非空文本')
        if not isinstance(card.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', card['sha256']):
            add('sha256', 'invalid_digest', '必须为 64 位小写十六进制 SHA-256')
        if type(priority) not in (int, float) or (type(priority) is float and not math.isfinite(priority)):
            add('priority', 'invalid_priority', '优先级必须为有限数值，不能为布尔值')
        return issues

    @staticmethod
    def duplicate_asset_ids(assets):
        ids = [c.get('id') for c in assets if isinstance(c, dict) and isinstance(c.get('id'), str)]
        return {identifier for identifier in ids if ids.count(identifier) > 1}

    @classmethod
    def _assets(cls, manifest):
        assets = manifest.get('assets', [])
        if not isinstance(assets, list):
            return []
        duplicates = cls.duplicate_asset_ids(assets)
        return [card for card in assets if not cls.asset_issues(card, duplicates)]

    @classmethod
    def live_rule_issues(cls, rule, path=''):
        issues = cls.condition_issues(rule, path)
        if isinstance(rule, dict) and not nonempty_text(rule.get('prompt')):
            issues.append(dict(path=path + '/prompt', code='invalid_prompt', message='生图提示词必须为非空文本'))
        return issues

    def opening(self, package, entry):
        from .official_openings import POLICY
        model = package['story'].get('entryModel', {})
        if model.get('policy') != POLICY or len(entry.get('sourceCharacterIds', [])) != 1:
            return None
        character_id = entry['sourceCharacterIds'][0]
        character = next((c for c in package['characters'] if c['id'] == character_id), {})
        if character.get('defaultEntryPointId') != entry['id']:
            return None
        manifest = self.manifest(package['id'], package['version'])
        if manifest.get('source_sha256') != package.get('sourceAnalysis', {}).get('sha256'):
            return None
        node = dict(canonicalBeatId=entry['beatId'],
                    narrativeText=entry.get('sourceCharacterNarratives', {}).get(character_id, ''),
                    branchState={**entry.get('openingState', {}), 'playerCharacterId': character_id})
        return self.resolve(package['id'], package['version'], node, scene_id=entry['id'])

    def resolve(self, package_id, version, node, *, scene_id=None):
        manifest = self.manifest(package_id, version)
        for card in sorted(self._assets(manifest), key=lambda c: c.get('priority', 0), reverse=True):
            if scene_id is not None and card.get('scene_id') != scene_id:
                continue
            if self.compatible(card, node):
                try:
                    self.asset(package_id, version, card['id'])
                except (OSError, ValueError):
                    continue
                return {'id': card['id'], 'alt': card['alt'], 'source': 'published',
                        'url': f"/api/v1/scene-assets/{package_id}/{version}/{card['id']}"}
        return None

    def asset(self, package_id, version, asset_id):
        manifest = self.manifest(package_id, version)
        card = next((c for c in self._assets(manifest) if c['id'] == asset_id), None)
        if card is None:
            raise ValueError('unpublished asset')
        return self.checked_file(self.root / package_id / version, card)

    @staticmethod
    def checked_file(base, card):
        try:
            base = Path(base).resolve()
            path = (base / card['file']).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise AssetValidationError('file', 'invalid_asset_path', '素材路径无效或存在循环链接') from error
        if base not in path.parents:
            raise AssetValidationError('file', 'asset_path_outside', '素材文件位于当前版本目录之外')
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise AssetValidationError('file', 'asset_missing', '素材文件不存在') from error
        except OSError as error:
            raise AssetValidationError('file', 'asset_unreadable', '素材文件不可读取') from error
        if hashlib.sha256(data).hexdigest() != card['sha256']:
            raise AssetValidationError('sha256', 'asset_digest_mismatch', '素材文件 SHA-256 与登记值不符')
        return path

    def live_policy(self, package_id, version, node):
        rules = self.manifest(package_id, version).get('live_rules', [])
        if not isinstance(rules, list) or not isinstance(node, dict) or not node.get('parentId'):
            return None
        for rule in rules:
            if not self.live_rule_issues(rule) and self.compatible(rule, node):
                return rule
        return None
