"""Export a complete, explicitly reviewed real-model run for offline demonstration."""
import argparse
import hashlib
import html
import json
from pathlib import Path


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build_record(evidence, review):
    report = json.loads((evidence / 'report.json').read_text())
    if report['status'] != 'runtime_passed_pending_prose_review':
        raise ValueError('只有完整实跑成功的记录可以导出')
    turns = report['turns']
    if len(turns) < 3 or len(turns) != report['requestedTurns']:
        raise ValueError('演示至少需要三个连续完成的回合')
    if report.get('novelCjk', 0) < 100000:
        raise ValueError('演示必须来自十万汉字以上官方母本')
    if review.get('decision') != 'approved' or not review.get('reviewer') or not review.get('scope'):
        raise ValueError('缺少明确的逐段审核结论')
    opening = json.loads((evidence / 'opening.json').read_text())
    parent = opening['branch']
    session = opening['session']
    if (session['storyPackageId'], session['storyPackageVersion']) != (report['packageId'], report['packageVersion']):
        raise ValueError('开场与报告的故事包不一致')
    chapters = []

    def append(node, action, title, elapsed=0):
        text = node['narrativeText']
        digest = sha(text)
        if not text.strip() or review.get('narrativeHashes', {}).get(node['id']) != digest:
            raise ValueError('正文未经审核或审核后已变化：' + node['id'])
        if node.get('fallbackMode') or node['sessionId'] != session['id']:
            raise ValueError('不接受降级正文或混合会话')
        chapters.append(dict(id=node['id'], title=title, action=action, text=text,
                             narrativeSha256=digest, stateSha256=sha(json.dumps(node['branchState'], sort_keys=True)),
                             generationMs=elapsed))

    append(parent, '', '山门雨夜 · 开场')
    for number, turn in enumerate(turns, 1):
        result = json.loads((evidence / f'turn-{number}.json').read_text())
        node = result['branch']
        if (turn['status'] != 'committed' or result['status'] != 'written'
                or node['parentId'] != parent['id'] or node['id'] != turn['branchId']
                or node['sequence'] != parent['sequence'] + 1
                or node['narrativeText'] != turn['prose']
                or not turn.get('idempotent') or not turn.get('replayMatchesCommitted')
                or node.get('reviewedNarrativeSha256') != sha(node['narrativeText'])):
            raise ValueError('回合链、审核签名或重放验证不完整')
        append(node, turn['action'], f'第 {number} 回合', turn['elapsedMs'])
        parent = node
    return dict(schemaVersion='prepared-delivery-demo/1', mode='prepared',
                title='太虚遗录 · 山门雨夜', identity='陆照临',
                packageId=report['packageId'], packageVersion=report['packageVersion'],
                novelSha256=report['novelSha256'], novelCjk=report['novelCjk'],
                model=report['model'], promptVersion=report['promptVersion'],
                providerCalls=report['providerCalls'], sourceRun=evidence.name,
                review=review, chapters=chapters)


def standalone_html(record):
    """Self-contained fallback: same unedited record, no service or network."""
    escape = html.escape
    pages = []
    for index, chapter in enumerate(record['chapters']):
        action = f"<blockquote>你的行动：{escape(chapter['action'])}</blockquote>" if chapter['action'] else ''
        paragraphs = ''.join(f'<p>{escape(p)}</p>' for p in chapter['text'].split('\n\n'))
        next_button = (f'<button data-page="{index + 1}">下一步：{escape(record["chapters"][index + 1]["action"])}</button>'
                       if index + 1 < len(record['chapters']) else '<p>本段演示完成。可查看完整记录或从开场演示。</p>')
        pages.append(f'<section class="chapter" data-index="{index}" {"hidden" if index else ""}><h2>{escape(chapter["title"])}</h2>{action}{paragraphs}<footer>{next_button}</footer></section>')
    buttons = ''.join(f'<button data-page="{i}">{escape(c["title"])}</button>' for i, c in enumerate(record['chapters']))
    issues = ''.join(f'<li>第 {issue["chapter"]} 回合：{escape(issue["description"])}</li>' for issue in record['review']['knownMinorIssues'])
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>太虚遗录 · 完整演示记录</title><style>
body{margin:36px auto;max-width:900px;padding:0 28px;background:#14171c;color:#eee9df;font:18px/1.9 system-ui}
h1,h2,.chapter p{font-family:"Songti SC",serif}button{font:inherit;color:inherit;background:#242831;border:1px solid #8f7958;border-radius:8px;padding:10px 16px;cursor:pointer}nav{display:flex;flex-wrap:wrap;gap:10px;margin:24px 0}.chapter{padding:20px 32px;border:1px solid #383b40;border-radius:12px;margin:24px 0}blockquote,details,.mode{color:#b1b0ad}blockquote{border-left:2px solid #d3af70;padding-left:16px;margin-left:0}footer{margin-top:24px}[hidden]{display:none!important}body.all footer{display:none}
</style><main><p class="mode">预生成演示 · 已审核固定路线 · 可离线使用</p>''' + f'<h1>{escape(record["title"])}</h1><p>以{escape(record["identity"])}的身份。正文来自真实模型实跑，现场展示已经审核的原文，不重新生成或改写。</p><nav>{buttons}<button data-page="all">完整记录</button></nav>' + ''.join(pages) + f'<details><summary>记录来源与审核说明</summary><p>{escape(record["review"]["scope"])}</p><p>{escape(record["sourceRun"])} · {escape(record["model"])}</p><ul>{issues}</ul></details></main>' + '''<script>
document.addEventListener('click',event=>{const button=event.target.closest('button[data-page]');if(!button)return;const page=button.dataset.page;document.body.classList.toggle('all',page==='all');document.querySelectorAll('.chapter').forEach(section=>{section.hidden=page!=='all'&&section.dataset.index!==page});window.scrollTo(0,0)});
</script></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--html', type=Path, help='可选：导出不依赖服务的单文件离线演示')
    args = parser.parse_args()
    record = build_record(args.evidence, json.loads(args.review.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as target:
        target.write(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    if args.html:
        with args.html.open('x') as target:
            target.write(standalone_html(record))
    print(f"已导出 {len(record['chapters']) - 1} 个连续回合：{args.output}")


if __name__ == '__main__':
    main()
