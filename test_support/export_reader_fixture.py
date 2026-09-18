"""Export actual long-novel source data for the desktop-only API fault fixture."""
import argparse
import json
from pathlib import Path

from .longform import longform_cases
from open_story_engine.content import load_runtime_story_package
from open_story_engine.cocreation import create_contract, entry_node


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--package-id')
    args = parser.parse_args()
    cases = longform_cases()
    case = next((c for c in cases if c['package_id'] == args.package_id), None) if args.package_id else cases[0]
    if case is None:
        raise ValueError('指定长篇不在已校验清单中')
    package = load_runtime_story_package(case['path'], lazy=True)
    cid = package['story']['entryModel']['sourceCharacterIds'][0]
    contract = create_contract(package, 'desktop-fixture', dict(kind='source_character', sourceCharacterId=cid))
    opening = entry_node(package, contract)
    reader = json.loads(case['path'].with_name('reader.json').read_text())
    character = next(c for c in package['characters'] if c['id'] == cid)
    location = next(p for p in package['locations'] if p['id'] == opening['branchState']['playerLocationId'])
    entry = next(e for e in package['story']['entryModel']['entryPoints'] if e['id'] == contract['entryPointId'])
    # Full source chapters exercise desktop reading length. They are display
    # loads, not model-written alternative history or narrative-quality evidence.
    payload = dict(package_id=case['package_id'], version=case['version'], title=case['title'],
                   source_sha256=case['sha256'], source_cjk=case['cjk'], chapter_count=len(reader['chapters']),
                   character=character, location=location, entry=entry, opening=opening,
                   chapters=reader['chapters'])
    args.output.write_text(json.dumps(payload, ensure_ascii=False))
    print(str(args.output))


if __name__ == '__main__':
    main()
