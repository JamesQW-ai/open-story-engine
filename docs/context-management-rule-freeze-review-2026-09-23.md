状态：待人工审核；本文件只提交冻结选项，不修改 StoryPackage、状态提交链或 Planner 提示。

历史快照说明（2026-09-27）：本文保留当时的冻结选项与审核记录，不表示后续实施停留在此阶段，也不要求重复授权。当前阶段投影已实施；动态记忆生产闭环仍未完成，详见[最新核查](context-management-offline-review-2026-09-27.md)。

## 审核目的

当前 `ContextBundle` 契约、确定性 Builder 和预算审计已经具备，但三层规则仍不能进入正式提示迁移。下一步需要先确认业务包级 `stateVisibility`、短期记忆过期边界和动态记忆状态迁移的可执行定义。

## 1. 业务包级 `stateVisibility`

### 建议位置

把声明放在每个 StoryPackage 的 `modules/state-schema.json` 顶层，与 `stateModel` 同级：

```json
{
  "schemaVersion": "story-package-state-module/0.1",
  "package": {"id": "...", "version": "..."},
  "stateVisibility": {
    "schemaVersion": "state-visibility/0.2",
    "paths": {
      "/playerCharacterId": "player_known",
      "/playerLocationId": "player_known"
    },
    "pathTemplates": {
      "/characterLocationIds/{playerCharacterId}": "player_known"
    }
  },
  "stateModel": {}
}
```

`state-visibility/0.1` 只支持静态 `paths`；本稿新增 `pathTemplates`，因此正式版本升级为 `state-visibility/0.2`。`stateVisibility.paths` 和 `pathTemplates` 均针对运行时归一化后的 branch state，不直接针对 `initialState`。加载器先依据 `state-schema.json` 和状态提交结果形成归一化状态，再把模板中的 `{playerCharacterId}` 替换为当前状态中的实际值；替换值写入 JSON Pointer 前必须按 RFC 6901 转义 `~` 为 `~0`、`/` 为 `~1`。替换后必须得到 JSON Pointer 叶子路径，未解析的占位符、路径不存在、路径指向对象或数组都受控失败。静态声明或不同模板展开到同一具体路径时，只有 audience 完全一致才允许去重；同一路径出现不同 audience 必须 fail closed。传给 `ContextBundle` 的最终 map 只能包含去重后的具体叶子路径，因此与当前 `ContextBundle.stateVisibility` 校验格式一致。

声明必须同时绑定 package ID 和 version；加载时版本不匹配、值不属于四类 visibility 或缺少必需声明，都应在模型调用前失败。模板只用于需要运行时实体 ID 的字段，不能用通配符放宽整棵对象。

### 状态字段来源映射

业务包声明校验使用归一化状态字段，字段来源按下表解释，避免把原始模块结构和运行时结构混用：

| 归一化字段 | 业务包来源 | 首版投影约束 |
| --- | --- | --- |
| `playerCharacterId` | `initialState.player.characterId` 经会话初始化归一化 | `player_known` |
| `playerLocationId` | `stateModel.locationReferenceFields` 产生的运行时分支字段 | `player_known` |
| `characterLocationIds.<characterId>` | `stateModel.runtimeStateFields` 的运行时实体位置表 | 仅展开当前玩家 ID |
| `sourceProgress` | `stateModel.monotonicEnums.sourceProgress` 对应的分支进度字段 | 默认隐藏，须单独人工确认 |
| `freeTextProgress` | `stateModel.runtimeStateFields` | 默认隐藏 |

因此，`currentLocationId` 等只存在于原始 `initialState`、但没有进入归一化分支状态的字段，不得仅凭原始字段存在就加入可见声明。

### 缺失策略

业务包正式声明前继续允许 `runtime_default_migration`，但只用于只读审计。运行时配置 `contextProjection.stateVisibilityMode` 必须明确投影模式，并把模式写入 bundle 审计：

1. `audit_fallback`：兼容未声明的旧包，使用现有保守白名单，记录 `stateVisibilitySource=runtime_default_migration` 和 `stateVisibilityMode=audit_fallback`，不得写回 StoryPackage。
2. `formal_required`：只接受包级正式声明；缺少声明、版本不匹配或展开失败均在模型调用前受控失败，并记录 `stateVisibilityMode=formal_required`。

该配置默认保持 `audit_fallback`，只有发布闸门明确切换才允许使用 `formal_required`。发布闸门必须同时提供：官方长篇包的 ID/version 清单、每个包的 `state-visibility/0.2` 声明校验结果、正式模式回归结果和 bundle 审计字段核对结果；缺任一项不得切换。正式模式在 bundle 生成前失败时，不伪造 bundle，而是写入独立的 `projectionRejectionAudit`：

```json
{
  "auditType": "state_visibility_projection_rejected",
  "package": {"id": "<id>", "version": "<version>"},
  "mode": "formal_required",
  "sessionId": "<sessionId>",
  "parentBranchId": "<parentBranchId>",
  "requestId": "<requestId>",
  "reason": "path_collision"
}
```

`reason` 每条记录只能取一个值：`missing_declaration`、`version_mismatch`、`unresolved_path` 或 `path_collision`。拒绝记录还应带上 `sessionId` 和 `parentBranchId`，以便与本次请求和分支审计关联。

该拒绝记录与 bundle 分开保存，并且不得被当作模型上下文。fallback 不得自动升级为正式声明。

### 当前官方长篇的审核样例

`taixu-relics-part1@0.1.3` 的原始 state module 同时包含初始化字段、位置引用、运行时字段和单调枚举；它们必须先按上表映射到归一化分支状态。建议首版只允许作者明确声明玩家身份、玩家当前位置和展开后的玩家自身位置；其他字段保持未声明并隐藏，是否将公开地点、已确认人物结果或物品持有关系加入 `public_world_fact`，需由人工按产品规则逐项确认。

## 2. 短期记忆过期规则

建议冻结为以下首版行为：

| 内容 | 生命周期 | 过期后的处理 |
| --- | --- | --- |
| `turnIntent` | 当前请求的 `requestId`（本稿统一称 `turnKey`） | 成功提交或不可重试的终止失败后清除；可重试的提供方失败保留给同一 `requestId`，不复制到下一回合 |
| `continuityWindow` | 当前分支最近 2 个已确认回合 | 从短期记忆移除，转为带来源的历史证据 |
| 父分支短期记忆 | 分支切换时立即失效 | 子分支重新从自身 lineage 计算 |
| 未确认或来源缺失条目 | 立即失效 | 标记 `unknown` 或 `rejected`，不得留在短期窗口 |

`turnKey` 不新增为 `ContextBundle.turnIntent` 的正文域，而是请求层对现有 `requestId` 的统一称呼。正式 API 上下文入口沿用 [`turn-protocol.md`] 的约定，要求调用方提供非空 `requestId`。仅旧入口的兼容适配层允许在进入 Planner 前由服务端生成 requestId；生成后必须立即写入请求记录并在响应中返回，且不得以空值进入 Planner。正式 API、旧入口和重试路径都必须复用同一 ID。重试、provider transport fallback 和语义重试都复用同一 ID；该 ID 必须绑定 `sessionId`、父分支 ID 和玩家输入摘要，绑定不一致时拒绝重试。它只用于请求幂等和生命周期管理，不进入正文提示内容。最近 2 个按已提交且已确认的分支节点计数，不按模型调用次数计数；失败草稿、重试和未提交结果不占用窗口。可重试失败只保留同一 `requestId` 的临时意图，不进入下一回合。当前 Builder 的 2 条上限与此提案一致，但正式装配器仍未实现回合 TTL 和分支切换记录。

连续性条目过期时生成一条只读历史证据，最小结构为：

```json
{
  "sourceId": "continuity:<branchId>:<nodeId>",
  "kind": "confirmed_event",
  "branchId": "<branchId>",
  "nodeId": "<nodeId>",
  "location": "continuityWindow",
  "content": "<经校验的短摘要>",
  "authority": "confirmed_evidence",
  "validity": "confirmed",
  "visibility": "<原条目可见性>",
  "origin": "continuityWindow",
  "expiredAtTurn": 3
}
```

该结构满足当前 `allowedEvidence` 的必需字段；`nodeId`、`origin` 和 `expiredAtTurn` 是连续性审计扩展字段。`expiredAtTurn` 指该条目第一次不再属于最近两个已确认节点时，对应新提交节点的整数 `derivedTurn`，不是模型调用次数、重试次数或墙上时间。证据不再计入 `continuityWindow`，但可按阶段投影规则进入 `allowedEvidence`；其 `sourceId`、分支、节点和有效性必须可追溯，缺任一项则只保留审计记录并标记 `unknown`，不得作为事实喂给模型。

## 3. 动态记忆生命周期

建议冻结以下状态机：

```text
candidate -> confirmed | unknown | rejected
unknown   -> candidate | confirmed | rejected
confirmed -> rejected（必须有权威冲突和 conflictWith）
rejected  -> 无迁移；新证据必须创建新的 memoryId
```

约束如下：

- 模型最多提出 `candidate` 或 `unknown`；`confirmed` 只能由代码校验和审查流水线生成。
- 每次迁移创建新快照，`sequence` 严格递增；旧快照保留在审计记录中。
- `candidate`、`unknown`、`rejected` 不得跨分支继承。
- branch 范围的 `confirmed` 记忆只有在业务明确允许时才能复制；复制必须生成新的 `memoryId`、保留 `parentMemoryId` 和 `inheritanceReason`。
- `confirmed -> rejected` 必须记录否定来源和 `conflictWith`；不能原地覆盖旧记录。`conflictWith` 中的每个 ID 必须能解析到同一包内的既有记忆快照；当前 rejected 快照的 `sourceIds` 至少包含一条 `authority=authoritative` 且 `validity=confirmed` 的否定证据，该证据还必须带 `relation=contradicts` 和覆盖目标 `memoryId` 的 `targetMemoryIds`。当前记忆与目标快照的范围必须兼容：branch 只能指向同一 branch，或显式存在于当前 branch lineage 的已复制快照；`package` 只能指向 `package` 或 `global`，`global` 只能指向 `global`。
- `player_known` 和 `public_world_fact` 仍须通过阶段投影过滤，不能因状态为 `confirmed` 自动公开。

当前代码已经提供字段校验和纯迁移守卫，但没有持久化、审查晋升入口或分支继承流水线；本审核稿不要求现在接入这些写入路径。冻结后，生命周期写入器必须在调用纯迁移守卫前完成三项语义校验：解析 `conflictWith` 的目标快照，验证否定来源的权威性、确认状态和 `contradicts` 关系，并验证记忆范围兼容性。缺目标、来源不足、关系不匹配或跨范围来源必须拒绝迁移；纯守卫不能把非空 `conflictWith` 单独视为“权威冲突”。

## 4. 审核后可执行的最小实现顺序

1. 人工确认上述三个规则，尤其是 `stateVisibility` 的正式缺失策略。
2. 在 `state-schema.json` 增加 `state-visibility/0.2` 声明，为加载器增加归一化字段映射、模板展开、RFC 6901 转义、版本、叶子路径和路径冲突校验。
3. 增加 `contextProjection.stateVisibilityMode`、官方包清单闸门、bundle 审计字段和独立 `projectionRejectionAudit`；正式 projection 缺声明即受控失败。
4. 在正式上下文入口校验调用方提供的非空 `requestId`，仅在旧入口适配层生成并持久化缺省 ID；增加同一 `requestId` 重试和分支切换测试，不改变模型提示。
5. 按当前 `allowedEvidence` 契约增加历史连续性证据的来源、位置、权威性、分支、有效性和 `derivedTurn` 校验测试。
6. 增加动态记忆迁移流水线的内存态测试和审计记录，覆盖冲突目标、`contradicts` 关系、范围兼容性与权威来源，不接入正式状态提交。
7. 通过复审后，才开始 `PlayerNarrativePlanner` 的阶段投影迁移。

## 人工需要确认的事项

- 是否接受 `stateVisibility` 放在 `modules/state-schema.json` 顶层，使用 `state-visibility/0.2`，声明针对归一化 branch state，并使用 JSON Pointer 叶子路径和受限的运行时 `pathTemplates`。
- 是否接受使用 `audit_fallback` / `formal_required` 两阶段闸门；正式 projection 缺少业务包声明时直接受控失败，fallback 仅保留只读审计。
- 是否接受 `turnIntent` 当前回合有效、`continuityWindow` 只保留最近 2 个已确认回合。
- 是否接受以现有 `requestId` 作为 `turnKey`，可重试提供方失败保留同一 `requestId` 的 `turnIntent`，终止失败才清除。
- 是否接受正式 API 强制调用方提供 `requestId`，仅旧入口适配层允许在首次模型调用前生成并返回，且拒绝空值进入 Planner。
- 是否接受连续性过期条目按 `confirmed_event` 历史证据结构保留，并继续受阶段投影过滤。
- 是否接受 `expiredAtTurn` 使用首次移出窗口时新提交节点的 `derivedTurn`。
- 是否接受 branch confirmed 记忆必须显式复制并生成新的 `memoryId`。
- 是否接受 `confirmed -> rejected` 必须同时满足既有 `conflictWith` 目标、`relation=contradicts`、权威已确认来源和范围兼容性。
- 对 `characterLocationIds`、`itemOwnerCharacterIds`、`itemLocationIds`、`characterOutcomeStates`、`sourceProgress` 等字段，哪些可以标记为 `public_world_fact`。
