import type { BranchNode, StoryArc } from "./branch-node.js";
import type { BranchPlanRequest, BranchPlanner, PlannerExecution } from "./branch-planner.js";
import type { BranchStatePatch } from "./branch-state.js";
import { narrativePlanSchema } from "./narrative-plan.js";
import { assertPlannerResultFitsContext, plannerResultSchema, type PlannerResult } from "./planner-result.js";

export type PlannedBranchNode = Omit<BranchNode, "id" | "kind" | "parentId" | "createdAt" | "branchState">;

type Template = Omit<PlannerResult, "nextDirections" | "storyArc"> & {
  nextDirections: Array<Omit<PlannerResult["nextDirections"][number], "statePatch">>;
};

const templates: Record<string, Template> = {
  direction_find_token: {
    narrativeText: "许川把唐栖语音里反复出现的十七号柜当作第一条可追的线索。旧铜牌落进掌心时，他意识到站务室和姜序都在等他作出取舍。",
    summary: "许川取得十七号柜铜牌，站务室取证与隧道救援成为两条不同的后续方向。",
    factDeltas: [{ id: "fact_locker_token_selected", source: "derived", summary: "许川决定先追查唐栖留下的十七号柜线索。" }],
    openThreads: ["站务室中的录音", "姜序掌握的维修通道", "唐栖的下落"],
    nextDirections: [
      { id: "direction_secure_evidence", title: "先取得证据", summary: "进入站务室，查清唐栖留下的录音与调度记录。" },
      { id: "direction_rescue_first", title: "救援优先", summary: "暂时放下记录，请姜序从维修通道带路去找唐栖。" },
    ],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_evidence_exists", rationale: "录音与原始记录是世界固定事实。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_secure_evidence: {
    narrativeText: "许川没有把铜牌交给陈砚。他绕进站务室，调度终端的冷光照出被人刻意改过的记录；唐栖留下的声音正从十七号柜里等着被确认。",
    summary: "许川优先进入站务室取证，仍沿原著规范线推进。",
    factDeltas: [{ id: "fact_evidence_priority", source: "user", summary: "许川选择先取得证据，再进入隧道救援。" }],
    openThreads: ["录音与调度记录", "唐栖的确切位置", "陈砚的隐瞒"],
    nextDirections: [{ id: "direction_verify_records", title: "核实失联线索", summary: "翻查录音与调度记录，确认唐栖所在位置。" }],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "canonical_node", ref: "node_arrival", rationale: "站务室取证是进入节点已公布的规范方向。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_rescue_first: {
    narrativeText: "姜序听完许川的决定，只说信号室等不起。他们把站务室里的记录留在身后，穿过雨水灌进来的检修口，先去寻找唐栖。",
    summary: "许川将救援置于取证之前，动态分支开始偏离原著规范线。",
    factDeltas: [{ id: "fact_rescue_priority", source: "user", summary: "许川选择先救唐栖，尚未取得站务室中的证据。" }],
    openThreads: ["降低隧道水位", "打开信号室", "未取得的证据"],
    nextDirections: [
      { id: "direction_lower_water_without_proof", title: "排开积水", summary: "先打开排水通路，在水位继续上涨前靠近信号室。" },
      { id: "direction_return_for_records", title: "折返取证", summary: "趁水位尚未恶化，返回站务室保全录音与维修图纸，再重新组织救援。", rejoinTargetId: "rejoin_records_before_rescue" },
    ],
    canonicalRelation: "diverged",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "隧道积水使救援优先具有即时合理性。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_verify_records: {
    narrativeText: "录音里夹着水声与急促的呼吸，调度记录的改动时间却比唐栖失联更早。许川终于确认，信号室不是猜测，而是必须立刻赶去的地方。",
    summary: "唐栖的位置和陈砚篡改记录的事实得到确认。",
    factDeltas: [{ id: "fact_tang_location_confirmed", source: "derived", summary: "唐栖被困在信号室的事实已确认。" }],
    openThreads: ["进入隧道", "救出唐栖", "保全录音"],
    nextDirections: [{ id: "direction_enter_tunnel_with_proof", title: "带着证据进入隧道", summary: "请姜序带路，在救援时保住能够揭露真相的记录。" }],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_chen_tampered_records", rationale: "篡改记录是核实失联线索的原著依据。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_enter_tunnel_with_proof: {
    narrativeText: "姜序看见录音笔后没有再问。许川把证据贴身收好，跟着他踏进维修隧道；积水已经漫过靴面，信号室方向传来闷响。",
    summary: "许川带着证据进入隧道，救援与保全真相同时成为分支约束。",
    factDeltas: [{ id: "fact_enter_tunnel_with_proof", source: "derived", summary: "许川带着录音进入维修隧道。" }],
    openThreads: ["降低水位", "打开信号室", "安全带回证据"],
    nextDirections: [{ id: "direction_lower_water_with_proof", title: "排开积水", summary: "先打开排水通路，为进入信号室争取时间。" }],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "隧道进水约束救援的推进方式。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_lower_water_with_proof: {
    narrativeText: "阀门在两人手下转动，退去的水流给信号室留下一线余地。许川听见里面的撞击声，录音笔仍贴在胸前，没有被这场救援甩在身后。",
    summary: "排水通路打开，许川带着证据接近信号室。",
    factDeltas: [{ id: "fact_water_lowered_with_proof", source: "derived", summary: "隧道水位暂时下降，证据仍由许川保管。" }],
    openThreads: ["打开信号室", "唐栖的安危", "保存证据"],
    nextDirections: [{ id: "direction_open_signal_room_with_proof", title: "打开信号室", summary: "趁水位退去，设法拉开卡住的滑栓救出唐栖。" }],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "水位必须下降后才能接近信号室。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_lower_water_without_proof: {
    narrativeText: "没有录音，也没有图纸，许川只能按姜序的指引拧开手动阀。积水终于不再上涨，可站务室里那份能改变一切的记录仍留在陈砚能够触及的地方。",
    summary: "排水通路打开，但救援优先分支仍未取得证据。",
    factDeltas: [{ id: "fact_water_lowered_without_proof", source: "derived", summary: "隧道水位暂时下降，站务室证据仍未取得。" }],
    openThreads: ["打开信号室", "唐栖的安危", "未取得的证据"],
    nextDirections: [{ id: "direction_open_signal_room_without_proof", title: "打开信号室", summary: "趁水位退去，优先救出被困在信号室的唐栖。" }],
    canonicalRelation: "diverged",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "水位威胁决定救援分支仍须先排水。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_return_for_records: {
    narrativeText: "许川没有把已经听见的撞击声当成可以忽略的背景。他和姜序确认信号室暂时还能支撑，随即从检修口折回站务室；十七号柜里的录音和维修图纸仍在，陈砚却已开始试图抹去终端上的痕迹。许川带走证据，也带回了唐栖被困的确切消息，救援不再只能依靠猜测。",
    summary: "许川折返站务室保全证据，再以已确认的线索重新组织救援。",
    factDeltas: [{ id: "fact_evidence_recovered_after_detour", source: "derived", summary: "许川在折返后保全录音与维修图纸，唐栖位置得到确认。" }],
    openThreads: ["进入隧道", "救出唐栖", "保全录音"],
    nextDirections: [{ id: "direction_enter_tunnel_with_proof", title: "带着证据进入隧道", summary: "请姜序带路，在救援时保住能够揭露真相的记录。" }],
    canonicalRelation: "diverged",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_evidence_exists", rationale: "录音与原始记录仍可在站务室取得。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_open_signal_room_with_proof: {
    narrativeText: "滑栓终于松开，唐栖在姜序的帮助下离开信号室。许川把录音笔和原始记录收好，三人回到候车厅时，列车司机仍在等陈砚的放行复诵。",
    summary: "唐栖获救，证据仍在许川手中；必须在列车放行前公开真相。",
    factDeltas: [{ id: "fact_tang_rescued_with_proof", source: "derived", summary: "唐栖获救，录音和原始记录仍被保全。" }],
    openThreads: ["阻止列车放行", "陈砚的责任", "公开证据"],
    nextDirections: [{ id: "direction_hold_train", title: "公开真相", summary: "向列车司机说明录音和救援事实，阻止末班列车放行。" }],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_evidence_exists", rationale: "获救后的公开说明仍以录音和原始记录为依据。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_open_signal_room_without_proof: {
    narrativeText: "许川拉开卡住的滑栓，和姜序一起把唐栖带回候车厅。人已经获救，但站务室里的录音和原始记录没有随他们回来；列车的灯光映在雨里，真相暂时只能留在三人之间。",
    summary: "唐栖获救，但站务室中的证据尚未取得，救援优先分支在此收束。",
    factDeltas: [{ id: "fact_tang_rescued_without_proof", source: "derived", summary: "唐栖获救，但录音和原始记录仍未取得。" }],
    openThreads: ["未取得的证据", "陈砚的责任"],
    nextDirections: [],
    canonicalRelation: "diverged",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_tunnel_flooding", rationale: "水位威胁使先救出唐栖成为该分支的合理收束。" }], confidence: "high", stateChangeProposals: [] },
  },
  direction_hold_train: {
    narrativeText: "司机听完录音，没有接受陈砚的放行指令。末班列车仍停在雨里，唐栖的证词和原始记录都留在候车厅的灯下，陈砚再也无法把这一夜从记录里抹去。",
    summary: "唐栖获救，证据公开，列车没有放行；规范救援与真相线完成。",
    factDeltas: [{ id: "fact_train_held_with_evidence", source: "derived", summary: "司机拒绝放行列车，陈砚无法继续掩盖事实。" }],
    openThreads: [],
    nextDirections: [],
    canonicalRelation: "on_line",
    planning: { citations: [{ kind: "immutable_fact", ref: "fact_evidence_exists", rationale: "司机拒绝放行的依据是已保全的录音和原始记录。" }], confidence: "high", stateChangeProposals: [] },
  },
};

const directionStatePatches: Record<string, BranchStatePatch> = {
  direction_secure_evidence: { playerLocationId: "location_station_office" },
  direction_rescue_first: { playerLocationId: "location_signal_tunnel", jiangLocationId: "location_signal_tunnel", tangStatus: "located" },
  direction_verify_records: { evidenceStatus: "secured", tangStatus: "located" },
  direction_enter_tunnel_with_proof: { playerLocationId: "location_signal_tunnel", jiangLocationId: "location_signal_tunnel" },
  direction_lower_water_with_proof: { waterLevel: "lowered" },
  direction_lower_water_without_proof: { waterLevel: "lowered" },
  direction_return_for_records: { playerLocationId: "location_station_office", jiangLocationId: "location_waiting_hall", evidenceStatus: "secured" },
  direction_open_signal_room_with_proof: { playerLocationId: "location_waiting_hall", jiangLocationId: "location_waiting_hall", tangLocationId: "location_waiting_hall", tangStatus: "rescued", signalRoomStatus: "opened" },
  direction_open_signal_room_without_proof: { playerLocationId: "location_waiting_hall", jiangLocationId: "location_waiting_hall", tangLocationId: "location_waiting_hall", tangStatus: "rescued", signalRoomStatus: "opened" },
  direction_hold_train: { trainStatus: "held" },
};

export class MockBranchPlanner implements BranchPlanner {
  async plan(request: BranchPlanRequest): Promise<PlannerExecution> {
    if (!request.context.parent.nextDirections.some((direction) => direction.id === request.selectedDirectionId)) {
      throw new Error(`当前分支不存在可选方向: ${request.selectedDirectionId}`);
    }
    const narrativePlan = narrativePlanSchema.parse(request.narrativePlan);
    if (narrativePlan.directionId !== request.selectedDirectionId || narrativePlan.targetNodeRef !== request.sourceNodeRef) {
      throw new Error("Mock Planner 收到的叙事计划与已选方向或场景窗口不一致");
    }
    const template = templates[request.selectedDirectionId];
    if (!template) throw new Error(`Mock Planner 尚未配置方向: ${request.selectedDirectionId}`);
    const result = {
      ...structuredClone(template),
      storyArc: createStoryArc(request, template),
      nextDirections: template.nextDirections.map((direction) => ({
        ...direction,
        statePatch: getDirectionStatePatch(direction.id),
      })),
    };
    return { kind: "completed", result: assertPlannerResultFitsContext(plannerResultSchema.parse(result), request.context, request.resolvedState) };
  }
}

function createStoryArc(request: BranchPlanRequest, template: Template): StoryArc {
  const parentArc = request.context.parent.storyArc;
  const playerGoal = request.playerDirection?.trim();
  const explicitPlayerGoal = playerGoal && !playerGoal.startsWith("选择方向：") ? playerGoal : undefined;
  const goalDisposition: StoryArc["goalDisposition"] = !parentArc
    ? "started"
    : explicitPlayerGoal && explicitPlayerGoal !== parentArc.activeGoal
      ? "replaced"
      : template.nextDirections.length === 0
        ? "completed"
        : "continued";
  const title = goalDisposition === "replaced" || parentArc?.chapter.status !== "continuing"
    ? `雨夜候车室·${request.context.parent.nextDirections.find((direction) => direction.id === request.selectedDirectionId)?.title ?? "新阶段"}`
    : parentArc.chapter.title;

  return {
    activeGoal: explicitPlayerGoal ?? parentArc?.activeGoal ?? request.narrativePlan.goal,
    currentPhase: template.summary,
    goalDisposition,
    chapter: { title, status: template.nextDirections.length === 0 ? "complete" : "continuing" },
  };
}

function getDirectionStatePatch(directionId: string): BranchStatePatch {
  const patch = directionStatePatches[directionId];
  if (!patch) throw new Error(`Mock Planner 缺少方向状态补丁: ${directionId}`);
  return patch;
}
