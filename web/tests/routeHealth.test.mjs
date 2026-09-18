import test from 'node:test'
import assert from 'node:assert/strict'
import { routeHealthMessages } from '../src/components/routeHealth.ts'

test('route health messages preserve each diagnostic signal', () => {
  assert.deepEqual(routeHealthMessages([
    'unchanged_tracked_state', 'repeated_action', 'repeated_body', 'source_progress_stalled', 'source_progress_regressed',
  ], 3), [
    '连续回合没有可追踪的状态变化。',
    '连续回合重复了相同行动。',
    '最近正文与此前回合重复。',
    '原著路线已连续 3 回合没有推进。',
    '原著路线进度出现回退，需要复核当前分支。',
  ])
})

test('missing source progress stays unknown in the message', () => {
  assert.equal(routeHealthMessages(['source_progress_stalled'], null)[0], '原著路线进度未确认，无法判断是否推进。')
})
