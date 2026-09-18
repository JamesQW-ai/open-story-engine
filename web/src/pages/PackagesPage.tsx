import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { PackageList } from '../api/types'
import {
  latestPackages,
  Loading,
  Notice,
  packagePath,
  storyImage,
} from '../components/StoryUI'

export function PackagesPage() {
  const [data, setData] = useState<PackageList | null>(null)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const load = () => {
    setError('')
    api
      .listPackages()
      .then(setData)
      .catch(() => setError('书架暂时无法打开，请稍后重试。'))
  }
  useEffect(load, [])
  const stories = latestPackages(data?.packages ?? [])
  const filtered = stories.filter((p) => p.title.includes(query.trim()))
  return (
    <main className="page quiet-shelf">
      <div className="quiet-heading">
        <div>
          <span className="eyebrow">未完之书 · 互动故事</span>
          <h1>选择你的故事</h1>
        </div>
        <Link className="text-button" to="/sessions">继续已有旅程 →</Link>
        {stories.length > 3 && (
          <input
            className="search"
            aria-label="搜索书籍"
            placeholder="搜索书籍…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        )}
      </div>
      <section className="shelf-intro" aria-label="游玩指引">
        <p>成为故事里的人。下一次抉择，由你决定。</p>
        <ol className="shelf-play-path">
          <li><span>01</span> 选故事</li>
          <li><span>02</span> 选身份</li>
          <li><span>03</span> 做出你的选择</li>
        </ol>
      </section>
      {error ? (
        <Notice text={error} retry={load} />
      ) : !data ? (
        <Loading />
      ) : (
        <div className="novel-grid shelf-story-grid">
          {filtered.map((pkg) => {
            const title = pkg.title.replace(/^《(.*)》$/, '$1')
            const setting = title.includes('太虚遗录')
              ? { label: '古典修仙 · 宗门悬疑', hook: '封山钟响，一盏没有火的灯，将你引向宗门不愿提起的往事。' }
              : { label: '互动故事', hook: '选择故事中的一个身份，从眼前的处境开始，走出自己的下一步。' }
            return <Link
              className="novel-cover"
              key={pkg.package_id}
              to={packagePath(pkg)}
              aria-label={`进入《${title}》`}
            >
              <img src={storyImage(pkg.title)} alt="" />
              <span className="shelf-story-setting">{setting.label}</span>
              <div className="novel-cover-copy">
                <h2>{title}</h2>
                <p>{setting.hook}</p>
                <span className="shelf-story-enter">进入故事 <b aria-hidden="true">↗</b></span>
              </div>
            </Link>
          })}
        </div>
      )}
      {data && !stories.length && (
        <div className="empty-state">
          <p>新的故事正在准备中，稍后再来看看。</p>
        </div>
      )}
      {query && !filtered.length && <p className="muted">没有找到这本书。</p>}
    </main>
  )
}
