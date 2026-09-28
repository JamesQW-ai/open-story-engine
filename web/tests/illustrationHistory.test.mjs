import test from 'node:test'
import assert from 'node:assert/strict'
import { illustrationAssetKey, readIllustrationHistory, rememberIllustration } from '../src/components/illustrationHistory.ts'

function storage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
}

test('physical asset fingerprint wins over aliases for once-only history', () => {
  const store = storage()
  const image = { id: 'chapter-alias', asset_key: 'same-file-sha256', url: '/alias.png' }
  assert.equal(illustrationAssetKey(image), 'same-file-sha256')
  rememberIllustration('session-1', illustrationAssetKey(image), 'branch-1', store)
  assert.equal(readIllustrationHistory('session-1', store)['same-file-sha256'], 'branch-1')
  assert.equal(readIllustrationHistory('session-1', store)['chapter-alias'], undefined)
})

test('history remains empty when storage is unavailable', () => {
  assert.deepEqual(readIllustrationHistory('session-1', null), {})
  rememberIllustration('session-1', 'asset', 'branch-1', null)
})
