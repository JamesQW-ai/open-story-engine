"""Reviewed public art, bound to a frozen package and explicit scene conditions."""
import hashlib
import json
import re
from pathlib import Path

DEFAULT_LIBRARY = Path(__file__).resolve().parents[1] / 'content' / 'illustrations'


class SceneLibrary:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_LIBRARY)

    def manifest(self, package_id, version):
        if not re.fullmatch(r'[\w-]{1,100}', package_id or '') or not re.fullmatch(r'\d+\.\d+\.\d+', version or ''):
            return {}
        try:
            data = json.loads((self.root / package_id / version / 'manifest.json').read_text())
            if data['package_id'] == package_id and data['version'] == version:
                return data
        except (OSError, ValueError, KeyError):
            pass
        return {}

    @staticmethod
    def compatible(card, node):
        state = node.get('branchState') or {}
        text = node.get('narrativeText') or ''
        if card.get('review_status') != 'approved' or not card.get('source_evidence'):
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
            if state.get('characterLocationIds', {}).get(person) != card['location_id']:
                return False
            if state.get('characterOutcomeStates', {}).get(person) != expected['outcome']:
                return False
            if state.get('characterAppearanceVersions', {}).get(person) != expected['appearance_version']:
                return False
        for field, facts in card.get('required_state', {}).items():
            actual = state.get(field, {})
            if not isinstance(actual, dict) or any(k not in actual or actual[k] != v for k, v in facts.items()):
                return False
        return True

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
        for card in sorted(manifest.get('assets', []), key=lambda c: c.get('priority', 0), reverse=True):
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
        card = next((c for c in manifest.get('assets', []) if c['id'] == asset_id and c.get('review_status') == 'approved'), None)
        if card is None:
            raise ValueError('unpublished asset')
        base = (self.root / package_id / version).resolve()
        path = (base / card['file']).resolve()
        if base not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != card['sha256']:
            raise ValueError('asset integrity mismatch')
        return path

    def live_policy(self, package_id, version, node):
        for rule in self.manifest(package_id, version).get('live_rules', []):
            if node.get('parentId') and self.compatible(rule, node):
                return rule
        return None
