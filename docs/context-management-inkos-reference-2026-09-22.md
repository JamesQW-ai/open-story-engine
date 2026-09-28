状态：参考记录；不代表 InkOS 代码已引入，也不代表本项目正文质量已经验收。

## 目的

本记录用于在上下文管理工程优化期间并行保留 InkOS 的可借鉴机制。参考对象为 `Narcooo/inkos` 当前公开仓库实现，重点关注正文生成前后的输入治理、预算降级、审校修复、持久化和可观测性。

InkOS 的系统边界与本项目有相似之处：模型生成候选内容，宿主系统控制上下文、状态、审校和持久化；长文流程包含写作、审校和有限修订。它的价值主要在于提供工程模式和检查清单，不足以替代本项目针对 StoryPackage、分支状态、角色限知和玩家行动边界的设计。

参考链接：

- [InkOS README](https://github.com/Narcooo/inkos#readme)
- [input-governance.ts](https://github.com/Narcooo/inkos/blob/master/packages/core/src/models/input-governance.ts)
- [composer.ts](https://github.com/Narcooo/inkos/blob/master/packages/core/src/agents/composer.ts)
- [chapter-review-cycle.ts](https://github.com/Narcooo/inkos/blob/master/packages/core/src/pipeline/chapter-review-cycle.ts)
- [LICENSE](https://github.com/Narcooo/inkos/blob/master/LICENSE)

## 逐项映射

| InkOS 机制 | 可借鉴点 | 与本项目 `ContextBundle` 的对应 | 当前决定 |
| --- | --- | --- | --- |
| `ChapterIntent` | 将本章必须完成、必须避免和文风偏好结构化 | `turnIntent`、`hardConstraints`、`outputContract` | 借鉴结构；改为玩家回合语义，不直接复用字段 |
| `RuleStack` | 规则分层、优先级和有限覆盖边 | 状态／确认事件 > 当前 beat 契约 > 玩家原子意图 > 公开证据 > 连续性摘要 > 文风 | 借鉴优先级模型；覆盖关系由本项目代码固定，不让模型声明覆盖 |
| `protected / compressible` | 预算不足时保护事实和任务约束，只处理软内容 | `hardConstraints`、状态、当前 beat、必要证据为 protected；文风、旧摘要和无关软证据可压缩 | 建议直接纳入预算设计 |
| `ChapterTrace` | 记录来源选择、预算、压缩和阶段输入 | `provenance`、`contextSha256`、阶段投影、审计字段 | 建议纳入；本项目需要额外记录排除来源和排除原因 |
| FTS5/BM25 检索 | 把可检索记忆作为重建的搜索投影 | 只能作为 StoryPackage 受控模块或非权威文风／参考材料的候选检索 | 暂不进入关键路径；首期保持确定性选择，不引入向量检索 |
| LLM semantic selector | 在有限候选中辅助选来源 | 可作为后续 shadow 或低风险软材料候选排序 | 暂不作为事实或分支选择的裁决者 |
| 上下文压缩器 | 对可压缩材料做预算适配 | 只允许处理旧连续性摘要、文风和软证据 | 不允许模型摘要覆盖状态、事实、当前 beat 或行动边界 |
| 审校—修订循环 | 审校输出问题，修订轮数有界，可保留最佳快照 | `grounding_review`、`repair` 投影与 P0/P1/P2 检查 | 借鉴有界修复和问题范围；不把综合分数当作事实验收 |
| `repair_scope` | 区分局部、结构性和未知修复范围 | `repair` 阶段的局部修复契约 | 建议采用；未知范围必须受控失败或转人工，不允许无边界重写 |
| atomic file set | 正文、状态和账本尽量同一提交边界 | accepted prose + state projection 的最终写入 | 借鉴提交协议；上下文快照和审计仍需单独记录，不能声称全链路天然原子 |

## 对现有设计的补强

InkOS 最适合补充当前设计的四个工程细节：

1. 在 `allowedEvidence` 中增加显式的 `tier` 或等价字段，区分不可压缩、可压缩和诊断材料，并将此字段纳入规范化哈希。
2. 为每个阶段投影记录 `selectedSourceIds`、`excludedSourceIds`、预算估算、压缩结果和省略原因，让审计能解释一次正文调用实际看到了什么。
3. 将审校问题映射为有限的 `repair_scope`，例如句段局部、结构边界、事实支撑和未知范围；修复提示只能消费原 bundle 与明确问题。
4. 将“保护内容超预算”定义为零次模型调用的受控错误，避免无限重试、模板填充或模型摘要掩盖最小有效上下文不足。

这些补强不能削弱当前设计已有的边界：`branchId`、`visibility`、`location`、`authority`、`validity`、`contextSha256`、未来／其他分支排除和状态优先级仍然是本项目自己的必需字段。[上下文管理质量设计](context-management-quality-design-2026-09-22.md) 和 [上下文管理执行计划](context-management-development-plan-2026-09-22.md) 继续作为实现依据。

## 暂不采用的部分

### 通用长文评分作为最终质量门槛

InkOS 的审校维度和评分适合辅助长篇章节审阅，但本项目的首要质量问题是行动契约、角色知识边界、状态投影、事实依据和停止点。评分提高不能证明正文没有越权或状态冲突。因此，综合文学评分最多作为软信号；P0/P1 硬检查必须先通过。

### LLM 决定权威检索和上下文压缩

模型可以遗漏候选、带入未来信息或把相似文本误当作当前分支事实。当前阶段继续采用确定性来源选择，模型只处理已经被代码筛选并标注权限的输入。未来如实验 semantic selector，也必须限制在软材料并保留候选全集、选择结果和回退路径。

### 反检测、文风重写和全章重写模式

这些模式与当前玩家回合的质量目标无直接关系，且可能在修复时改动事实、行动和人物决定。当前只接受可定位、可审计的局部修复；需要重新规划时创建新上下文版本，而不是无边界重写。

## 许可证与直接复用限制

InkOS 使用 `AGPL-3.0-only`。因此本项目不直接复制、改造或嵌入其 TypeScript 模块；尤其不把 `composer`、审校器、检索器或运行时写入器作为本项目依赖。当前采取“参考机制、独立实现”的方式。若未来需要代码级复用，必须先完成许可证评估并明确隔离、发布和网络服务源码义务。

## 后续实验与验收指标

InkOS 参考点应与上下文管理优化并行记录，但实验变量要保持可归因。建议按以下顺序验证：

| 实验 | 变量 | 必须记录的指标 | 通过条件 |
| --- | --- | --- | --- |
| 规则分层 | 引入 `hardConstraints`／`turnIntent` 投影 | 首稿通过率、行动越权、停止点越界、同输入哈希稳定性 | 同一输入选中 ID、排序和哈希稳定；P0 越权不增加 |
| 预算分层 | protected/compressible 降级顺序 | protected 删除次数、软内容省略数、受控失败次数、输入 token | protected 永不静默删除；最小上下文超限时零次模型调用 |
| 追踪完整性 | `ChapterTrace` 对应的 bundle 审计 | 选中来源、排除来源、排除原因、阶段 `contextSha256` 完整率 | 每次规划、正文、抽取和复核都可追溯到同一 bundle 或显式子版本 |
| 有界修复 | `repair_scope` 和最多一轮局部修复 | 修复后硬检查通过率、新增事实／状态错误、模型调用次数 | 修复不新增 P0/P1 错误；未知范围不自动重写 |
| 持久化边界 | accepted prose + state 原子提交 | 正文缺失、状态前进但正文缺失、正文存在但状态未落盘 | 失败不产生半提交；快照、审计和运行索引的非原子边界单独可见 |
| 真实长篇回放 | 现有模型与优化前基线对照 | 首稿有效率、最终写入率、知识泄露、状态冲突、P95 延迟、输入 token | 使用达标官方长篇和固定场景；mock 通过不能替代真实模型验收 |

真实回放至少覆盖“告知、询问依据、短等待”三类场景，并保留失败样本。任何指标改善都要同时检查是否通过放宽事实校验、增加模板正文、无限重试或隐藏拒绝来获得。

## 结论

InkOS 对本项目的**机制参考价值高**，特别是 `RuleStack`、protected/compressible budget、`ChapterTrace`、有界审校修复和原子提交边界；对其代码的**直接复用价值低且受 AGPL-3.0-only 限制**。本项目应继续以现有 `ContextBundle` 设计为主，将 InkOS 参考点作为实现补强和实验清单，不替换分支、可见性、证据权威、上下文哈希和阶段投影这些更严格的产品边界。
