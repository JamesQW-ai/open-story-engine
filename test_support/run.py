"""Current-product suites. Retired short-story tests are not imported or run."""
import argparse
import ast
import importlib
import unittest
from .longform import ROOT, longform_cases

RETIRED = {
    'core': {'test_python_runtime', 'test_story_draft_regressions'},
    'api': {'test_play_api', 'test_player_routes'},
}


def main():
    parser = argparse.ArgumentParser(description='十万字长篇当前产品回归；历史短篇测试不执行')
    parser.add_argument('suite', choices=('core', 'api'))
    args = parser.parse_args()
    for case in longform_cases():
        print(f"长篇：{case['title']} {case['cjk']} CJK / {case['package_id']}@{case['version']}", flush=True)
    directory = 'tests_py' if args.suite == 'core' else 'tests_api'
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for path in sorted((ROOT / directory).glob('test_*.py')):
        if path.stem in RETIRED[args.suite]:
            count = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')
                        for c in ast.parse(path.read_text()).body if isinstance(c, ast.ClassDef) for n in c.body)
            print(f'历史用例不执行：{path.name}（{count} 项；保留源码，不能计入当前通过数）', flush=True)
            continue
        text = path.read_text()
        if any(token in text for token in ('rainy-waiting-room', '雨夜候车室', '唐栖', '许川', '陈砚', '姜序')):
            raise ValueError('当前测试仍引用停用短篇：' + str(path))
        module = importlib.import_module(directory + '.' + path.stem)
        tests = loader.loadTestsFromModule(module)
        suite.addTests(tests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
