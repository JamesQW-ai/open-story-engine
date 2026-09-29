export type IllustrationAsset = {
  asset_key?: string | null
  id?: string | null
  url?: string | null
  index?: number
}

export function illustrationAssetKey(image: IllustrationAsset): string | undefined {
  return image.asset_key || image.id || image.url || (image.index === undefined ? undefined : `index:${image.index}`)
}

function historyKey(sessionId: string) {
  return `story-illustrations-used:${sessionId}`
}

function storageOrNull(storage?: Storage | null): Storage | null {
  if (storage) return storage
  try {
    return sessionStorage
  } catch {
    return null
  }
}

export function readIllustrationHistory(sessionId: string, storage?: Storage | null): Record<string, string> {
  try {
    const target = storageOrNull(storage)
    if (!target) return {}
    const parsed = JSON.parse(target.getItem(historyKey(sessionId)) ?? '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, string>
      : {}
  } catch {
    return {}
  }
}

export function rememberIllustration(sessionId: string, assetKey: string, branchId: string, storage?: Storage | null) {
  try {
    const target = storageOrNull(storage)
    if (!target) return
    const history = readIllustrationHistory(sessionId, target)
    history[assetKey] = branchId
    target.setItem(historyKey(sessionId), JSON.stringify(history))
  } catch {
    /* Storage is optional; reading must continue. */
  }
}

export function canShowIllustration(sessionId: string, assetKey: string, branchId?: string, storage?: Storage | null): boolean {
  const owner = readIllustrationHistory(sessionId, storage)[assetKey]
  return !owner || owner === branchId
}
