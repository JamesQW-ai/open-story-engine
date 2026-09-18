import { ApiError } from '../api/client'
import type { PackageSummary } from '../api/types'

export const storyImage = (title: string) =>
  title.includes('雨夜候车室')
    ? '/images/rainy-station.png'
    : title.includes('太虚遗录')
      ? '/images/taixu-prologue-v1.png'
      : '/images/reading-desk.png'
export const packagePath = (pkg: PackageSummary) =>
  `/packages/${encodeURIComponent(pkg.package_id)}/${encodeURIComponent(pkg.version)}`
export function messageOf(error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === 'session_not_found')
      return '这份存档已不存在，请返回我的故事。'
    if (error.code === 'package_not_found')
      return '这本小说暂时无法打开，请返回书架。'
    if (error.status === 409 || error.status === 422)
      return '暂时无法完成，请检查输入内容后重试。'
    return '暂时无法完成，请稍后重试。'
  }
  return error instanceof Error ? error.message : '暂时无法完成，请稍后重试。'
}
export function Loading({ text = '正在翻开故事……' }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <span className="loader" />
      {text}
    </div>
  )
}
export function Notice({ text, retry }: { text: string; retry?: () => void }) {
  return (
    <div className="notice" role="alert">
      <span>{text}</span>
      {retry && (
        <button className="button subtle" onClick={retry}>
          重试
        </button>
      )}
    </div>
  )
}
export function latestPackages(packages: PackageSummary[]) {
  const map = new Map<string, PackageSummary>()
  for (const pkg of packages) {
    const previous = map.get(pkg.package_id)
    if (
      !previous ||
      pkg.version.localeCompare(previous.version, undefined, {
        numeric: true,
      }) > 0
    )
      map.set(pkg.package_id, pkg)
  }
  return [...map.values()]
}
