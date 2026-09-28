状态：设计中；本文件只冻结上下文工程边界，不代表正文质量已经验收。

## 目标与边界

本阶段先解决“模型每一轮到底允许知道什么、必须完成什么、哪些内容只能视为证据”的工程问题，再讨论是否引入 Jev。目标是提高有效初稿率，减少知识越界、替玩家作决定、状态与正文冲突，以及修复阶段反复引入新错误的情况。

本阶段不承诺解决完整的文学质量、母本重复、真实模型延迟或所有语义审查问题；也不改变 StoryPackage、分支状态和事件账本的权威关系。模型只生成候选正文和结构化判断，不能直接改变权威状态。

当前问题依据：`ModuleContextResolver` 已限制模块和前史范围，但 `PlayerNarrativePlanner._prompt` 仍把 `reading_history`、`turn_material`、`fact_sheet`、结果契约和状态防护分别拼接。规划、正文、事实提取与复核会重新组织输入，尚未共享一个带版本和哈希的上下文快照。真实回放已经出现错误行动边界、规则写反、背景编造、事实提取覆盖不足和多轮修复耗时过长；因此先治理输入边界比先替换模型更可行。

## 核心对象：`ContextBundle`

每个回合只构建一次 `ContextBundle`，规划、正文、审查和修复都引用它的固定版本。它不是新的状态存储，也不替代现有 `runtime-index.json` 或 `ModuleContextResolver`，而是把已有权威数据投影成可审计的输入。

建议的最小结构如下：

```json
{
  "schemaVersion": "narrative-context/0.3",
  "contextId": "ctx-...",
  "contextSha256": "...",
  "package": {"id": "...", "version": "..."},
  "branch": {"parentBranchId": "...", "lineageHead": "..."},
  "hardConstraints": {},
  "turnIntent": {},
  "authoritativeState": {},
  "stateVisibility": {},
  "allowedEvidence": [],
  "continuityWindow": [],
  "dynamicMemory": [],
  "styleGuide": {},
  "outputContract": {},
  "provenance": {},
  "budget": {}
}
```

`contextSha256` 对规范化后的 bundle 计算，模型调用审计记录 `contextId`、哈希、选中的模块／事实 ID、实际输入 token 和省略原因。重试或修复如果改变了事实边界，必须创建新版本并重新校验；只改变措辞修复指令时可以复用原 bundle。

## 三层上下文总架构

上下文工程固定为三层。三层不是三个互相覆盖的提示词，而是三种不同生命周期和权威级别的数据。

### 底层硬规范 `hardSpec`

由代码从 StoryPackage、当前分支状态、确认事件和本回合行动契约生成。它包括身份、视角、入口、分支、当前位置、人物与物品状态、允许的状态变化、行动契约、停止点、输出格式和审查边界。玩家原子要求保留在 `shortTermMemory.turnIntent`，再由行动契约把可执行边界固化到 `hardSpec`。模型只能读取，不能改写、晋升或删除。正式状态提交只能依据通过校验的结果契约和确认事件完成。

### 短期记忆 `shortTermMemory`

只服务当前回合和当前路线的短窗口，包含当前回合意图、最近 1–2 个已确认段落、刚完成的动作、待回应问题和必要的连续性摘要。它帮助正文保持动作、问答和语气连续，但不构成新的世界事实。回合提交、分支切换或上下文重建时重新计算；过期内容必须降级为带来源的历史证据，不能继续以短期记忆身份留在上下文中。

### 动态记忆 `dynamicMemory`

承载跨回合但可变化的事件、线索、关系、目标进展和公开事实。每条记忆必须包含 `memoryId`、`kind`、`sourceIds`、`branchId`、`visibility`、`authority`、`validity`、`status` 和 `sequence`。`status` 至少包括 `candidate`、`confirmed`、`rejected`、`unknown`；模型只能提出 `candidate`，代码和审查通过后才能晋升为 `confirmed`。冲突时保留旧记录和冲突关系，不覆盖历史；分支创建时只继承明确允许继承的 confirmed 记忆，candidate 不得跨分支继承。

三层优先级固定为：`hardSpec > confirmed dynamicMemory/confirmed event > shortTermMemory.turnIntent > shortTermMemory.continuityWindow > unconfirmed evidence > styleGuide`。角色认知、玩家已知、公开世界事实和作者真相必须使用不同的 `visibility`，不能因为某个角色说过一句话就把它晋升为世界事实。

`authoritativeState` 不是天然可供玩家正文读取的对象。每个可公开状态叶子必须通过 `stateVisibility` 标注；`chapter`、`result_contract` 和 `repair` 投影只保留 `player_known` 与 `public_world_fact`，未标注或标为 `author_truth`、`character_known` 的状态不会进入玩家阶段。动态记忆的 `sourceIds` 必须解析到当前 `allowedEvidence` 中带有完整权限字段的事实证据，并且来源可见性、有效性和权威级别必须与记忆一致。

当前 `narrative-context/0.3` 保留兼容字段映射：`hardConstraints + authoritativeState + stateVisibility + outputContract` 对应 `hardSpec`，`turnIntent` 和 `continuityWindow` 是 `shortTermMemory` 的两个子项，`allowedEvidence + provenance` 是 `dynamicMemory` 的只读证据投影。完整 `provenance` 只进入内部事实提取和依据复核投影，玩家阶段不携带来源 ID 或排除 ID。这个映射不表示动态记忆的写入、晋升和失效链已经实现；进入阶段 2 前必须完成独立的状态迁移规则。

### 三层验收矩阵

| 检查项 | 底层硬规范 | 短期记忆 | 动态记忆 |
| --- | --- | --- | --- |
| 产生者 | 代码／权威状态 | 回合装配器 | 事件提交与审查流水线 |
| 模型权限 | 只读 | 只读、不可延长生命周期 | 只能提出 candidate |
| 生命周期 | package／branch／状态版本 | 当前回合或短窗口 | append-only，按状态晋升或失效 |
| 冲突处理 | 最高优先级 | 降级为历史证据 | 保留冲突，不静默覆盖 |
| 分支规则 | 当前分支严格隔离 | 随当前分支重建 | 只继承明确允许的 confirmed 记录 |
| 失败判定 | schema／状态错误即阻断 | 超期或来源缺失即降级 | 未确认记忆不得进入事实层 |

## 三层内部字段分解（兼容字段）

以下字段仍保留在 `narrative-context/0.3` 中，但不再代表额外的上下文层。它们只是三层的内部字段或只读投影：`hardConstraints`、`authoritativeState`、`stateVisibility`、`outputContract` 属于 `hardSpec`；`turnIntent`、`continuityWindow` 属于 `shortTermMemory`；`allowedEvidence`、`provenance` 是 `dynamicMemory` 的只读证据投影；`styleGuide` 是可被预算淘汰的输出策略字段。

### `hardSpec` 内部字段

由代码生成，优先级最高，不能被摘要或模型改写：

- 会话身份、第二人称限知视角、当前入口和当前分支。
- 当前玩家位置、人物生死／下线、物品实际持有与地点、已确认关系和目标状态。
- 当前 StoryPackage 的 arc、beat、action contract、允许的时间线和状态变化边界。
- 本回合停止点：允许玩家完成的动作、不能替玩家作出的决定、未授权转场和未授权发言。
- 输出格式、事实引用要求、长度和自然收束条件。

状态冲突时以已确认事件和当前状态为准；历史摘要只能作为待核对证据，不能覆盖状态。

### `shortTermMemory.turnIntent` 内部字段

保留玩家原始输入，同时将其解析为代码可检查的原子要求：对象、动作、范围、顺序、停止点和是否允许转场。结果契约属于这一层的派生约束，必须保留原始要求与规划结果的对应关系，不能只给模型一个自由改写后的总结。

### `dynamicMemory` 的只读证据投影

所有证据项都带有 `sourceId`、来源类型、分支、可见性、证据位置、有效状态和用途。例如 `public_fact`、`current_beat`、`confirmed_event`、`recent_prose`、`style_sample` 应分开。`sourceDialogueContext` 只能证明角色说过某句话，不能单独证明其内容为世界事实；玩家可见、角色已知和作者真相也不能混为一层。

证据筛选先沿用 `ModuleContextResolver` 的当前 beat、受控前置 beat、`contextRefs`、公开事实和当前分支事件。首期不引入向量检索，避免相似文本把未来剧情、其他分支或未公开信息带入上下文。

### `shortTermMemory.continuityWindow` 内部字段

只保留当前路线最近的少量已确认承接，用于语气和动作连续。最近正文不再作为事实账本；较早内容压缩为带来源 ID 的事件／目标摘要。建议首版保留最近 1–2 个完整已确认段落，历史摘要最多保留当前未解决目标和刚发生的结果，具体数量以实际 tokenizer 测量后冻结。

### `styleGuide` 兼容字段

文风、句式和节奏样本与事实证据分离。文风字段可以被预算淘汰，硬约束和当回合证据不能被静默淘汰。禁止用整段原著或长篇历史正文作为“模仿模板”，避免把未确认的环境描写重新带回下一回合。

## 阶段投影

同一个 bundle 为不同阶段生成最小投影，防止每个阶段各自拼接上下文：

| 阶段 | 允许输入 | 不应输入 |
| --- | --- | --- |
| `result_contract` | 硬约束、回合意图、当前状态、必要公开证据 | 文风样本、无关历史正文 |
| `chapter` | 硬约束、已确认结果契约、授权证据、连续性窗口、短文风字段 | 未来 beat、其他分支、作者真相 |
| `fact_extract` | bundle、正文草稿、需要抽取的字段 | 用模型摘要替代正文或状态 |
| `grounding_review` | bundle 中的证据项、正文中的断言及其位置 | 规划器未引用的隐含证据 |
| `repair` | 原正文、明确问题、原 bundle | 无边界的重新创作上下文 |

审查输入必须包含原始证据和正文，而不是只接受规划器的结论；这样可以继续识别“计划看似正确但正文越权”的情况。

## 预算与降级规则

预算按实际目标模型 tokenizer 计算，不能用汉字数代替 token。每次审计至少记录固定规则／文风、硬状态、证据、连续性、总输入和输出的 token 数。

超预算时按以下顺序降级：

1. 删除无关文风样本和重复表述。
2. 缩短较早连续性摘要，保留事件 ID、结果和来源。
3. 删除与当前角色、地点、物品、动作无关的软证据。
4. 仍超预算时返回明确的受控失败。

不得截断当前硬约束、回合意图、状态变化边界、当前 beat 或必要公开证据；不得通过重复重试或模板填充掩盖最小有效上下文超限。

## 可观测性与校验

首期应先增加 bundle 级审计，不改变提示措辞：

- 同一 package、状态、分支和玩家输入必须得到相同的选中 ID、排序和哈希。
- 日志能回答“加载了哪些模块、带入了哪些事实、排除了哪些未来／其他分支内容、因预算省略了什么”。
- 当前状态覆盖历史摘要冲突；未知值保持 `unknown`，不能因相似文本变成已知。
- 规划、正文和审查使用同一 `contextSha256`；若状态投影因结果契约改变，记录新版本和父版本。
- 失败保留原始 bundle、草稿、问题和修复结果，不把拒绝伪装成质量通过。

离线测试先覆盖确定性、分支隔离、未来信息排除、权限边界、状态优先级、最小预算失败和审查输入完整性。然后用达标长篇的真实模型连续验证“告知、询问依据、短等待”三类场景，记录首稿通过率、最终写入率、知识泄露、行动越权、事实／状态冲突、调用次数、输入 token、P95 等指标。离线或 mock 通过不能关闭 NQ-001。

## 分阶段落地

### A：契约与审计（不改变正文行为）

新增 bundle 数据结构、规范化哈希、来源元数据和审计字段；用现有 `ModuleContextResolver`、状态投影和结果契约填充。把当前提示拼接结果作为兼容投影，先验证同一回合各阶段可以共享同一快照。

### B：确定性装配

将 `PlayerNarrativePlanner._prompt` 的历史、场景材料、事实表和状态防护迁移到 bundle builder；建立硬约束、意图、证据、连续性和文风的分层序列化。删除重复注入和阶段之间的独立检索，但不先调整模型或增加调用。

### C：阶段投影与失败边界

让规划、正文、事实抽取、依据核对和修复使用各自最小投影；补齐预算测试、证据省略原因、上下文版本绑定和受控失败。先在现有模型上复验三个真实场景，确认首稿有效率和错误类型是否改善。

### D：真实质量基线

固定 package、提示版本和上下文版本，做连续真实模型回放和桌面阅读验收。只有当上下文边界稳定、首稿和最终写入指标达到可比较基线后，才进入模型替换试验。

## Jev 的后置试验边界

Jev 暂不进入默认链路，也不承担上下文装配、事实权威或正文生成。后续若试验，只允许作为独立的 `JevDecisionGateway` shadow／旁路评估器：对脱敏且已裁剪的 bundle 提出原子判断，例如“该断言是否在允许证据中”“该行动是否超出授权范围”。问题必须拆成单一判断，代码负责计数、状态和最终裁决。

试验前置条件是：上下文 bundle 已有固定 schema、哈希、预算和审计；现有模型有真实长篇基线；Jev 版本固定并单独记录；中文场景完成校准；任何 Jev 超时、限流、低置信或不确定结果都回退到现有审查路径。Jev 的判断不能修改正文或 StoryPackage，也不能替代人工对文学可读性的验收。

## 当前结论

先做上下文工程是可行且优先级正确的路径：它复用现有模块索引、状态账本和事实审查，改动面可控，能够直接针对当前质量失败证据建立可比较指标。先接入 Jev 的收益难以归因，因为如果输入边界仍漂移，任何质量变化都无法判断来自模型还是上下文。建议先完成 A–C，再用 D 的固定基线决定是否启动 Jev 旁路试验。
