状态：待人工审核；本文件冻结候选规则，不代表动态记忆生命周期已经实现。

历史快照说明（2026-09-27）：上面的状态及下文记录的是 09-23 规则审核阶段，不代表当前需重新申请同一授权。当前实施边界与缺口以[最新核查](context-management-offline-review-2026-09-27.md)和[接续清单](context-management-next-checklist-2026-09-24.md)为准；动态记忆生产闭环仍未完成。

## 目的与边界

本文件把 `ContextBundle` 的三层上下文规则收敛为后续装配器、阶段投影和真实回放共同使用的审核基线。规则只约束上下文来源、权威性、生命周期和分支边界，不改变 StoryPackage、分支状态或正式写入接口。

在本文件被审核通过前，不能把任何 Planner 投影迁移到 bundle，也不能把动态记忆状态变化接入正式状态提交链。当前代码已经具备部分结构校验，但尚未提供完整的状态迁移 API、短期记忆 TTL 或业务包级 `stateVisibility` 声明。迁移期间允许运行时保守白名单作为只读审计 fallback，bundle 必须记录 `provenance.stateVisibilitySource=runtime_default_migration`；该 fallback 不能进入正式提示迁移。

## 一、底层硬规范 `hardSpec`

| 项目 | 冻结规则 |
| --- | --- |
| 产生者 | 代码、StoryPackage、当前分支权威状态、已确认事件和本回合行动契约 |
| 内容 | 身份、视角、当前入口、当前分支、当前位置、人物／物品状态、允许状态变化、停止点、输出契约和审查边界 |
| 模型权限 | 只读；模型不能改写、删除、晋升或降低其权威级别 |
| 生命周期 | 与 package、branch 和状态版本绑定；状态发生合法变化时创建新 bundle，并通过 `parentContextId` 关联 |
| 冲突规则 | `hardSpec` 高于 confirmed dynamic memory、短期记忆、未确认证据和文风字段；冲突必须保留诊断记录 |
| 分支规则 | 只能使用当前分支、package 或 global 范围中经过可见性校验的内容；不能读取未来 beat、兄弟分支或作者真相 |
| 失败规则 | schema、状态叶子、行动契约或可见性校验失败时，在模型调用前受控失败 |

`authoritativeState` 不能直接等同于玩家可见状态。每个进入玩家阶段的叶子字段必须由 `stateVisibility` 显式标记，只有 `player_known` 和 `public_world_fact` 可以进入玩家投影。

业务包正式规则要求显式提供 `stateVisibility`。在业务包声明尚未完成前，`runtime_default_migration` 只能公开玩家身份、玩家当前位置和玩家自身位置，且只用于只读审计；审计记录必须能区分显式声明与 fallback 来源。

## 二、短期记忆 `shortTermMemory`

短期记忆只保存当前回合所需的有限承接，不创造新的世界事实。

| 子项 | 冻结规则 |
| --- | --- |
| `turnIntent` | 只对应当前玩家输入和当前选择；回合结束后失效，不自动继承到下一回合 |
| `continuityWindow` | 只保留当前分支最近两个已确认回合的承接；每项必须带来源 ID、分支 ID和确认状态 |
| 分支切换 | 切换分支时重新计算；父分支短期记忆不直接复制到子分支 |
| 过期处理 | 超出两个已确认回合、来源缺失或来源未确认时，从短期记忆移除并降级为带来源的历史证据 |
| 内容边界 | 不得新增角色知识、世界事实、物品状态或行动授权；摘要与权威状态冲突时摘要降级为 `unknown` 或 `rejected` |
| 文风关系 | `styleGuide` 不属于短期事实记忆，预算不足时优先删除文风样本和重复表述 |

两个已确认回合是第一版确定性规则，后续只有在预算测量和真实回放后才允许调整，并必须更新规则版本和回归样本。

## 三、动态记忆 `dynamicMemory`

动态记忆采用不可静默覆盖的快照方式。每次状态变化产生新的投影记录，旧记录保留在审计中；同一 bundle 只暴露每个 `memoryId` 的当前有效快照。

### 状态定义

| 状态 | 含义 | 可进入玩家阶段 |
| --- | --- | --- |
| `candidate` | 有来源但尚未完成代码和审查确认的候选记忆 | 否 |
| `confirmed` | 来源已确认、权限一致且通过状态／语义审查的记忆 | 仅当可见性允许 |
| `unknown` | 信息不足、来源冲突或无法判断的记忆 | 否 |
| `rejected` | 被权威状态、证据或审查明确否定的记忆 | 否 |

### 状态迁移

| 当前状态 | 允许迁移 | 条件 |
| --- | --- | --- |
| `candidate` | `confirmed` | 所有来源为 confirmed，代码校验通过，审查确认其权限和事实支撑 |
| `candidate` | `unknown` | 来源不足、冲突未解决或无法判断真假 |
| `candidate` | `rejected` | 被权威状态或已确认证据否定，且记录否定来源 |
| `unknown` | `candidate` | 出现新的可追溯证据；必须增加 sequence，不得覆盖旧快照 |
| `unknown` | `confirmed` | 新证据满足 confirmed 的全部条件 |
| `unknown` | `rejected` | 出现明确否定证据 |
| `confirmed` | `rejected` | 仅允许权威状态或更高优先级 confirmed 证据形成明确矛盾；旧 confirmed 记录保留，并生成冲突关系 |
| `rejected` | 无 | 不原地复活；新证据若独立成立，创建新的 `memoryId` |

模型最多只能提出 `candidate` 或 `unknown`。模型输出不能直接产生 `confirmed`，也不能直接修改 `authoritativeState`。

### 来源、可见性和分支规则

- 每条动态记忆必须通过 `sourceIds` 追溯到当前 `allowedEvidence` 的事实证据；文风样例、角色台词或未确认正文不能单独支撑 confirmed 事实。
- `visibility` 不可隐式升级。`character_known`、`player_known`、`public_world_fact` 和 `author_truth` 之间不能自动去除限制。
- `candidate`、`unknown` 和 `rejected` 不得跨分支继承。
- package/global 范围的 confirmed 公开事实可以被新分支读取，但仍需通过当前投影的可见性检查。
- branch 范围的 confirmed 记忆不能自动复制；若业务明确允许继承，必须创建新的 branch 快照，保留 `parentMemoryId` 和继承原因。
- 冲突不得通过覆盖旧记录解决；必须保留冲突双方、来源 ID、sequence 和最终裁决。

动态记忆链接字段固定为：`parentMemoryId`（继承来源）、`inheritanceReason`（继承理由）和 `conflictWith`（冲突记忆 ID 列表）。它们在当前阶段只做结构校验；真正的状态迁移和冲突裁决仍由后续流水线负责。package/global 范围的 `unknown` 与 `rejected` 记录只能用于内部 `grounding_review`、`fact_extract` 和审计投影，不得进入玩家或 `chapter` 投影。

## 四、固定优先级与阶段边界

上下文冲突按以下顺序裁决：

```text
hardSpec
  > confirmed dynamicMemory / confirmed event
  > shortTermMemory.turnIntent
  > shortTermMemory.continuityWindow
  > unconfirmed evidence
  > styleGuide
```

五类阶段投影必须遵守同一套优先级：

- `result_contract`：只读取硬规范、当前意图、状态边界和必要公开证据。
- `chapter`：增加已确认结果契约、连续性窗口和可压缩文风字段。
- `fact_extract`：只能从已生成正文中提取候选事实，不能用摘要替代正文。
- `grounding_review`：同时读取正文断言和原始证据，不接受 Planner 的隐含结论作为证据。
- `repair`：只读取原 bundle、原正文和明确问题；未知修复范围必须受控失败或转人工。

## 五、当前实现对照与未完成项

当前 `ContextBundle` 已经覆盖：

- 三层兼容字段映射和固定优先级。
- dynamic memory 的字段、状态和值域校验。
- 来源追溯、可见性一致性、分支隔离和玩家投影过滤。
- dynamic memory 的 `parentMemoryId`、`inheritanceReason` 和 `conflictWith` 链接字段结构校验。
- dynamic memory 的 append-only 状态迁移守卫；它只校验迁移，不负责持久化、语义审查或状态提交。
- `selectedDirection` 的嵌套结构和长度校验。

仍未实现、因此不能宣称第一项已经完成的内容：

1. 短期记忆的回合计数、过期和分支切换装配器。
2. dynamic memory 的状态迁移持久化、冲突裁决和 `parentMemoryId` 继承记录；当前只有纯校验守卫。
3. 业务包级 `stateVisibility` 声明及其版本化校验；当前仅记录显式声明和迁移期 fallback 的来源。
4. 由代码和审查流水线生成 confirmed 快照的正式入口。
5. 对上述规则的 API 集成测试和真实长篇回放验证。

## 六、审核门槛

本规则进入“已冻结”前必须满足：

1. 人工确认两个回合的短期记忆窗口是否作为首版规则。
2. 人工确认 branch confirmed 记忆采用“显式复制 + `parentMemoryId`”的继承方式。
3. 为每条状态迁移补充至少一个通过样例和一个拒绝样例。
4. 明确业务包级 `stateVisibility` 的声明位置、版本和缺失时的失败策略；在正式声明前只允许带标记的只读 fallback。
5. API 环境可运行后，补充回合入口的状态、分支和投影集成测试。

在这些门槛完成前，阶段 2 只能继续做只读审计和测试夹具准备；不得进入 Planner 正式提示迁移，也不得接入 Jev。
