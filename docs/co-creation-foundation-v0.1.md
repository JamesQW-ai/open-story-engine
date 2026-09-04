# 私有共创基础 v0.1

## 目的

本阶段为“从原著节点进入，围绕用户选择持续生成新的剧情树”建立持久化且可审计的骨架。它不改写原著 `StoryPackage`，也不允许规划器直接改变 `GameState`。

```text
StoryPackage + 进入节点 + 继承范围 + 角色
  -> SessionStoryContract
  -> source_entry BranchNode
  -> 选择剧情方向
  -> 未偏离且选择规范方向：复用对应原文的 canonical BranchNode
  -> 已偏离或自定义方向：generated BranchNode
  -> 下一批剧情方向
```

## `SessionStoryContract`

每个共创会话有且只有一个独立契约，保存于 `session_story_contracts`：

- `sourcePackageRef`：固定原著包 ID 与版本。
- `entryNodeId`：从哪一个原著节点开始共创。
- `canonicalPrefixNodeIds`：从规范原著起点走到进入节点的不可变前史。
- `canonicalTimelineRefs`、`immutableFactRefs`：本局始终继承的时间线和世界事实。
- `continuityScope`、`persona`、`direction`：继承范围、玩家角色和高层故事目标。
- `provenance`：每个契约自身如何来自原著或用户设定。

初版默认从 `node_arrival` 进入，并代入原著角色许川；schema 已支持 `new_character`，但尚未提供角色创建流程。

## `BranchNode`

`branch_nodes` 记录一棵追加式树，而不是覆盖一条当前剧情线。源入口优先显示对应 `NarrativeBeat.sourceExcerpt.text` 的原文。规范路线的 Beat 节选必须按原稿行号连续覆盖，前一段的 `lineRange` 结束行之后必须立即衔接下一段起始行，不能留下玩家未见的原文空洞。若祖先链始终为 `on_line`，且玩家选择带有 `canonicalBeatId` 的规范方向，服务直接复用该目标 Beat 的原文节选，不调用 Planner；其下一批方向继续携带规范映射。只要任一祖先节点已经偏离，后续即使选择了接近原著目标的方向，也必须由 Planner 依据该分支祖先链生成连续新正文，不能回填或拼接原著段落。

节点保存：

- `parentId` 与全局递增 `sequence`，用于回放和重建树关系。
- 本次选择的 `selectedDirectionId` 与原始玩家方向文本。
- 仅属于本节点的剧情正文、摘要、事实增量、未解线索与下一批方向。
- 可选 `storyArc`：当前分支的宏观目标、正在推进的阶段、目标变更原因与章节标题/收束状态。它是叙事连续性元数据，不是 `BranchState`，不能改变地点、人物、证据或结局条件。
- `canonicalRelation`：`on_line`、`diverged` 或由故事包兼容条件确认的 `rejoined`。

## `BranchState`

每个 `BranchNode` 都保存完整的 `BranchState`，作为共创分支的唯一运行时事实快照。当前最小字段为：许川、唐栖和姜序的位置，唐栖状态，十七号柜铜牌、证据、积水、信号室和列车状态。

方向必须声明非空 `statePatch`。服务在调用 Planner 前以父节点状态和该补丁计算下一状态，并验证地点存在、人物位置与原著场景一致，以及唐栖、证据、水位、信号室和列车状态不会倒退。规范方向还必须与目标原著 Beat 的状态快照完全一致。

Planner 不输出或决定 `BranchState`。它只获得已确认的 `resolvedState` 来叙述本回合，并为下一批高层方向声明候选 `statePatch`。下一回合只有在玩家选择该方向后，服务端才验证和应用该补丁。这样，模型不能凭正文让唐栖获救、让证据出现、让列车重新进站或离站。

仓储层拒绝跨会话父节点、非契约进入节点的根节点，以及不属于父节点 `nextDirections` 的子节点。因此 Planner 无法静默改写来源或跳过玩家已见方向。

## `NarrativePlan`

从第二阶段起，服务在方向已选定、路线已解析、状态补丁已确认之后，构建并持久化一个 `NarrativePlan`。它不新增状态字段，也不替代玩家的下一次选择；它把一次高层方向限定为 2 至 3 个可审阅的叙事节拍：

1. `establish_constraint`：承接当前场景和有限的未解线索。
2. `transition_scene`：仅在 `sceneRoutes` 切换原著场景时出现。
3. `advance_direction`：落实本次已经选择的方向及其已确认 `statePatch`。

计划保存方向 ID、源/目标场景节点、被选择的状态补丁和每个节拍的摘要。它由 `CoCreationService` 生成并随子 `BranchNode` 一同追加，Planner 的 JSON schema 不包含此字段；即使模型返回同名字段，也会在服务端解析时被丢弃。LLM 只可按计划顺序组织新增正文，不能创造、删除、重排节拍或把后续方向的状态提前写成当前结果。

## 受控汇合

故事包可在 `rejoinTargets` 中声明可汇合检查点。每个检查点固定来源场景、目标叙事锚点和必要未解线索；目标锚点的完整 `BranchState` 是额外兼容条件。方向只有携带已声明的 `rejoinTargetId` 才能请求汇合。

服务在玩家真正选择该方向后，依次验证：父场景与必要线索、`sceneRoutes` 解析出的目标场景、状态补丁后的完整状态，以及祖先链中确实存在 `diverged`。全部通过才把新节点标为 `rejoined`。Planner 的 `canonicalRelation` 不具有决定权，普通动态结果统一由服务标记为 `diverged`。

`rejoined` 是兼容检查点，不是原著节选拼接许可。汇合节点与其后代仍由 Planner 生成连续正文，避免跳过玩家未见的原著段落；原文复用仍只适用于祖先链全为 `on_line` 的规范路径。

## Planner 边界

当前 `MockBranchPlanner` 只为《雨夜候车室》提供固定测试内容，用于验证树的生长和分叉。它的输入是：

- `ContextBuilder` 从不可变 `SessionStoryContract`、父节点祖先链、当前原著节点局部窗口和叙事约束组装出的只读上下文；
- 玩家选择的方向 ID；
- 可选的玩家方向原文。

它输出 `PlannerResult`：剧情正文、摘要、事实增量、后续方向、与原著的关系，以及规划引用、置信度和状态变更建议。状态建议只用于审计和后续规则映射；Planner 不得直接修改 `GameState`。SQLite 在确认父子关系后才会赋予节点 ID、时间和顺序。

`ContextBuilder` 只携带本回合需要的原著不可变事实、规范前史、当前父节点的祖先链、当前节点场景、已确认的 `BranchState` 和相关实体；不会把整棵分支树、完整原著或其他会话数据交给 Planner。若方向的 `statePatch.playerLocationId` 改变许川地点，服务层只依据故事包中 `sceneRoutes` 声明的“当前节点 + 当前地点 -> 目标地点 -> 目标节点”路线切换下一回合场景窗口；路线不存在时拒绝该方向。Planner 不输出或决定 `sourceNodeRef`。`PlannerResult` 的每个引用必须位于这份上下文的 `availableReferences` 中，否则拒绝写入。

`LlmBranchPlanner` 已通过 `LlmGateway` 接入 OpenAI-compatible `/chat/completions`，但 CLI 默认仍为 Mock。`npm run co-create` 会使用 Node 的 `--env-file-if-exists=.env` 自动读取本地配置；只有 `.env` 设置 `STORY_PLANNER=openai`、`STORY_LLM_BASE_URL`、`STORY_LLM_API_KEY` 和 `STORY_LLM_MODEL` 后才会发起外部调用。`.env` 被 Git 忽略，`.env.example` 只保存无密钥模板。网关请求 SSE 流，兼容常规 `delta.content` 与个别中转站在 SSE 事件中直接给出的 `message.content`；若中转站只返回普通响应也会兼容处理。服务会把已构建的 `NarrativePlan` 连同受限上下文交给模型，CLI 只会在模型 JSON 经 schema、引用范围、叙事事实与技术安全上限校验、分支落库后逐段呈现正文；不再以固定文学字数拒绝可用章节。`storyArc` 让模型持续看见当前宏观目标，并要求它只展开可交互的当前阶段，待自然收束时才标记“本章完”。每个后续方向的 `statePatch` 还必须至少推进一项已确认状态，防止正文已经完成某个行动却把同一行动再次交给玩家选择。叙事事实校验会拒绝正文提前宣称信号室已穿过、证据已取得、唐栖已离开隧道、水位已下降或列车已移动，并把明确原因用于一次修复重试；它是对 `BranchState` 可表达事实的确定性补充，不试图替代通用语义理解。规范方向的原文复用不调用外部模型。模型 JSON 不完整、超过技术安全上限或 schema 校验失败时，Planner 会用更严格的紧凑输出约束重试一次；两次失败仍不会创建分支。调用请求摘要、原始模型输出或错误保存到 `llm_audits`，密钥不写入数据库或审计。

## 自由文本方向判定

`DirectionEvaluator` 只负责把玩家的自然语言意图锚定到父节点已经公布的高层方向，不能创建方向、状态补丁、场景路线或叙事正文。对于“先 X，再 Y”的多阶段目标，它选择能落实 X 的最早合法方向，并保留完整输入供 Planner 逐章推进；只有当前行动确实无法判定时才要求澄清。`MockDirectionEvaluator` 以关键词覆盖离线试玩；当 `STORY_PLANNER=openai` 时，CLI 使用同一受限上下文和 `LlmGateway` 创建 `LlmDirectionEvaluator`。后者只可返回 `accepted`、`clarification_needed` 或 `rejected` 三类结果：接受结果的 `directionId` 必须属于父节点的 `nextDirections`，拒绝结果的不可变事实引用必须在当前上下文中。

模型 JSON、引用或方向 ID 不合格时，评估器会携带失败原因重试一次；仍失败则降级为澄清提示，不创建分支。服务层会再次校验评估结果后才调用 Planner，因此模型不能借自由文本绕过内容包、地点路由、`RuleEngine` 约束或受控汇合检查。

每次自由文本方向判定都会写入 `direction_evaluations`，无论结果是接受、澄清还是拒绝。记录保存原始输入、父分支、结构化判定和时间，不修改 `GameState`。LLM 判定的受限请求摘要、原始输出和错误另存入 `direction_evaluator_audits`；`audits` 查看判定历史，`llm-audits` 同时显示规划与方向判定调用。

调用方可为 `continue` 或 `continueWithPlayerDirection` 提供 `requestId`。同一会话内该 ID 会绑定到唯一的自由文本判定和生成子节点；重复提交返回既有判定或分支节点，不重新调用模型、不重复追加剧情。若同一 ID 被用于不同父节点、输入或方向，服务会明确拒绝。

## 开发试玩

运行：

```bash
npm run co-create
```

该命令创建一个独立会话，显示当前 Planner 的方向编号；也可输入自然语言方向，`history` 可查看本次生成的分支节点。默认配置使用 Mock 验证数据流；设置 `STORY_PLANNER=openai` 后，方向评估与动态正文都会通过受限 LLM 路径运行。它仍不是正式玩家界面，也不开放模型自行创造状态、场景路线或剧情方向。
