import { useState } from 'react'
import { StoryProse } from '../components/StoryProse'
import record from '../data/deliveryDemo.json'
import './demo.css'

// Bundled, reviewed prose: stepping through this route performs no API calls.
// Keep this visibly separate from the live free-text story experience.
export function DemoPage() {
  const [index, setIndex] = useState(0)
  const [showRecord, setShowRecord] = useState(false)
  const chapter = record.chapters[index]
  const next = record.chapters[index + 1]

  function select(page: number) {
    setIndex(page)
    setShowRecord(false)
    window.scrollTo(0, 0)
  }

  function downloadRecord() {
    const text = [record.title, '预生成演示记录 · 官方开场 + 真实模型生成的三个回合',
      `身份：${record.identity}`, ...record.chapters.flatMap(c => [
        `\n${c.title}`, ...(c.action ? [`玩家行动：${c.action}`] : []), c.text,
      ]), '\n审核范围', record.review.scope, '\n已记录的小细节误差',
      ...record.review.knownMinorIssues.map(issue => `第 ${issue.chapter} 回合：${issue.description}`)].join('\n\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'taixu-delivery-demo.txt'
    link.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }

  return <main className="demo-page">
    <header className="demo-heading">
      <div><span className="eyebrow">预生成演示 · 已审核固定路线</span>
        <h1>{record.title}</h1><p className="muted">以{record.identity}的身份 · 开场与 {record.chapters.length - 1} 个连续回合</p></div>
      <button className="button subtle" onClick={downloadRecord}>下载完整记录</button>
    </header>
    <p className="demo-mode">正文已提前生成并审核。现场点击展示原始完整段落，不重新生成或改写；自由输入实时续写在“书架”中体验。</p>
    <nav className="demo-progress" aria-label="演示进度">
      {record.chapters.map((c, i) => <button key={c.id} className="button subtle"
        aria-current={!showRecord && index === i ? 'step' : undefined}
        onClick={() => select(i)}>{c.title}</button>)}
      <button className="button subtle" aria-pressed={showRecord} onClick={() => setShowRecord(!showRecord)}>完整记录</button>
    </nav>
    {showRecord ? <article aria-label="完整演示记录" className="demo-paper">
      {record.chapters.map(c => <section key={c.id}>
        <h2>{c.title}</h2>
        {c.action && <p className="demo-action">你的行动：{c.action}</p>}
        <StoryProse text={c.text} title={c.title} opening={false} streaming={false} fixedOpeningImage={null} />
      </section>)}
    </article> : <>
      <article className="demo-paper" aria-label="当前演示正文" key={chapter.id}>
        <h2>{chapter.title}</h2>
        {chapter.action && <p className="demo-action">你的行动：{chapter.action}</p>}
        <StoryProse text={chapter.text} title={chapter.title} opening={index === 0} streaming={false} fixedOpeningImage={null} />
      </article>
      <section className="demo-next" aria-label="演示行动">
        {next ? <><p className="eyebrow">下一步行动</p>
          <button className="demo-choice" onClick={() => select(index + 1)}>{next.action}<span aria-hidden="true"> →</span></button></>
          : <><h2>本段演示完成</h2><p className="muted">三个回合已完整展示。故事停在山门前，文书仍由你持有。</p>
            <button className="button" onClick={() => setShowRecord(true)}>查看完整记录</button>{' '}
            <button className="button subtle" onClick={() => select(0)}>从开场演示</button></>}
      </section>
    </>}
    <details className="demo-review"><summary>记录来源与审核说明</summary>
      <p>{record.review.scope}</p><p>模型：{record.model} · 母本 {record.novelCjk.toLocaleString()} 汉字 · {record.sourceRun}</p>
      <p>原始正文逐字保留，审核登记的小细节误差：</p>
      <ul>{record.review.knownMinorIssues.map(issue => <li key={issue.chapter}>第 {issue.chapter} 回合：{issue.description}</li>)}</ul>
    </details>
  </main>
}
