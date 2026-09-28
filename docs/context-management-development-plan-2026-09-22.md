状态：执行计划；设计依据见 [上下文管理质量设计](context-management-quality-design-2026-09-22.md)。本计划不表示任何阶段已经完成。

状态入口（2026-09-27）：下文为最初计划与历史阶段记录；当前事实以[独立复核及三层核查](context-management-offline-review-2026-09-27.md)和[接续清单](context-management-next-checklist-2026-09-24.md)为准。新增[剧情配图计划](context-management-illustration-followup-2026-09-27.md)在上下文工程验收后实施。

## 1. 交付目标

把当前分散在 `ModuleContextResolver`、`reader_scene_review`、`reader_consequences`、`api_narrative.PlayerNarrativePlanner` 和 `api_play` 中的上下文拼接，收敛为一条可复用、可审计、可测试的上下文装配链：

```text
StoryPackage + branch state + player input
                    |
                    v
           ContextBundleBuilder
                    |
                    v
   frozen ContextBundle + contextSha256 + provenance
        |          |          |          |
        v          v          v          v
 result_contract chapter fact_extract grounding/repair
```

最终需要回答五个问题：

1. 本回合模型被允许知道哪些事实？
2. 这些事实分别来自哪个模块、事件或正文段落？
3. 哪些内容明确被排除，为什么被排除？
4. 规划、正文和复核是否使用同一份上下文？
5. 上下文大小、遗漏和质量结果是否能被量化比较？

Jev 不在上述默认链路中。只有阶段 5 的真实基线和阶段 6 的质量门槛通过后，才启动独立旁路试验。

## 2. 固定约束

开发期间必须保持以下边界：

- `StoryPackage`、已确认事件、分支状态和状态投影是权威来源；模型输出不能直接写入权威状态。
- 当前只使用达标的官方长篇运行包；《雨夜候车室》不得重新作为当前测试素材。
- 不读取 `reader/` 原著全文作为 Planner 上下文，不读取未来 beat、其他分支或未公开作者真相。
- 不用摘要覆盖当前状态；状态与摘要冲突时保留冲突记录并以状态为准。
- 不通过降低事实／状态校验、无限重试、模板填充或伪造正文来提高通过数。
- 保留现有未提交改动；本专项新增文件和改动只限上下文工程相关模块、测试和文档。
- 每阶段都必须能独立回滚；阶段之间不合并 Jev、提示词大改和状态模型重构。

## 3. 目标文件布局

首期建议新增两个小模块，避免把 bundle 继续塞进 `api_narrative.py`：

| 文件 | 责任 |
| --- | --- |
| `open_story_engine/context_bundle.py` | `ContextBundle` 的规范化结构、来源记录、哈希、阶段投影和校验错误 |
| `open_story_engine/context_budget.py` | tokenizer 适配、预算计算、降级顺序、最小有效上下文超限错误 |
| `open_story_engine/module_context.py` | 保留模块读取与 StoryPackage 边界；必要时增加结构化来源输出，不改变模块加载规则 |
| `open_story_engine/api_narrative.py` | 在回合入口构建一次 bundle；各阶段只消费投影，不再各自重新装配事实 |
| `open_story_engine/reader_scene_review.py` | 复核接口接受证据项和来源 ID，继续区分角色台词、背景断言和世界事实 |
| `open_story_engine/reader_consequences.py` | 将规划用状态、目标和结果契约作为 bundle 的硬约束投影 |
| `tests_core/test_context_bundle.py` | 不依赖模型的确定性、哈希、隔离和预算测试 |
| `tests_api/test_context_bundle_integration.py` | Planner 阶段共用 bundle、审计绑定和失败回执测试 |
| `docs/evidence/context-management-2026-09-22/` | 真实回放的 bundle 摘要、指标和失败样本；不得只保存通过样本 |

若现有测试目录没有 `tests_core/`，按仓库实际核心测试入口放入现有核心测试目录，不为计划强行创建第二套测试发现机制。

## 4. 阶段 0：冻结基线与保护工作区

### 工作内容

1. 记录当前代码版本、StoryPackage ID／version、提示版本和网关模型。
2. 选定三类真实回放输入：普通告知、询问依据、短等待；保留完整请求、正文、审查结果、失败阶段和用量。
3. 为当前 `PlayerNarrativePlanner.plan` 增加只读诊断入口或测试捕获，记录现有各阶段收到的上下文摘要，不改变提示内容。
4. 盘点当前 `context_resolver.resolve`、`turn_material`、`fact_sheet`、`scene_knowledge` 和 `planning_context` 的字段来源，形成字段映射表。

### 产物

- `docs/evidence/context-management-2026-09-22/baseline.json`
- `docs/evidence/context-management-2026-09-22/field-map.json`
- 一份当前失败分类：知识越界、行动越权、状态冲突、证据缺失、修复重复、预算／延迟。

### 退出条件

- 三类输入均有可追溯的当前基线，失败也保留。
- 能区分“上下文输入问题”和“模型输出／审查问题”；无法区分的记录为 `unknown`。
- 不改变正式存档，不将基线结果计为质量通过。

### 检查

```bash
python3 -B -m test_support.run core
.venv-api/bin/python -B -m test_support.run api
git diff --check
```

## 5. 阶段 1：冻结 `ContextBundle` 契约

### 工作内容

在 `context_bundle.py` 定义最小稳定结构：

```text
schemaVersion
contextId / parentContextId
package {id, version}
branch {sessionId, parentBranchId, lineageHead}
hardConstraints
turnIntent {rawInput, atomicRequirements, stopPoint}
authoritativeState
stateVisibility
allowedEvidence[]
continuityWindow[]
dynamicMemory[]
styleGuide
outputContract
provenance {modulePaths, sourceIds, excludedSources, exclusions}
budget {limits, estimates, omittedSoftItems}
contextSha256
```

每个 `allowedEvidence` 项至少包含：`sourceId`、`kind`、`visibility`、`branchId`、`location`、`content`、`authority`、`validity`。`authority` 至少区分 `authoritative`、`confirmed_evidence`、`style_only`；`validity` 至少区分 `confirmed`、`unknown`、`rejected`。

阶段 1 的契约必须同时明确三层映射：`hardConstraints + authoritativeState + stateVisibility + outputContract` 属于只读的底层硬规范；`turnIntent` 和 `continuityWindow` 是当前回合短期记忆的两个子项；`dynamicMemory[]` 是动态记忆结构；`allowedEvidence + provenance` 只是它的只读证据投影。动态记忆的 `candidate/confirmed/rejected/unknown` 生命周期、晋升条件、失效条件和分支继承规则在阶段 2 装配器前必须单独冻结，不能把当前 `allowedEvidence` 误当作已经完成的动态记忆系统。

`visibility` 固定为 `player_known`、`character_known`、`public_world_fact`、`author_truth` 四类；`dynamicMemory` 每项必须包含 `memoryId`、`kind`、`sourceIds`、`branchId`、`visibility`、`authority`、`validity`、`status`、`sequence` 和 `content`。当前实现只负责结构校验和只读投影，不负责记忆晋升、失效或分支继承。`authoritativeState` 的玩家投影必须经过 `stateVisibility` 叶子白名单；玩家阶段只允许 `player_known` 和 `public_world_fact`。动态记忆来源必须解析到 `allowedEvidence` 的事实证据，来源的可见性、有效性和权威级别不能与记忆声明不一致。

实现规范化序列化和稳定哈希：字典键排序、列表按明确优先级或稳定 ID 排序、去除非语义临时字段。哈希前不得包含原始响应、时间戳或随机 ID；`contextId` 可用于追踪，`contextSha256` 必须由内容决定。

### 唯一冲突优先级

```text
hardSpec（权威状态、stateVisibility、当前 beat action contract、停止点）
    > confirmed dynamicMemory / confirmed event
    > shortTermMemory.turnIntent（玩家原始输入的原子化结果）
    > shortTermMemory.continuityWindow（连续性窗口和最近已确认段落）
    > unconfirmed evidence
    > styleGuide / style sample
```

玩家原始意图不能覆盖行动契约；短期记忆不能新增世界事实；冲突项必须标记为矛盾、无效或待确认，不能静默合并。后续文档和代码只引用这一套顺序。

### 退出条件

- 相同输入得到相同 `contextSha256`。
- schema 缺字段、来源跨分支、无效状态和不受支持的证据类型会在进入模型前失败。
- `visibility` 不属于四个受控值、动态记忆缺字段、candidate 跨分支或快照内部字段被修改时会在进入模型前失败。
- 可从 bundle 反查每一条硬约束和证据的来源。

### 检查

新增单元测试覆盖空值、未知值、排序、哈希稳定性、schema 错误和冲突优先级。不调用外部模型。

三层契约的审核矩阵也必须覆盖：硬规范不可被模型改写；短期记忆过期后降级为带来源历史证据；动态记忆只能由代码和审查流水线从 candidate 晋升；candidate 不得跨分支继承；角色认知、玩家已知和世界事实不得混层。矩阵未通过时，不进入动态记忆生命周期和 Planner 接线阶段；当前 builder 只做只读、确定性装配，不宣称三层生命周期已经完成。

## 6. 阶段 2：实现确定性装配器

### 工作内容

在 `api_narrative` 回合入口建立 `ContextBundleBuilder`（可以先放在 `context_bundle.py`，不再增加第三个模块）：

1. 从 `ModuleContextResolver.resolve` 取得当前 beat、受控前置 beat、角色／地点／物品和公开事实。
2. 从父分支和 projected state 提取位置、人物后果、物品状态、关系、目标、线程和允许状态变化。
3. 将玩家原文拆成原子要求；无法可靠拆分的部分保留原文并标记 `unknown`，不猜测意图。
4. 将 `turn_material`、`fact_sheet`、`scene_knowledge` 和结果契约映射到不同层，不再把它们拼成无标签长字符串。
5. 生成最近 1–2 个已确认连续段落和带 ID 的较早结果摘要；禁止加载整条历史正文。
6. 生成 `provenance.excludedSources`，明确排除未来 beat、其他分支、未公开事实和不相关实体。

### 选择规则

首版只做确定性选择，不引入向量数据库或外部检索：

- 当前 beat 和其声明的 `contextRefs` 优先。
- 玩家当前角色、位置、行动对象和未结目标相关实体次之。
- 事件账本只取当前分支且已确认的结果。
- 历史摘要必须带来源 ID；摘要与事件冲突时摘要降为 `rejected` 或 `unknown`。
- 角色台词和世界事实分开保存，不能仅因角色说过就提升为事实。

当前已实现第一版 `ContextBundleBuilder`：它只消费 `ModuleContextResolver` 已经裁剪过的结构化结果，或由调用方显式传入同等的 `module_context`；分支标识必须由调用方提供，`stateVisibility` 可由调用方显式声明，未声明时由 Planner 使用保守的玩家字段白名单。builder 会保留经过边界裁剪的角色、地点和物品身份，只接受由状态层显式传入且能与当前状态形状核对的 `validated_state_patch`，并将模块证据标为 package 范围。builder 不改变 Planner 提示和正式写入链，未来 beat、兄弟分支、reader 原著和未标注作者真相只作为排除记录，不进入 `allowedEvidence`。

本轮接线仍限定为只读审计：`CoCreationService` 先完成 `apply_branch_patch`，再向 Planner 传递带有 `validatedStatePatchSource=apply_branch_patch` 的补丁投影；缺少该状态层标记时，bundle 不会被记为有效记录。`LlmPlanner._prompt` 在模块上下文已解析后记录 bundle 的 `contextId`、`contextSha256`、完整快照和状态。分支使用当前父节点作为 `parentBranchId`，没有凭空推导独立分支 ID；调用方未声明 `stateVisibility` 时使用 builder 的保守白名单，仅公开玩家身份、玩家当前位置和玩家自身在 `characterLocationIds` 中的位置，其他状态字段保持隐藏；调用方显式传入的可见性声明仍按业务包配置校验。自由文本和其他受保护的内部补丁会在 bundle 输入投影中明确列入 `excludedStatePatchFields`，不进入 `selectedStatePatch`。bundle 被拒绝时仅写入 `status=rejected` 与错误原因，当前阶段不改变既有 Planner 提示或正式写入路径；待状态可见性声明和受保护补丁投影完成审核后，才进入阶段 3 的提示迁移。

### 退出条件

- 同一 package／state／input 的装配结果确定性一致。
- 构造一个未来 beat、兄弟分支和未公开事实的污染夹具时，三者都不进入 `allowedEvidence`。
- 当前状态能覆盖错误历史摘要；未知字段仍保持未知。
- 不改变现有模型提示和正式写入行为；此阶段允许只记录 bundle。

### 检查

运行核心测试，并为 `ModuleContextResolver` 增加结构化来源断言；必要时使用临时 package fixture，不修改官方长篇素材。

## 7. 阶段 3：阶段投影与提示迁移

### 工作内容

为同一个 bundle 实现五种只读投影：

| 投影 | 内容 | 目的 |
| --- | --- | --- |
| `result_contract` | 硬约束、原子意图、状态、必要公开证据 | 限定计划，不提供文风噪声 |
| `chapter` | 硬约束、已确认结果契约、授权证据、连续性窗口、短文风字段 | 生成正文 |
| `fact_extract` | bundle、正文、待抽取字段 | 只抽取正文已写出的事实 |
| `grounding_review` | 证据项、正文断言、来源 ID | 检查依据，不接受规划器隐含结论 |
| `repair` | 原正文、明确问题、原 bundle | 局部修复，禁止无边界重写 |

逐步替换 `PlayerNarrativePlanner._prompt` 中的独立拼接。迁移期间保留兼容输出，使用日志比较“旧提示字段”和“bundle 投影字段”的差异；只有字段来源和边界一致后，才删除重复拼接。

当前先在 `LlmPlanner.promptContext.projectionCompatibility` 记录旧模板字段与 `chapter` 投影的候选字段路径、路径存在状态、未映射字段和投影独有字段；它只验证 schema 路径是否存在，不证明字段值语义等价。报告只记录字段名、路径、状态及 bundle 哈希，不记录 prompt 字段值；正文 `chapter` 仍由原模板生成，完整提示迁移尚未开始。

当前已补充达标官方长篇运行包的 core prompt 集成审计：覆盖全部官方身份入口、module/package 两种 prompt 路径，以及 bundle 拒绝后不得复用旧 bundle 的行为；`PlayerNarrativePlanner` 也已接入只读审计记录。该审计仍只证明接线和边界记录正确，不代表任何 Planner 已迁移到 bundle projection。

本轮增量改为按问题类型选择 `repair` 所需的最小层：连续性问题只注入 `continuityWindow`；只有结果契约没有覆盖时，状态问题才注入玩家可见 `authoritativeState`，行动问题才注入非空 `actionContract`；没有对应问题类型时不注入额外 bundle 内容。修复请求的 `sceneEvidence` 同时按问题中的引用 ID、问题文本重合度和连续性最近历史进行选择，最多 8 项、最多 6000 字符；固定事实先登记为稳定 ID并进入 `hardConstraints.immutableFacts`，再按问题重合度选择，最多 10 项、3200 字符，并保持完整句子边界；只有旧 bundle 没有硬事实时才回退到 legacy `fact_sheet`。无关联内容时保持为空，避免把完整公开证据和固定事实窗口重新堆积进模型请求。修复请求中的 `scene` 已改为结构化对象，分开传递当前场景、玩家行动、带 ID 的固定事实和按问题类型需要的结果契约；背景或连续性修复不再携带整份结果契约。修复审计记录保存同一 bundle 的 `contextId`、`contextSha256`、证据来源 ID、窗口计数以及两类上下文选择的来源、ID、数量和省略数。完整 bundle 不进入模型 payload，正文 `chapter`、规划和复核的重复拼接仍未删除，也不代表阶段 3 已完成。

本轮继续迁移正文 `chapter` prompt：当 bundle 的 `continuityWindow` 有内容时，普通正文和 `interlude` 的历史字段都改用最多两个带 `sourceId` 的已确认摘要，并在 `last_prompt_context.chapterContinuity` 记录来源和数量；投影为空、格式无效或 bundle 不可用时，审计明确记录 `legacy` fallback，实际 prompt 才使用旧的兼容历史路径。其他正文字段（硬事实、状态、结果契约和 `event_brief`）尚未完成语义等价审核，不能据此宣称正文 prompt 已整体迁移。

本轮进一步迁移正文硬事实前，先修正 `ModuleContextResolver` 的来源装配：当模块没有独立 `facts` 字段时，也会从 `world.immutableFacts` 激活当前已确认行号以内的事实，并排除未来来源；`chapter` prompt 只按当前回合文本的词项重合或显式事实 ID 筛选最多 10 条、3200 字符，不再因为同章来源自动加分。legacy `fact_sheet` 也先拆成稳定 ID 条目，再执行同样的有界筛选；bundle 没有相关事实时才使用该兼容路径，不会同时注入两套来源。审计额外记录每条选中事实的来源章节、来源进度、行号和选择依据。官方 `taixu-relics-part1@0.1.3` 非根节点测试已覆盖 `world.immutableFacts -> ContextBundle` 链路。正式预检失败时的错误处理也保持 `candidate_history` 可用，避免把上下文拒绝覆盖成 `UnboundLocalError`。

随后补充当前 beat 的显式事实引用：模块自身声明的 `facts` ID 会进入 `currentBeat.contextRefs.factIds`，正文筛选器可在词面不重合时仅接受这些 beat 级来源；同章但未被当前 beat 引用的事实仍被排除。官方长篇测试覆盖该引用从 resolver、ContextBundle 到 `chapter_hard_facts_text` 的完整链路。

状态投影改变时必须显式生成 `parentContextId` 关联的新 bundle：例如结果契约投影了合法状态变化，正文和后续审查只能使用新版本；同一版本不得一半使用父状态、一半使用新状态。

### 退出条件

- 一个回合的规划、正文和所有审查请求记录同一个 `contextSha256`，或记录有明确的子版本关系。
- 审查请求仍能看到原始证据和正文，不会只收到规划器摘要。
- 三类旧失败都能在审计中定位到“缺少／错误／冗余的上下文层”。
- 现有离线和 API 测试保持通过；失败时保留原有失败正文和审计，不自动写入。

### 检查

重点覆盖 `tests_api/test_reader_actions.py`、`tests_api/test_reader_consequences.py`、`tests_api/test_reader_epistemics.py`、`tests_api/test_player_routes.py` 的现有调用次数和审计断言；不在本阶段主动减少审查轮数，避免把上下文改动与成本优化混在一起。

## 8. 阶段 4：预算、降级与审计完善

### 工作内容

在 `context_budget.py` 实现模型适配器：

- 固定规则／文风预算单独计算；其余上下文使用独立总预算。
- 首选目标模型 tokenizer；未配置 tokenizer 时明确记录估算方法和 `estimated` 标记，不能把字符数冒充真实 token。
- 降级顺序固定为：重复文风 → 较早连续性摘要 → 不相关软证据 → 受控失败。
- 硬约束、回合意图、当前 beat、状态边界和必要公开证据不可删除。
- 预算失败不触发无意义重试；错误码区分 `context_budget_exceeded`、`context_integrity_error` 和模型传输错误。

每次调用观察增加：`contextSha256`、投影名称、输入 token、输出 token、选中来源数、排除来源数、省略项、预算估算方式和省略原因。不得默认保存超出既有审计边界的敏感原文；需要保存原文时沿用当前审计保留规则。

### 退出条件

- 小预算夹具验证降级顺序稳定且不删除硬约束。
- 最小有效 bundle 超限时，模型调用次数为零，返回可识别的受控失败。
- 正常 bundle 的 token 统计可与供应商回执区分，未知用量不记为零。

### 检查

新增预算单元和 API 回归；执行 `git diff --check`。不把预算单元测试通过当作真实模型质量通过。

## 9. 阶段 5：现有模型真实基线与验收

### 回放设计

固定以下变量：

- 同一官方长篇 package ID／version。
- 同一身份入口、初始分支和数据库副本。
- 同一玩家输入、提示版本、ContextBundle schema 和模型配置。
- 同一审计和人工阅读标准。

至少连续回放三类场景，每类保留成功、失败和不确定样本：

1. 告知一个行动并停在玩家下一次决定前。
2. 询问一个有依据的问题，不允许新增持物、规则或背景。
3. 原地短等待，允许自然反应但不允许替玩家接受安排或转场。

### 指标

首稿有效率、最终写入率、事实／状态冲突率、知识泄露率、行动越权率、审查误拒率、修复后新问题率、每回合调用数、输入／输出 token、首字延迟、最终确认延迟和 P95。每项必须能按上下文版本和阶段投影分组。

### 阶段门槛

- bundle 在三类场景均能稳定重建且来源边界无污染。
- 首稿和最终写入指标相对阶段 0 可比较；任何改善都必须保留失败样本和人工阅读记录。
- 不能因为离线／mock 通过就关闭 NQ-001；真实模型和桌面未知项仍单独标记。
- 若上下文版本造成首稿变短、审查误拒显著增加或调用成本失控，回到阶段 3 调整投影，不进入 Jev 试验。

## 10. 阶段 6：Jev 旁路试验（后置、可撤回）

只有阶段 5 形成稳定基线后，才增加独立适配器，建议文件为 `open_story_engine/jev_gateway.py`，默认不导入核心 Planner。

### 允许的判断

- `claim_supported`：正文断言是否能在 `allowedEvidence` 中找到支持。
- `action_within_scope`：正文是否超出 `turnIntent` 和结果契约。
- `state_consistent`：正文是否与 bundle 中的权威状态冲突。

每个问题只做一个判断，代码负责组合结果、置信处理、超时、限流和回退。Jev 不生成正文、不生成状态补丁、不决定是否提交。

### 试验方式

- shadow 模式：现有审查照常决定，Jev 只记录判断和延迟。
- 脱敏和最小化输入：只发送必要证据、断言和约束，不发送整本小说或无关历史。
- 固定 Jev 版本、问题模板、样本集和结果映射；中文场景单独统计。
- 与现有人工标注和当前模型审查比较 precision、recall、误拒、漏检、延迟和成本。
- 任何超时、限流、不确定或异常都回退现有路径，不阻塞用户回合。

### 试验退出条件

- Jev 在每个判断类型上都有足够样本，结果不能只看总体平均。
- 相对现有审查的收益能归因于判断器本身，而非上下文变化。
- 没有新增知识泄露、状态误写或不可解释拒绝。
- 未达到收益门槛时删除旁路开关和依赖，不影响默认链路。

## 11. 推荐提交顺序

每个提交只包含一个可验证意图：

1. `docs/context-management-development-plan-2026-09-22.md`：冻结本计划。
2. `context_bundle.py` 契约、规范化哈希和单元测试。
3. Builder 只读装配、来源追踪和隔离测试。
4. Planner 阶段投影接入与兼容审计。
5. 预算降级、受控失败和调用审计。
6. 三类真实回放及失败分类记录。
7. 只有用户确认继续且阶段 5 门槛通过后，才新增 Jev shadow 适配器。

任何提交如果同时改变提示文案、审查轮数、模型供应商或状态提交逻辑，都应拆开，否则无法解释指标变化。

## 12. 主要风险与处理

| 风险 | 早期信号 | 处理 |
| --- | --- | --- |
| bundle 只是把旧长提示换成 JSON | token 不降、来源仍不可追溯 | 按层投影并强制来源 ID；不允许自由拼接长字符串 |
| 为了预算删除硬约束 | 越权和状态冲突上升 | 硬约束设为不可删除，超限直接失败 |
| 历史摘要继续冒充事实 | 摘要与状态冲突但未告警 | 摘要降级为证据并记录冲突，状态优先 |
| 阶段之间上下文漂移 | 同一回合哈希不同且无父子关系 | 所有请求必须携带 bundle 版本；状态变化显式建子版本 |
| 测试只覆盖 mock | 离线绿、真实三类场景仍失败 | 阶段 5 是单独门槛，保留人工阅读和真实审计 |
| Jev 试验混入默认链路 | 失败无法区分来自模型或上下文 | 只允许 shadow，固定基线后再比较 |

## 当前执行判定

阶段 0、1、2 的字段盘点、基线、bundle 契约和确定性装配已有代码与证据；阶段 3 当前只完成 core 与 `PlayerNarrativePlanner` 的只读兼容审计、模式映射和真实长篇集成检查，正文提示仍未迁移。下一步先冻结业务包级 `stateVisibility`、短期记忆过期规则和 dynamicMemory 生命周期，再逐阶段迁移 `PlayerNarrativePlanner` 的投影；暂不改 Jev 依赖、默认模型、数据库状态提交和前端流程。

## 13. 对 Gemini 对话的复核

这次对话中有三类内容值得吸收：

1. 用结构化状态表承载人物、地点、物品和世界规则，而不是把全部历史正文塞入提示。
2. 将最近正文窗口、较早的长期记忆和当前回合意图分层，按当前行动选择相关内容。
3. 将硬规则检查、语义复核和重写控制拆开，并对重写次数设置上限。

这些方向与本计划的 `ContextBundle`、权限化证据、连续性窗口和受控失败一致，但需要按本项目的权威状态和分支隔离规则实现。

### 需要修正后采用的内容

- **动态 RAG**：首期不直接引入向量数据库。先使用 StoryPackage 的 beat、`contextRefs`、事件账本和可见性过滤做确定性召回；以后若增加检索，也只能在当前分支、已公开、带来源 ID 的索引内运行。相似度高不能自动证明信息有权进入上下文。
- **事实卡片**：采用“权威状态 + 带来源的证据项”，不建立一个会被模型或异步小模型自由覆盖的全局 JSON。卡片中的 `unknown`、`rejected` 和冲突必须保留。
- **近期窗口**：`1–2` 轮可以作为初始实验值，不能作为永久规则。窗口大小、摘要长度和 token 占用必须通过目标模型 tokenizer 与真实回放确定。
- **轻量审核员**：先由代码检查能确定的状态、身份、位置、物品和行动边界，再由语义模型检查代码无法判定的原子断言。审核员不输出重写正文。
- **重写上限**：设置最多一次局部重写是可试验的成本边界，但第二次失败不能自动拼接保底套话或伪造正文；应保留未确认草稿、返回明确失败，或走产品定义的安全停顿。
- **前置自查**：可以让模型先输出结构化 `result_contract` 或 `preflight`，供代码校验；不要求暴露或依赖自由形式的 `<thought>`，也不把模型自述当作事实证明。

### 不直接采用的内容

- **用“自愈型 Prompt”把已发生矛盾顺水推舟圆掉**：如果正文已经与权威状态冲突，必须拒绝或进入受控修复；后续剧情只有在形成明确、可验证的新事件后才能改变状态。不能用“其实是伪造的”“记错了”等临时解释静默改写历史。
- **异步状态更新器直接覆盖状态**：未通过审查或未提交的草稿不能触发状态写入。状态应从已接受的结果契约、代码投影和确认事件原子更新；异步任务只能做诊断、索引或候选提取。
- **公开思维链作为质量保障**：增加隐式长推理会增加 token、延迟和泄露风险，也不能替代代码校验。需要的是可验证字段与证据引用。
- **双候选并行生成作为首期优化**：它会增加调用和审查成本，且不能解决上下文边界漂移。只有阶段 5 已有稳定基线、并且有明确延迟收益时再做独立实验。
- **固定“总分低于 7 就重写”**：整体 Score 混合了文学偏好和事实判断，不宜直接作为提交闸门。硬冲突应由原子布尔／分类判断和代码组合；文风分数最多作为观测指标。

### 对 Jev 判断的补充边界

Gemini 对话中关于 Jev 的具体延迟、价格、置信度和“降低幻觉 90%”等数字不能直接作为本项目承诺。官方能力、版本、区域可用性、中文表现和接口行为必须在阶段 6 单独验证。特别是 `Noul`、`Choice`、`Score` 的返回字段和置信语义不能从对话中的示例推断。

后续 Jev shadow 试验应只接收最小化的 `ContextBundle` 投影和候选断言，例如：

```text
state = {
  authoritativeState,
  turnIntent,
  allowedEvidence,
  candidateClaims
}
questions = {
  claim_supported,
  action_within_scope,
  state_consistent
}
```

代码负责阈值、缺失值、超时、回退和最终提交；Jev 不能成为上下文来源、状态写入者或文学质量的唯一裁判。

### 本次复核后的优先级调整

阶段 1 增加 `evidence.kind`、`authority`、`visibility` 和 `validity` 的强制校验；阶段 2 增加“检索排除原因”与未来／兄弟分支污染测试；阶段 3 保留 `result_contract` 作为结构化前置检查，不引入公开思维链；阶段 4 将异步状态更新明确排除在正式提交链之外；阶段 5 增加“矛盾是否被错误圆回”的人工指标。阶段 6 才评估 Jev 是否能降低语义复核成本和误拒率。

## 用户确认的 Jev 后续开发顺序（2026-09-22）

用户确认后续按“先规则、再扩大测试、最后决定接入”的顺序推进。该顺序不改变 Jev 的后置旁路边界；在规则和测试门槛通过前，Jev 不进入默认生成、提交或状态写入链路。

### 1. 冻结项目专用的分层审核规则

审核规则必须针对本项目的 `ContextBundle`、StoryPackage、分支状态和玩家回合契约编写，不能直接复用通用的文本评分提示。Jev 只输出结构化判断，代码负责组合、阈值、回退和最终裁决。

分层规则暂定为：

1. **L0 硬规则层**：代码检查权威状态、角色身份与生死、位置和物品、分支可见性、玩家授权范围、未来 beat 泄露、输出契约和必要证据。命中明确硬冲突时，不得被高层分数覆盖。
2. **L1 原子语义层**：Jev 分别判断 `claim_supported`、`action_within_scope`、`state_consistent`，并补充知识越界、未来事实泄露、可修补异常和修复证据是否充分等单一问题。每个问题必须保留原始结果、置信度、版本和输入哈希。
3. **L2 处置层**：代码将 L0/L1 组合为 `allow`、`allow_with_patch`、`rewrite` 或 `reject`。`allow_with_patch` 只能用于有明确修复边界、机制、代价、线索和解释期限的有限异常；Jev 不生成修复正文或状态补丁。
4. **L3 评估层**：记录按判断类型分组的 precision、recall、漏检、误拒、修复后新问题率、调用次数、token、成本、首字延迟和最终确认延迟。综合分数只用于比较模型和版本，不单独作为提交闸门。

规则冻结时必须同时给出正例、负例、边界例和不确定例，明确缺失证据、超时、限流、低置信和解析失败的处理。任何不确定结果都回退现有审查路径，不默认放行。

### 2. 扩大数据与响应测试

规则冻结后，建立可复用的分层测试集并扩大真实回放：

- **语义覆盖**：一致正文、明确状态冲突、角色越权、知识泄露、未来 beat 泄露、身份歧义、物品异常、环境小幻觉、缺少修复支持、混合硬冲突与可修补异常。
- **上下文边界**：短／长候选正文、不同窗口大小、摘要与状态冲突、缺失证据、相邻分支污染、未来事件污染、多人和多物品场景。
- **真实素材**：只使用当前达标的官方十万字以上长篇运行包，保留三类真实基线，并增加连续回放和人工阅读；《雨夜候车室》不重新启用。
- **稳定性与压力**：固定 Jev 版本、问题模板和输入哈希，按判断类型达到预先规定的样本量，重复运行以观察误拒、漏检和长尾延迟；同时记录并发、限流、网络错误和回退行为。

测试必须同时报告 Jev 单调用和完整回合的 P50/P95/P99、首字延迟、最终确认延迟、重写率、每回合调用数和成本。当前 1009ms P50、1282ms P95 只作为起始观测，不能直接视为接入承诺或用户体验结论。

### 3. 接入判定

只有在每类原子判断都有足够样本、相对现有审查的收益可归因、没有新增状态误写／知识泄露／不可解释拒绝，并且完整回合延迟和成本满足项目门槛后，才评估从 shadow 进入受控门控。首次门控仍必须可撤回；超时、限流、不确定或低置信时回退现有路径。
