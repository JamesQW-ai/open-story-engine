import test from 'node:test'
import assert from 'node:assert/strict'
import { prepareOpeningImage } from '../src/components/openingImage.ts'

const asset = { id: 'gate', url: '/gate.png', alt: '山门', source: 'published' }
function fixture() { return { complete: false, naturalWidth: 0, onload: null, onerror: null, src: '' } }

test('opening art is ready before prose, including a cached image', async () => {
  const image = fixture()
  const pending = prepareOpeningImage(asset, new AbortController().signal, 100, () => image)
  assert.equal(image.src, asset.url)
  image.onload()
  assert.equal(await pending, asset)
  assert.equal(await prepareOpeningImage(asset, new AbortController().signal, 100,
    () => ({ ...fixture(), complete: true, naturalWidth: 1536 })), asset)
})

test('missing, failed and timed-out art remain absent even when the image arrives late', async () => {
  const signal = new AbortController().signal
  assert.equal(await prepareOpeningImage(null, signal), null)
  const failed = fixture()
  const error = prepareOpeningImage(asset, signal, 100, () => failed)
  failed.onerror()
  assert.equal(await error, null)
  const slow = fixture()
  const pending = prepareOpeningImage(asset, signal, 5, () => slow)
  const lateLoad = slow.onload
  assert.equal(await pending, null)
  lateLoad()
  assert.equal(await pending, null)
  assert.equal(slow.onload, null)
})

test('leaving the opening cancels image preparation before any generation starts', async () => {
  const controller = new AbortController()
  const pending = prepareOpeningImage(asset, controller.signal, 1000, fixture)
  controller.abort()
  assert.equal(await pending, null)
  assert.equal(await prepareOpeningImage(asset, controller.signal, 1000,
    () => { throw new Error('must not request an image') }), null)
})
