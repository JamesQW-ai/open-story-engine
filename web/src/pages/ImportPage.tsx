import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api } from '../api/client'
import { messageOf, Notice } from '../components/StoryUI'

export function ImportPage() {
  const navigate = useNavigate()
  const [file, setFile] = useState<{ name: string; text: string } | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [reading, setReading] = useState(false)
  const [playable, setPlayable] = useState(false)
  const readId = useRef(0)
  useEffect(() => {
    api
      .health()
      .then((h) => setPlayable(h.phase === 'play'))
      .catch((e) => setError(messageOf(e)))
    return () => {
      readId.current++
    }
  }, [])
  async function pickFile(next?: File) {
    if (!next || busy) return
    const id = ++readId.current
    setReading(true)
    setFile(null)
    setError('')
    try {
      if (!/\.txt$/i.test(next.name)) throw new Error('请选择 .txt 小说文件。')
      if (next.size > 10_000_000)
        throw new Error('文件超过 10 MB，请选择更小的小说文件。')
      let text: string
      try {
        text = new TextDecoder('utf-8', { fatal: true }).decode(
          await next.arrayBuffer(),
        )
      } catch {
        throw new Error('无法读取文件，请使用 UTF-8 编码的 TXT。')
      }
      if (text.trim().length < 100 || text.includes('\0'))
        throw new Error('小说正文需至少 100 字，并包含书名和章节。')
      if (id === readId.current) setFile({ name: next.name, text })
    } catch (e) {
      if (id === readId.current) setError(messageOf(e))
    } finally {
      if (id === readId.current) setReading(false)
    }
  }
  async function runImport() {
    if (!file || busy || reading || !playable) return
    setBusy(true)
    setError('')
    try {
      const result = await api.importNovel({
        file_name: file.name,
        source_text: file.text,
      })
      navigate(
        `/packages/${encodeURIComponent(result.package_id)}/${encodeURIComponent(result.version)}`,
      )
    } catch (e) {
      setError(messageOf(e))
      setBusy(false)
    }
  }
  return (
    <main className="page">
      <Link className="back-link" to="/packages">
        返回书架
      </Link>
      <div className="setup-layout import-layout">
        <aside className="setup-art">
          <img src="/images/reading-desk.png" alt="暖灯下翻开的书与雨夜的窗" />
        </aside>
        <section className="setup-panel">
          <h1>添加小说</h1>
          <label
            className={`dropzone ${busy ? 'disabled' : ''}`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault()
              if (!busy) void pickFile(e.dataTransfer.files[0])
            }}
          >
            <span className="upload-symbol">TXT</span>
            <strong>{file ? file.name : '拖入 TXT 文件，或点击选择'}</strong>
            <span>
              {reading
                ? '正在读取…'
                : file
                  ? `已读取 ${file.text.length.toLocaleString()} 字 · 点击重新选择`
                  : 'UTF-8 编码 · 最大 10 MB'}
            </span>
            <input
              type="file"
              accept=".txt"
              aria-label="选择 TXT 小说"
              disabled={busy}
              onChange={(e) => {
                void pickFile(e.target.files?.[0])
                e.target.value = ''
              }}
            />
          </label>
          <p className="format-note">
            第一行写书名，章节标题单独成行，例如“第一章 雨夜”，下方填写正文。
          </p>
          {error && <Notice text={error} />}
          {!playable && <p className="muted">导入服务暂未就绪，请稍后重试。</p>}
          <button
            className="button primary large full-width"
            disabled={!file || busy || reading || !playable}
            onClick={runImport}
          >
            {busy ? '正在整理故事，请稍候…' : '添加小说 '}
          </button>
        </section>
      </div>
    </main>
  )
}
