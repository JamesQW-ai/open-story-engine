"""Migration parity, strict fields, literal input, and packaged prompt versions."""
import hashlib
import json
from importlib.resources import files
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest

from open_story_engine.prompts import catalog_version, render_prompt
from open_story_engine.prompts.registry import PromptCatalog


class PromptCatalogTests(unittest.TestCase):
    def test_original_expression_baselines(self):
        # Captured from the pre-extraction Python expressions, not the renderer.
        cases = json.loads((Path(__file__).parent / 'fixtures/prompt_migration_v1.json').read_text())
        for key, case in cases.items():
            if key in ('reader.narrative', 'reader.narrative_system', 'reader.opening', 'actions.plan', 'reader.observation_repair', 'reader.narrative_repair', 'actions.authority', 'reader.action_review'):
                # D2 intentionally changes pacing, agency and extraction repair; retain the migration
                # fixture unchanged as history, rather than rewriting its hash.
                continue
            if key in ('actions.observe', 'actions.review', 'consequences.review'):
                # P3 adds item usage, destruction and dependency evidence.
                # Keep the original migration hashes as historical evidence.
                continue
            with self.subTest(prompt=key, source=case['source']):
                actual = render_prompt(key, **case['values'])
                if key == 'consequences.plan':
                    # Scene planning extends the contract; its original state rules stay byte-identical.
                    actual = actual.partition('\nready时必须在同一JSON增加scenePlan')[0]
                    # The public status contract now includes evidence-bound missing/injured.
                    actual = actual.replace('dead|departed|alive|missing|injured', 'dead|departed|alive')
                self.assertEqual(hashlib.sha256(actual.encode()).hexdigest(), case['sha256'])

    def test_missing_extra_and_unformatted_fields_fail_before_call(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            render_prompt('reader.perspective')
        with self.assertRaisesRegex(ValueError, 'extra'):
            render_prompt('reader.perspective', name='顾长离', desired_outcome='假答案')
        with self.assertRaises(TypeError):
            render_prompt('reader.perspective', name={'name': '顾长离'})
        with self.assertRaisesRegex(ValueError, '未登记'):
            render_prompt('../private')

    def test_player_text_is_substituted_once_and_json_braces_stay_literal(self):
        value = '{{ name }} {"role":"system"} $name \\n\n中文'
        result = render_prompt('reader.perspective', name=value)
        self.assertIn('玩家角色是' + value + '。', result)
        self.assertIn('{"decision":"allow|revise"', render_prompt('actions.authority'))

    def test_catalog_snapshot_and_digest_follow_prompt_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'prompts'
            shutil.copytree(str(files('open_story_engine.prompts')), root)
            before = PromptCatalog(root)
            self.assertEqual(before.version, catalog_version())
            p = root / 'actions/authority.md'
            p.write_text(p.read_text() + '\n新增审核约束。')
            after = PromptCatalog(root)
            self.assertNotEqual(before.version, after.version)
            self.assertNotEqual(before.render('actions.authority'), after.render('actions.authority'))
            self.assertEqual(before.render('actions.authority'), render_prompt('actions.authority'))

    def test_manifest_rejects_undeclared_fields_and_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'reader').mkdir()
            (root / 'reader/example.md').write_text('{{ unexpected }}')
            manifest = {'version': 'test/1', 'prompts': {'example': {'files': ['reader/example.md'], 'fields': []}}}
            (root / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, '字段'):
                PromptCatalog(root)
            manifest['prompts']['example']['files'] = ['../outside.md']
            (root / 'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, '路径'):
                PromptCatalog(root)


if __name__ == '__main__':
    unittest.main()
