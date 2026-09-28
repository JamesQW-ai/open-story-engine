"""Frozen new-scene holdout using the unchanged qualifier contract."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from test_support import context_qualifier_evidence_eval as v
from test_support import context_qualifier_evidence as e
from test_support.longform import longform_cases

f, q = v.f, v.q
FIXTURE = f.ROOT/'test_support/fixtures/context-qualifier-heldout-2026-09-26.json'
PROMPT = v.PROMPT
OUTPUT = f.OUTPUT.with_name('context-qualifier-heldout-2026-09-26.json')
AUDIT = f.OUTPUT.with_name('context-qualifier-heldout-audit-2026-09-26.json')


def load_cases(path=FIXTURE):
    fixture = json.loads(path.read_text())
    source = fixture['source']
    book = next((b for b in longform_cases() if b['package_id'] == source['packageId'] and b['version'] == source['version']), None)
    if (book is None or book['sha256'] != source['sha256'] or book['cjk'] != source['cjk']
            or str(book['source'].relative_to(f.ROOT)) != source['path']):
        raise ValueError('官方长篇来源绑定变化')
    text = book['source'].read_text()
    scene = source['sceneSpan']
    def span(ref):
        if (set(ref) != {'start','end','quote'} or type(ref['start']) is not int or type(ref['end']) is not int
                or not 0 <= ref['start'] < ref['end'] <= len(text) or text[ref['start']:ref['end']] != ref['quote']):
            raise ValueError('来源跨度无效')
    span(scene)
    chapter = f.ROOT/source['chapterPath']
    if f.sha(chapter) != source['chapterSha256']:
        raise ValueError('官方章节模块变化')
    bounds = json.loads(chapter.read_text())['chapter']['lineRange']
    if not bounds['start'] <= text.count('\n',0,scene['start'])+1 <= text.count('\n',0,scene['end'])+1 <= bounds['end']:
        raise ValueError('场景不在冻结章节范围内')
    previous = json.loads(v.OUTPUT.read_text())['inputHashes']
    expected = {str(p.relative_to(f.ROOT)) for p in (PROMPT, f.ROOT/'test_support/context_qualifier_evidence.py', f.ROOT/'test_support/context_fullscan.py')}
    if set(fixture['frozenImplementationHashes']) != expected:
        raise ValueError('冻结实现集合变化')
    for name, digest in fixture['frozenImplementationHashes'].items():
        if digest != previous[name] or f.sha(f.ROOT/name) != digest:
            raise ValueError('留出实验不得改变提示或评分实现')
    entities = fixture['entities']
    if not entities or set(entities) != set(fixture['entitySources']):
        raise ValueError('实体称呼来源不完整')
    for key, entity in entities.items():
        refs = fixture['entitySources'][key]
        if (set(entity) != {'kind','mentions'} or entity['kind'] not in {'scene_person','scene_boundary'}
                or not refs or entity['mentions'] != [r['quote'] for r in refs]):
            raise ValueError('实体称呼与原文不一致')
        for ref in refs:
            span(ref)
            if not scene['start'] <= ref['start'] < ref['end'] <= scene['end']:
                raise ValueError('实体称呼超出可见场景')
    seen = set()
    for case in fixture['cases']:
        if not isinstance(case['id'],str) or not case['id'] or case['id'] in seen:
            raise ValueError('用例编号无效')
        seen.add(case['id'])
        if hashlib.sha256(case['draft'].encode()).hexdigest() != case['draftSha256']:
            raise ValueError('用例正文变化')
        if case['origin'] == 'source_excerpt':
            if not case['draft'] or case['draft'] not in scene['quote']:
                raise ValueError('原文样例不在来源片段中')
        elif case['origin'] != 'synthetic_scene_control':
            raise ValueError('未知样例来源类型')
        units = f.indexed_input(case)['units']
        signatures = set()
        for t in case['targets']:
            ids = t['unitIds']
            if not ids or ids != [i for i in units if i in ids]:
                raise ValueError('金标单元不存在、重复或乱序')
            if t['expectedStatus'] == 'candidate':
                f._normal(t['expectedNormal'],entities)
                if t['expectedLimitations']:
                    raise ValueError('候选不得丢弃限定')
            elif t['expectedStatus'] == 'needs_review':
                flags = t['expectedLimitations']
                if t['expectedNormal'] is not None or not flags or not set(flags) <= f.LIMITATIONS or len(set(flags)) != len(flags):
                    raise ValueError('未决标签无效')
            else:
                raise ValueError('未知金标状态')
            signature = f._signature(ids,t['expectedStatus'],t['expectedNormal'],t['expectedLimitations'])
            if signature in signatures:
                raise ValueError('重复金标')
            signatures.add(signature)
    if len(seen) != 9 or sum(c['origin']=='source_excerpt' for c in fixture['cases']) != 1:
        raise ValueError('本轮冻结为一段原文、八条控制稿')
    return fixture


def messages(case, entities):
    return [dict(role='system', content=PROMPT.read_text()),
            dict(role='user', content=json.dumps(f.model_input(case, entities), ensure_ascii=False))]


def summary(rows):
    return {s: sum(r['status'] == s for r in rows) for s in
            ('matched', 'mismatched', 'invalid_response', 'model_error', 'harness_error', 'not_run')}


def run(output):
    fixture = load_cases()
    config = f.writer_config_from_env()
    if any(not config.get(k) for k in ('base_url', 'api_key', 'model')) or 'json' not in PROMPT.read_text().lower():
        raise ValueError('模型配置或 JSON 提示无效')
    dependencies = q.DEPENDENCIES + ['test_support/context_qualifier_eval.py', 'test_support/context_qualifier_evidence.py']
    dependencies += ['test_support/longform.py', 'test_support/context_qualifier_evidence_eval.py']
    paths = [FIXTURE, PROMPT, Path(__file__)] + [f.ROOT/p for p in dependencies]
    report = dict(schemaVersion='context-qualifier-heldout/0.1', status='incomplete',
                  recordedAt=datetime.now(timezone.utc).isoformat(),
                  inputHashes={str(p.relative_to(f.ROOT)): f.sha(p) for p in paths},
                  priorEvidenceHashes={str(p.relative_to(f.ROOT)): f.sha(p) for p in (v.OUTPUT, v.AUDIT)},
                  model={k: config[k] for k in ('route', 'model')}, scope=fixture['scope'],
                  callLimit=9, actualCalls=0, cases=[], acceptance=False, productionEnablement=False,
                  formalSessionWrites=0, reviewRecordsWritten=0, jevCalls=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    f.save_checkpoint(output, report, create=True)
    recorder = f._gateway(config, 9)

    def save():
        report['actualCalls'] = len(recorder.calls)
        try:
            f.save_checkpoint(output, report)
        except OSError as error:
            raise f.CheckpointError('证据保存失败，停止调用') from error

    blocked = False
    for case in fixture['cases']:
        row = dict(caseId=case['id'], status='incomplete', calls=[])
        report['cases'].append(row)
        if blocked:
            row['status'] = 'not_run'
            save()
            continue
        before = len(recorder.calls)
        try:
            try:
                response = recorder.complete_json(messages(case, fixture['entities']))
            finally:
                row['calls'].extend(recorder.calls[before:])
                save()
            try:
                row.update(e.assess(f.parse_json_content(response.content), case, fixture['entities']))
            except (ValueError, f.LlmError) as error:
                row.update(status='invalid_response', error=str(error))
        except f.CheckpointError:
            raise
        except f.LlmError as error:
            row.update(status='model_error', error=str(error))
            blocked = True
        except Exception as error:
            row.update(status='harness_error', error=str(error))
            blocked = True
        save()
        print(json.dumps(dict(caseId=row['caseId'], status=row['status'])), flush=True)
    report['status'] = 'blocked' if blocked else 'completed'
    report['summary'] = summary(report['cases'])
    save()
    return report


def audit(source=OUTPUT):
    fixture = load_cases()
    report = json.loads(source.read_text())
    for name, h in {**report['inputHashes'], **report['priorEvidenceHashes']}.items():
        if f.sha(f.ROOT/name) != h:
            raise ValueError('输入、实现或历史证据变化：'+name)
    if report['status'] != 'completed' or report['actualCalls'] != 9 or len(report['cases']) != 9:
        raise ValueError('实验未完成')
    rows = []
    for case, row in zip(fixture['cases'], report['cases']):
        if case['id'] != row['caseId'] or len(row['calls']) != 1:
            raise ValueError('案例或请求次数不一致')
        call = row['calls'][0]
        if call['messages'] != messages(case, fixture['entities']):
            raise ValueError('实际输入不一致')
        result = e.assess(f.parse_json_content(call['content']), case, fixture['entities'])
        if any(row[k] != v for k, v in result.items()):
            raise ValueError('评分不可复现')
        rows.append(dict(caseId=case['id'], **result))
    if summary(rows) != report['summary']:
        raise ValueError('汇总不可复现')
    name = source.relative_to(f.ROOT) if source.is_relative_to(f.ROOT) else source
    return dict(schemaVersion='context-qualifier-heldout-audit/0.1', sourceArtifact=str(name),
                sourceSha256=f.sha(source), originalCalls=9, newModelCalls=0, actualInputsVerified=True,
                allScoresReproduced=True, cases=rows, summary=summary(rows),
                requiresSemanticReview=True, acceptance=False, productionEnablement=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.audit:
        result = audit()
        f.save_checkpoint(args.output or AUDIT, result, create=True)
    else:
        f._load_env(f.ROOT/'.env')
        result = run(args.output or OUTPUT)
    print(json.dumps(result['summary']))
