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
        <h1>书架</h1>
        <Link className="button subtle" to="/import">
          ＋ 添加小说
        </Link>
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
      <section className="shelf-welcome" aria-label="新手引导">
        <div className="welcome-copy">
          <span className="eyebrow">从阅读，到亲历</span>
          <h2>
            故事的下一页，
            <br />
            由你的选择展开。
          </h2>
          <p>挑一本好奇的小说，成为故事里的人。</p>
        </div>
        <ol className="welcome-steps">
          <li>
            <span>01</span>
            <div>
              <strong>挑选小说</strong>
              <p>先看序幕，走进故事的背景。</p>
            </div>
          </li>
          <li>
            <span>02</span>
            <div>
              <strong>选择身份</strong>
              <p>认识人物，从开篇开始你的经历。</p>
            </div>
          </li>
          <li>
            <span>03</span>
            <div>
              <strong>决定下一步</strong>
              <p>选择行动或写下想法，故事逐段继续。</p>
            </div>
          </li>
        </ol>
      </section>
      {error ? (
        <Notice text={error} retry={load} />
      ) : !data ? (
        <Loading />
      ) : (
        <div className="novel-grid">
          {filtered.map((pkg) => (
            <Link
              className="novel-cover"
              key={pkg.package_id}
              to={packagePath(pkg)}
              aria-label={`进入《${pkg.title}》`}
            >
              <img src={storyImage(pkg.title)} alt="" />
              <div className="novel-cover-copy">
                <h2>{pkg.title}</h2>
                <span>进入故事</span>
              </div>
            </Link>
          ))}
        </div>
      )}
      {data && !stories.length && (
        <div className="empty-state">
          <p>书架还是空的。</p>
          <Link className="button primary" to="/import">
            导入小说
          </Link>
        </div>
      )}
      {query && !filtered.length && <p className="muted">没有找到这本书。</p>}
    </main>
  )
}
