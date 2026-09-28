本轮完成[断言来源原型](context-management-assertion-scope-review-2026-09-27.md)的离线参考评分工具和反例检查。它能将结构有效但与固定参考不一致的候选分项报告，不是运行时语义审核器。模型调用为 0，正文质量、模型准确率和时延收益仍未验证。

## 实现与证据边界

- 新增[评分器](../test_support/context_assertion_scoring.py)、[回放与反例工具](../test_support/context_assertion_scoring_replay.py)及[回归测试](../tests_py/test_context_assertion_scoring.py)。原型、旧标签、模型输入、提示及历史证据均不改动。
- 评分参考复用已冻结的 44 条手写控制，绑定 caseId、实际正文 SHA-256 和旧标签；不让模型生成自己的评分参考。仅用于该登记实体与同单元任务范围，不是通用事实判断。
- [评分回放](evidence/context-management-2026-09-22/context-assertion-scoring-replay-2026-09-27.json)保存逐条结果和全部故意错误回答；[重建审计](evidence/context-management-2026-09-22/context-assertion-scoring-replay-audit-2026-09-27.json)绑定源码、参考夹具及前序证据。重建须逐项相同，旧产物禁止覆盖。
- 所有输出保持 `semanticStatus=unverified`、`acceptance=false`、`productionEnablement=false`，不产生可提交的状态补丁，也不自动迁移旧协议回答。

## 评分顺序与口径

先调用已冻结的原型检查引用与结构；无效回答标记 `invalid_response`，各维度为 null，不能把“未能评分”显示成零错误。没有登记主体／边界任务的两个指代控制标记 `out_of_scope`，不因两个空数组相等就算成功。

其余回答与固定参考比较，使用保留重复次数的多重集合。任务和候选顺序变化不影响结果；重复候选仍计为多报；条件和其他限定列表按结构归一排序，但不合并重复项、不填补证据、不改写命题。字段对应如下：

| 维度 | 比较对象 | 能揭示的差异 |
| --- | --- | --- |
| decisions | 主体、positions/absent/unresolved 及非位置 basis | 漏报、错误弃答、错误无位置决定、依据范围差异 |
| subjects | 每个主体拥有的候选数量 | 人物候选丢失或增加；不单独代表归属准确率 |
| positionBindings | 主体＋边界＋位置值和极性 | 内容错误、主体与位置配错、候选遗漏／多报 |
| originBindings | 上述位置绑定＋speaker | 转述升级、错误说话人、来源配错 |
| qualifierBindings | 上述位置和来源绑定＋条件／其他限定／依赖划分 | 条件或推测丢失、限定及依赖挂错候选 |
| rangeBindings | 上述位置和来源绑定＋core／scope | 原文范围与参考不同 |
| fullBindings | 完整候选绑定 | 防止各字段数量相同但相互组合错误 |

每个维度分别列出 expected、actual、matched、missing、extra 及具体缺失／多出的值。维度之间有关联：主体或来源错误也会影响其下的限定和范围，**不能把所有维度的错误相加当成独立错误总数**。

额外单列 `unexpectedAbstentions`、`falseAbsences`、`unexpectedPositionTasks` 和 `reportedAsNarrator`。最后一项统计同一主体／边界／位置下，参考有转述而回答增加了叙述候选的数量上界受参考转述数限制；它能暴露升级风险，不能证明候选间的因果配对，也可能包含“保留转述同时多报直述”。

总体 `reference_match/reference_mismatch` 仅表示是否与当前固定参考一致，不命名为 semantic_pass。程序不能证明任意一句话的真实语义，也不能由位置叙述候选直接确认世界状态。

## 旧标签的可比与不可比项

原 48 个任务的旧标签全部保留，逐任务显示原类别、参考 resolution、实际 resolution；`predictedLegacySubtype=null`、`subtypeScored=false`。

| 旧类别 | 任务数 | 新参考中的表达 | 比较边界 |
| --- | ---: | --- | --- |
| position | 38 | positions，绑定主体、内容、来源及限定 | 可检查参考绑定，不把新口径与历史准确率直接拼接 |
| no_position | 2 | absent | 可发现错误 positions 或 unresolved，不能证明模型区分了三个非位置子类 |
| other_entity | 4 | absent | 保留独立诊断组，不把人物画像／物品位置转成人物位置 |
| word_mention | 2 | absent | 保留独立诊断组，不把字词提及当作断言 |
| unresolved | 2 | 手工明确为转述来源的 positions | 属于新参考细化，不能宣称模型解决了历史 unresolved |

新协议没有输出三个非位置子类，因此无法从 absent 反推出模型作了哪个细分类判断。旧细分类评分数明确为 **0**，原实测 **6 项归属错误及五项失败仍保留**。本轮没有把缺少的预测字段用参考标签填回去，也没有向模型输入堆入一套旧分类。

## 正反控制与复审

44 条参考自检中，42 条与自身一致，2 条因无登记任务排除。这只验证参考可被评分；**不是 42/44 的模型成绩**。它与上一轮 26 条结构有效、18 条带待审范围的口径不同：本轮仍保留 pendingUnits/pendingDependencies，参考一致不解除这些标志。

13 条故意错误控制中，10 条通过结构检查后被识别为参考不一致，3 条由原结构检查拒绝：

- 新评分分项检出：两种转述升级、说话人错误、引用合法的人物位置转移、虚构位置、删去位置、不必要弃答、删去条件、位置内外翻转、重复候选。
- 原结构检查拒绝：把多个人物合入一个不合规 core、历史实测中的片段 ID 冒充单元 ID、缺少任务。未将这些拒绝冒充新增评分器的语义能力。
- 单独测试同一人物的直述与转述候选互换来源：候选数量与位置内容仍相同，来源绑定必须不匹配。该测试复用官方长篇登记实体，仅作设计控制，不扩充模型评测成绩。
- 单独测试缩窄一个合法的 absent 依据范围：结构可通过，但与唯一参考不同，因此得到 reference_mismatch。这是当前评分边界，不能据此认定该回答语义错误。

开发检查发现并修正了一个反例构造问题：原参考位置已经是 outside，直接赋 outside 没有产生错误。现在按原值翻转，并检查每个故意错误回答确实不同于参考。原诊断标签及任何旧实测结果未因此改写。

## 验证与下一步

新增 8 项测试与原型相关 11 项合计 **19/19 通过**（19.935 秒），覆盖结构失败不计分、空任务排除、绑定错配、顺序不敏感、参考漂移、证据篡改及零网关调用。命令：

```sh
python3 -B -m unittest tests_py.test_context_assertion_scoring tests_py.test_context_assertion_scope -v
python3 -B -m test_support.context_assertion_scoring_replay
python3 -B -m test_support.context_assertion_scoring_replay --audit
```

实际证据生成、独立重建审计、全部历史／新增输入哈希绑定核验均通过；Python 语法、新增文件空白、文档链接及 `git diff --check` 检查通过。未重跑全量核心、API 或前端套件；未修改生产代码。历史 consequences.plan 哈希基线失败不在本轮修复范围。上述两条 CLI 采用排他写入，产物存在后用 `audit()` 只读复核，不覆盖旧文件。

本轮完成的是可执行的参考比较与明确的评分边界；尚不能冻结最终语义评测契约。`readyForModelEvaluation=false`，后续依次为：

1. **来源子句与 ID 表示复审。** 用最小范围表示区分同片段里的直述和引语，确定单位／片段 ID 的可靠选择方式；保留未知与嵌套来源的未决边界，不复制全文、不替模型推断或修复语义。
2. **随后冻结评分参考与容许变化。** 当前只有一种手写范围，合法但不同的引用只能报告参考差异。明确可接受替代范围及独立核验方式；旧子类不可比状态必须继续展示，不能通过降低分母声称旧错误消失。
3. **检查提示及输入／输出预算。** 上一轮手写响应增加约 19.7% 的风险仍未解决。范围方案、评分口径与预算复审通过后，才执行一次有界模型实验，完整保留失败。
4. 未见官方场景、授权来源核验、其他事实类型及正文／提交全链路依次后置。当前评分工具不替代三层上下文质量验收，生产接入与 Jev 仍未进入。

后续[来源定位与 ID 表示比较](context-management-assertion-locator-review-2026-09-27.md)已完成两种定位编码的离线检查；它提供更细的原文定位预览，没有改变本轮评分口径或把定位成功记作模型语义通过。
