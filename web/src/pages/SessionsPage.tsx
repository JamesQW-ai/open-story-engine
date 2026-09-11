import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import type { PackageSummary, SessionList } from '../api/types'
import { Loading, Notice, storyImage } from '../components/StoryUI'

type Save = SessionList['sessions'][number]
export function SessionsPage() {
  const [data, setData] = useState<SessionList | null>(null)
  const [packages, setPackages] = useState<PackageSummary[]>([])
  const [error, setError] = useState('')
  const [pending, setPending] = useState<Save | null>(null)
  const [renaming, setRenaming] = useState<Save | null>(null)
  const [saveName, setSaveName] = useState('')
  const [savingName, setSavingName] = useState(false)
  const renameDialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    if (renaming) renameDialog.current?.showModal()
    else renameDialog.current?.close()
  }, [renaming])
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const deleteLock = useRef(false)
  const load = () => {
    setError('')
    api
      .listSessions()
      .then(setData)
      .catch(() => setError('存档暂时无法打开，请重试。'))
    api
      .listPackages()
      .then((p) => setPackages(p.packages))
      .catch(() => undefined)
  }
  useEffect(load, [])
  useEffect(() => {
    if (pending) dialog.current?.showModal()
    else dialog.current?.close()
  }, [pending])
  const titleOf = (save: Save) =>
    packages.find(
      (p) => p.package_id === save.package_id && p.version === save.version,
    )?.title ?? '我的故事'
  const saveTitle = (save: Save) =>
    save.title ||
    `《${titleOf(save)}》· ${save.role_name || '未命名身份'} · ${save.created_at ? new Date(save.created_at).toLocaleString('zh-CN') : '故事开篇'}`
  const dateOf = (save: Save) =>
    save.updated_at || save.created_at
      ? new Date((save.updated_at || save.created_at)!).toLocaleString(
          'zh-CN',
          {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
          },
        )
      : '已保存'
  async function removeSave() {
    if (!pending || deleteLock.current) return
    deleteLock.current = true
    setDeleting(true)
    setDeleteError('')
    const id = pending.id
    try {
      try {
        await api.deleteSession(id)
      } catch (e) {
        if (
          !(
            e instanceof ApiError &&
            e.status === 404 &&
            e.code === 'session_not_found'
          )
        )
          throw e
      }
      setData((prev) =>
        prev
          ? { ...prev, sessions: prev.sessions.filter((s) => s.id !== id) }
          : prev,
      )
      try {
        localStorage.removeItem(`story-progress:${id}`)
      } catch {
        /* 不影响服务端删除 */
      }
      setPending(null)
    } catch {
      setDeleteError('未能删除存档，请重试。')
    } finally {
      deleteLock.current = false
      setDeleting(false)
    }
  }
  return (
    <main className="page save-page">
      <div className="quiet-heading">
        <div>
          <h1>我的故事</h1>
          <span className="muted">游戏存档</span>
        </div>
        <Link to="/packages" className="button subtle">
          新故事 ＋
        </Link>
      </div>
      {error ? (
        <Notice text={error} retry={load} />
      ) : !data ? (
        <Loading />
      ) : !data.sessions.length ? (
        <section className="empty-state">
          <img src="/images/reading-desk.png" alt="等待翻开的书" />
          <h2>还没有存档</h2>
          <Link to="/packages" className="button primary">
            挑选小说
          </Link>
        </section>
      ) : (
        <div className="save-grid">
          {data.sessions.map((save) => (
            <article className="save-card" key={save.id}>
              <Link
                className="save-art"
                to={`/sessions/${encodeURIComponent(save.id)}`}
                aria-label={`继续《${titleOf(save)}》`}
              >
                <img src={storyImage(titleOf(save))} alt="" />
              </Link>
              <div className="save-copy">
                <h2>{saveTitle(save)}</h2>
                {save.title && (
                  <p className="muted">
                    {titleOf(save)} · {save.role_name}
                  </p>
                )}
                <p className="save-progress">
                  {save.recent_progress?.replace(
                    /已推进，新的风险和选择仍然存在。$/,
                    '',
                  ) || '故事开篇'}
                </p>
                <time dateTime={save.created_at ?? undefined}>
                  {dateOf(save)}
                </time>
                <div className="save-actions">
                  <Link
                    className="text-link"
                    to={`/sessions/${encodeURIComponent(save.id)}`}
                  >
                    继续故事
                  </Link>
                  <button
                    className="text-button"
                    onClick={() => {
                      setSaveName(saveTitle(save))
                      setRenaming(save)
                    }}
                  >
                    命名
                  </button>
                  <button
                    className="text-button delete-save"
                    aria-label={`删除《${titleOf(save)}》${dateOf(save)}的存档`}
                    onClick={() => {
                      setDeleteError('')
                      setPending(save)
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
      <dialog
        ref={renameDialog}
        className="save-dialog"
        aria-label="存档命名"
        onCancel={() => setRenaming(null)}
      >
        <form
          onSubmit={async (e) => {
            e.preventDefault()
            if (!renaming || savingName || !saveName.trim()) return
            setSavingName(true)
            try {
              await api.renameSession(renaming.id, saveName.trim())
              setData((prev) =>
                prev
                  ? {
                      ...prev,
                      sessions: prev.sessions.map((s) =>
                        s.id === renaming.id
                          ? { ...s, title: saveName.trim() }
                          : s,
                      ),
                    }
                  : prev,
              )
              setRenaming(null)
            } catch {
              setError('存档名称未能保存，请重试。')
            } finally {
              setSavingName(false)
            }
          }}
        >
          <h2>给这段故事起个名字</h2>
          <input
            aria-label="存档名称"
            autoFocus
            maxLength={80}
            value={saveName}
            onChange={(e) => setSaveName(e.target.value)}
          />
          <div className="dialog-actions">
            <button
              type="button"
              className="button subtle"
              disabled={savingName}
              onClick={() => setRenaming(null)}
            >
              取消
            </button>
            <button
              className="button primary"
              disabled={savingName || !saveName.trim()}
            >
              保存名称
            </button>
          </div>
        </form>
      </dialog>
      <dialog
        ref={dialog}
        className="save-dialog"
        aria-labelledby="delete-save-title"
        aria-describedby="delete-save-description"
        onCancel={(e) => {
          if (deleteLock.current) e.preventDefault()
          else setPending(null)
        }}
      >
        <h2 id="delete-save-title">删除这份存档？</h2>
        <p id="delete-save-description">
          《{pending ? titleOf(pending) : ''}》<br />
          {pending ? dateOf(pending) : ''}
          <br />
          这份存档的全部进度与分支将永久删除。
        </p>
        {deleteError && (
          <p role="alert" className="delete-error">
            {deleteError}
          </p>
        )}
        <div className="dialog-actions">
          <button
            autoFocus
            className="button subtle"
            disabled={deleting}
            onClick={() => setPending(null)}
          >
            保留存档
          </button>
          <button
            className="button danger"
            disabled={deleting}
            onClick={removeSave}
          >
            {deleting ? '正在删除…' : '删除存档'}
          </button>
        </div>
      </dialog>
    </main>
  )
}
