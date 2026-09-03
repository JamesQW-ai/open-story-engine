import type { BranchState } from "./branch-state.js";

export type NarrativeFactViolation = {
  code: string;
  message: string;
};

type Guard = {
  code: string;
  when: (state: BranchState) => boolean;
  expression: RegExp;
  message: string;
};

const guards: Guard[] = [
  {
    code: "signal_room_locked_crossed",
    when: (state) => state.signalRoomStatus === "locked",
    expression: /(?:进入|走进|踏入|钻进|挤进).{0,24}(?:信号室|隔间)|(?:挤过|钻过|穿过).{0,40}(?:门缝|铁门|门).{0,80}(?:信号室|隔间)/u,
    message: "信号室仍锁闭，正文不能描写角色已经穿过门进入信号室或隔间。",
  },
  {
    code: "evidence_unsecured_claimed",
    when: (state) => state.evidenceStatus === "unsecured",
    expression: /(?:许川|他).{0,60}(?:(?:拿到|取得|收好|贴身收好|带着|握着).{0,35}(?:录音笔|原始(?:文件|记录)|证据)|(?:录音笔|原始(?:文件|记录)|证据).{0,16}(?:拿到|取得|收好|贴身收好|带着|握着))/u,
    message: "证据仍未取得，正文不能描写许川已经持有录音笔、原始文件或证据。",
  },
  {
    code: "tang_not_rescued_departed",
    when: (state) => state.tangStatus !== "rescued",
    expression: /(?:唐栖|她).{0,32}(?:被救出|已经获救|回到候车厅|离开(?:了)?(?:信号室|隧道))|(?:许川|姜序|两人).{0,32}(?:带着|扶着|背着).{0,20}(?:唐栖|她).{0,32}(?:回到|离开).{0,20}(?:候车厅|隧道|信号室)/u,
    message: "唐栖尚未获救，正文不能描写她已离开信号维修隧道或回到候车厅。",
  },
  {
    code: "water_still_rising_lowered",
    when: (state) => state.waterLevel === "rising",
    expression: /(?:水位|积水).{0,16}(?:已经|开始|正在|缓慢地|明显地|肉眼可见地)?(?:下降|降低|退去|不再上涨)/u,
    message: "水位仍在上涨，正文不能描写积水已经下降、退去或停止上涨。",
  },
  {
    code: "train_pending_moved",
    when: (state) => state.trainStatus === "pending_release",
    expression: /(?:末班列车|列车).{0,32}(?:驶离|离站|发车|驶出|进站|驶入)/u,
    message: "列车仍在等待放行，正文不能描写列车已经进站、离站或发车。",
  },
];

export function findNarrativeFactViolations(narrativeText: string, state: BranchState): NarrativeFactViolation[] {
  return guards
    .filter((guard) => guard.when(state) && guard.expression.test(narrativeText))
    .map(({ code, message }) => ({ code, message }));
}

export function assertNarrativeMatchesBranchState(narrativeText: string, state: BranchState): void {
  const violations = findNarrativeFactViolations(narrativeText, state);
  if (violations.length > 0) {
    throw new Error(`剧情正文与已确认状态矛盾：${violations.map((violation) => violation.message).join("；")}`);
  }
}
