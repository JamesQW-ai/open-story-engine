import test from 'node:test'
import assert from 'node:assert/strict'
import { illustrationAssetKey, readIllustrationHistory, rememberIllustration, canShowIllustration } from '../src/components/illustrationHistory.ts'

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

test('revisiting the owning page restores art while other pages still deduplicate', () => {
  const store = storage()
  assert.equal(canShowIllustration('session-1', 'asset', 'page-1', store), true)
  rememberIllustration('session-1', 'asset', 'page-1', store)
  assert.equal(canShowIllustration('session-1', 'asset', 'page-2', store), false)
  assert.equal(canShowIllustration('session-1', 'asset', 'page-1', store), true)
  assert.equal(canShowIllustration('session-2', 'asset', 'page-2', store), true)
  // A refreshed component has no in-memory visible-assets set.
  const refreshed = { getItem: store.getItem }
  assert.equal(canShowIllustration('session-1', 'asset', 'page-1', refreshed), true)
  assert.equal(canShowIllustration('session-1', 'asset', 'page-2', refreshed), false)
})
