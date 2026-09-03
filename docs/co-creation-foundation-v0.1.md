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
- `canonicalRelation`：`on_line`、`diverged` 或未来由一致性检查确认的 `rejoined`。

## `BranchState`

每个 `BranchNode` 都保存完整的 `BranchState`，作为共创分支的唯一运行时事实快照。当前最小字段为：许川、唐栖和姜序的位置，唐栖状态，十七号柜铜牌、证据、积水、信号室和列车状态。

方向必须声明非空 `statePatch`。服务在调用 Planner 前以父节点状态和该补丁计算下一状态，并验证地点存在、人物位置与原著场景一致，以及唐栖、证据、水位、信号室和列车状态不会倒退。规范方向还必须与目标原著 Beat 的状态快照完全一致。

Planner 不输出或决定 `BranchState`。它只获得已确认的 `resolvedState` 来叙述本回合，并为下一批高层方向声明候选 `statePatch`。下一回合只有在玩家选择该方向后，服务端才验证和应用该补丁。这样，模型不能凭正文让唐栖获救、让证据出现、让列车重新进站或离站。

仓储层拒绝跨会话父节点、非契约进入节点的根节点，以及不属于父节点 `nextDirections` 的子节点。因此 Planner 无法静默改写来源或跳过玩家已见方向。

## Planner 边界

当前 `MockBranchPlanner` 只为《雨夜候车室》提供固定测试内容，用于验证树的生长和分叉。它的输入是：

- `ContextBuilder` 从不可变 `SessionStoryContract`、父节点祖先链、当前原著节点局部窗口和叙事约束组装出的只读上下文；
- 玩家选择的方向 ID；
- 可选的玩家方向原文。

它输出 `PlannerResult`：剧情正文、摘要、事实增量、后续方向、与原著的关系，以及规划引用、置信度和状态变更建议。状态建议只用于审计和后续规则映射；Planner 不得直接修改 `GameState`。SQLite 在确认父子关系后才会赋予节点 ID、时间和顺序。

`ContextBuilder` 只携带本回合需要的原著不可变事实、规范前史、当前父节点的祖先链、当前节点场景、已确认的 `BranchState` 和相关实体；不会把整棵分支树、完整原著或其他会话数据交给 Planner。动态方向可在故事包上声明受控的 `sourceNodeRef`，用来切换下一回合的原著场景窗口；未声明时继承父节点场景。模型返回的 `sourceNodeRef` 不具备权限，服务会以该受控值覆盖它。`PlannerResult` 的每个引用必须位于这份上下文的 `availableReferences` 中，否则拒绝写入。

`LlmBranchPlanner` 已通过 `LlmGateway` 接入 OpenAI-compatible `/chat/completions`，但 CLI 默认仍为 Mock。`npm run co-create` 会使用 Node 的 `--env-file-if-exists=.env` 自动读取本地配置；只有 `.env` 设置 `STORY_PLANNER=openai`、`STORY_LLM_BASE_URL`、`STORY_LLM_API_KEY` 和 `STORY_LLM_MODEL` 后才会发起外部调用。`.env` 被 Git 忽略，`.env.example` 只保存无密钥模板。网关请求 SSE 流，若中转站只返回普通响应则兼容处理；CLI 只会在完整 JSON 经 schema、引用范围、叙事事实和 400-700 字正文长度校验、分支落库后逐段呈现正文，避免把无效半段剧情显示为正式内容。叙事事实校验会拒绝正文提前宣称信号室已穿过、证据已取得、唐栖已离开隧道、水位已下降或列车已移动，并把明确原因用于一次修复重试；它是对 `BranchState` 可表达事实的确定性补充，不试图替代通用语义理解。规范方向的原文复用不调用外部模型。模型 JSON 不完整、长度不合格或 schema 校验失败时，Planner 会用更严格的紧凑输出约束重试一次；两次失败仍不会创建分支。调用请求摘要、原始模型输出或错误保存到 `llm_audits`，密钥不写入数据库或审计。

## 自由文本方向的 Mock 判定

当前 `MockDirectionEvaluator` 已允许 CLI 以一句自然语言描述选择方向，但它不是通用自然语言理解器：只会把文本唯一映射到父节点已经公布的高层方向。模糊、同时命中多个方向或当前无法映射的输入会返回澄清提示；违反原著不可变事实的超自然、瞬移或复活类输入会被拒绝，且不创建分支节点。

因此，该阶段验证的是“自由文本输入 -> 可解释的受限方向选择 -> 分支规划”的完整路径，而不是开放式剧情生成。未来 LLM Evaluator 必须复用相同的结果类型，并将其判断依据限定在 `ContextBuilder` 提供的上下文中。

每次自由文本方向判定都会写入 `direction_evaluations`，无论结果是接受、澄清还是拒绝。记录保存原始输入、父分支、结构化判定和时间，不修改 `GameState`；`audits` 命令可在 CLI 中读取本次会话的判定历史。

## 开发试玩

运行：

```bash
npm run co-create
```

该命令创建一个独立会话，显示 Mock Planner 的方向编号；也可输入自然语言方向，`history` 可查看本次生成的分支节点。它只验证数据流，不是正式玩家界面，也不代表已接入 LLM 或开放式剧情生成。
