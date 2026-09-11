import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../src/api/client.ts'

function source(frames, splitBytes=1) {
  const bytes = new TextEncoder().encode(frames)
  return new Response(new ReadableStream({start(controller) {
    for (let i=0;i<bytes.length;i+=splitBytes) controller.enqueue(bytes.slice(i,i+splitBytes))
    controller.close()
  }}),{headers:{'Content-Type':'text/event-stream'}})
}

test('stream parser preserves split UTF-8, frame boundaries, reset and final result', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source(': heartbeat\n\nevent: delta\ndata: {"text":"雨夜"}\n\nevent: reset\ndata: {}\n\nevent: delta\ndata: {"text":"新的正文"}\n\nevent: done\ndata: {"status":"written","branch":{"id":"saved"}}\n\n'))
  let text=''; const calls=[]
  const result = await api.streamTurn('test',{},chunk=>{text+=chunk;calls.push(chunk)},()=>{text=''})
  assert.deepEqual(calls,['雨夜','新的正文'])
  assert.equal(text,'新的正文')
  assert.equal(result.branch.id,'saved')
})

test('a closed stream without done is a failure, not a saved chapter', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: delta\ndata: {"text":"未完成"}\n\n'))
  await assert.rejects(api.streamTurn('test',{},()=>{},()=>{}), e=>e.code==='stream_interrupted')
})

test('server error is surfaced without treating preview as saved', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: error\ndata: {"code":"generation_failed","status":503,"message":"未完成"}\n\n'))
  await assert.rejects(api.streamTurn('test',{},()=>{},()=>{}), e=>e.code==='generation_failed')
})
