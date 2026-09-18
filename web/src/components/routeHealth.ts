import type { RouteHealthSignal } from '../api/types'

const LABELS: Record<Exclude<RouteHealthSignal, 'source_progress_stalled'>, string> = {
  unchanged_tracked_state: '连续回合没有可追踪的状态变化。',
  repeated_action: '连续回合重复了相同行动。',
  repeated_body: '最近正文与此前回合重复。',
  source_progress_regressed: '原著路线进度出现回退，需要复核当前分支。',
}

export function routeHealthMessages(signals: RouteHealthSignal[], sourceProgressStreak: number | null): string[] {
  return signals.map(signal => signal === 'source_progress_stalled'
    ? (sourceProgressStreak === null
      ? '原著路线进度未确认，无法判断是否推进。'
      : `原著路线已连续 ${sourceProgressStreak} 回合没有推进。`)
    : LABELS[signal])
}
