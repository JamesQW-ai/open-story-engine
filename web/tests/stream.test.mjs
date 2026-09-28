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

test('only the complete confirmed branch is displayed, ignoring deltas and resets', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source(': heartbeat\n\nevent: delta\ndata: {"text":"雨夜"}\n\nevent: reset\ndata: {}\n\nevent: delta\ndata: {"text":"新的正文"}\n\nevent: done\ndata: {"status":"written","branch":{"id":"saved","narrativeText":"确认后的完整正文"}}\n\n'))
  let text=''; const calls=[]
  const result = await api.streamTurn('test',{},chunk=>{text+=chunk;calls.push(chunk)},()=>assert.fail('must not reset visible prose'))
  assert.deepEqual(calls,['确认后的完整正文'])
  assert.equal(text,'确认后的完整正文')
  assert.equal(result.branch.id,'saved')
})

test('a closed stream without done is a failure, not a saved chapter', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: delta\ndata: {"text":"未完成"}\n\n'))
  await assert.rejects(api.streamTurn('test',{},()=>assert.fail('partial prose leaked'),()=>assert.fail('reset leaked')), e=>e.code==='stream_interrupted')
})

test('server error is surfaced without treating preview as saved', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: delta\ndata: {"text":"未确认正文"}\n\nevent: error\ndata: {"code":"generation_failed","status":503,"message":"未完成"}\n\n'))
  await assert.rejects(api.streamTurn('test',{},()=>assert.fail('failed prose leaked'),()=>{}), e=>e.code==='generation_failed')
})

test('opening also publishes exactly one complete confirmed body', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: delta\ndata: {"text":"部分开场"}\n\nevent: done\ndata: {"session":{"id":"s"},"branch":{"id":"b","narrativeText":"完整开场"}}\n\n'))
  const texts=[]
  await api.streamOpening({},text=>texts.push(text),()=>assert.fail('reset leaked'))
  assert.deepEqual(texts,['完整开场'])
})

test('missing confirmed body and rejected turns never publish preview text', async (t) => {
  t.mock.method(globalThis,'fetch',async()=>source('event: done\ndata: {"status":"written","branch":{"id":"saved"}}\n\n'))
  await assert.rejects(api.streamTurn('test',{},()=>assert.fail('invalid confirmation leaked'),()=>{}),e=>e.code==='invalid_confirmation')
  t.mock.method(globalThis,'fetch',async()=>source('event: delta\ndata: {"text":"未确认"}\n\nevent: done\ndata: {"status":"rejected"}\n\n'))
  const result=await api.streamTurn('test',{},()=>assert.fail('rejected prose leaked'),()=>{})
  assert.equal(result.status,'rejected')
})

test('lost turn receipt automatically recovers with the same request and displays once', async (t) => {
  const requests=[]; const displayed=[]
  t.mock.method(globalThis,'fetch',async(url,init)=>{
    requests.push({url,body:JSON.parse(init.body)})
    if(requests.length===1) return source('event: delta\ndata: {"text":"尚无确认的片段"}\n\n')
    return source('event: done\ndata: {"status":"written","deduplicated":true,"branch":{"id":"same-branch","narrativeText":"已入账的完整正文"}}\n\n')
  })
  const payload={request_id:'stable-key',parent_branch_id:'parent',text:'询问眼前的人'}
  const result=await api.streamTurn('s',payload,text=>displayed.push(text),()=>assert.fail('must not reset'))
  assert.equal(requests.length,2)
  assert.deepEqual(requests[0],requests[1])
  assert.deepEqual(requests[1].body,payload)
  assert.equal(result.branch.id,'same-branch')
  assert.deepEqual(displayed,['已入账的完整正文'])
})

test('opening network failure recovers once using its original idempotency key', async (t) => {
  const requests=[]; const displayed=[]
  t.mock.method(globalThis,'fetch',async(url,init)=>{
    requests.push({url,body:JSON.parse(init.body)})
    if(requests.length===1) throw new TypeError('Failed to fetch')
    return source('event: done\ndata: {"session":{"id":"s"},"branch":{"id":"opening","narrativeText":"正式开局"}}\n\n')
  })
  await api.streamOpening({request_id:'opening-key'},text=>displayed.push(text),()=>{})
  assert.deepEqual(requests[0],requests[1])
  assert.equal(requests.length,2)
  assert.deepEqual(displayed,['正式开局'])
})

test('read failure recovers but persistent disconnect stops after two requests', async (t) => {
  let calls=0
  t.mock.method(globalThis,'fetch',async()=>{
    calls++
    return new Response(new ReadableStream({start(controller){controller.error(new TypeError('connection lost'))}}))
  })
  await assert.rejects(api.streamTurn('s',{request_id:'key'},()=>assert.fail('prose leaked'),()=>{}),e=>e.code==='stream_interrupted')
  assert.equal(calls,2)
})

test('a request without a key never retries a network failure', async (t) => {
  let calls=0
  t.mock.method(globalThis,'fetch',async()=>{calls++;throw new TypeError('network')})
  await assert.rejects(api.streamOpening({},()=>{},()=>{}),e=>e.code==='network_error')
  assert.equal(calls,1)
})

test('generation rejection and malformed confirmation are not retried', async (t) => {
  for(const [frame,code] of [
    ['event: error\ndata: {"code":"generation_failed","status":503}\n\n','generation_failed'],
    ['event: done\ndata: {"status":"written","branch":{"id":"b"}}\n\n','invalid_confirmation'],
  ]) {
    let calls=0
    t.mock.method(globalThis,'fetch',async()=>{calls++;return source(frame)})
    await assert.rejects(api.streamTurn('s',{request_id:'key'},()=>assert.fail('prose leaked'),()=>{}),e=>e.code===code)
    assert.equal(calls,1)
  }
})

test('cancelling the request prevents recovery and publishing', async (t) => {
  const controller=new AbortController(); let calls=0
  t.mock.method(globalThis,'fetch',async()=>{calls++;controller.abort();return source('')})
  await assert.rejects(api.streamTurn('s',{request_id:'key'},()=>assert.fail('prose leaked'),()=>{},controller.signal),e=>e.name==='AbortError')
  assert.equal(calls,1)
  await assert.rejects(api.streamTurn('s',{request_id:'another'},()=>{},()=>{},controller.signal),e=>e.name==='AbortError')
  assert.equal(calls,1)
})
