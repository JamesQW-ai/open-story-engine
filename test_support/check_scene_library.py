"""Read-only author diagnostics for all eligible official long novels."""
import argparse
import json
from pathlib import Path

from open_story_engine.scene_library import AssetValidationError, DEFAULT_LIBRARY, SceneLibrary
from .longform import longform_cases


def check_case(case, library_root=DEFAULT_LIBRARY):
    base = Path(library_root) / case['package_id'] / case['version']
    manifest_path = base / 'manifest.json'
    result = dict(package_id=case['package_id'], version=case['version'], source_cjk=case['cjk'],
                  manifest=str(manifest_path.absolute()), status='invalid',
                  asset_count=None, checked_assets=0, live_rule_count=None, checked_live_rules=0, issues=[])
    issues = result['issues']
    def add(path, code, message, **details):
        issues.append(dict(path=path, code=code, message=message, **details))
    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        add('/', 'manifest_missing', '素材清单不存在，未完成检查')
        return result
    except json.JSONDecodeError as error:
        add('/', 'invalid_json', '素材清单 JSON 无法解析', line=error.lineno, column=error.colno)
        return result
    except (OSError, ValueError):
        add('/', 'manifest_unreadable', '素材清单不可读取或文本编码无效')
        return result
    if not isinstance(data, dict):
        add('/', 'invalid_object', '素材清单根节点必须为对象')
        return result
    for field, expected in (('package_id', case['package_id']), ('version', case['version']),
                            ('source_sha256', case['sha256']), ('schema_version', 'story-scene-library/1')):
        if data.get(field) != expected:
            add('/' + field, 'binding_mismatch', '清单绑定与当前达标官方长篇或支持的格式不一致')
    for field in ('assets', 'live_rules'):
        cards = data.get(field, [])
        if not isinstance(cards, list):
            add('/' + field, 'invalid_list', '条目集合必须为列表')
            continue
        result['asset_count' if field == 'assets' else 'live_rule_count'] = len(cards)
        duplicates = SceneLibrary.duplicate_asset_ids(cards) if field == 'assets' else ()
        for index, card in enumerate(cards):
            path = f'/{field}/{index}'
            problems = (SceneLibrary.asset_issues(card, duplicates, path) if field == 'assets'
                        else SceneLibrary.live_rule_issues(card, path))
            if field == 'assets' and not problems:
                try:
                    SceneLibrary.checked_file(base, card)
                except AssetValidationError as error:
                    problems.append(dict(path=path + '/' + error.field, code=error.code, message=str(error)))
            issues.extend(problems)
            if not problems:
                result['checked_assets' if field == 'assets' else 'checked_live_rules'] += 1
    result['status'] = 'valid' if not issues else 'invalid'
    return result


def main():
    parser = argparse.ArgumentParser(description='只读检查全部十万汉字官方长篇的素材清单，不生图、不修改素材')
    parser.add_argument('--library-root', type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument('--package-id', help='可选：只检查自动盘点清单中的一个故事包')
    args = parser.parse_args()
    cases = longform_cases()
    if args.package_id:
        cases = [case for case in cases if case['package_id'] == args.package_id]
        if not cases:
            parser.error('指定故事包不在达标官方长篇清单中')
    books = [check_case(case, args.library_root) for case in cases]
    errors = sum(len(book['issues']) for book in books)
    result = dict(schema_version='scene-library-check/1', status='invalid' if errors else 'valid',
                  book_count=len(books), error_count=errors, books=books,
                  scope='仅验证清单结构、版本绑定和已登记文件完整性，不代表图片内容审校或全书覆盖验收')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
