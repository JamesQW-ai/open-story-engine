本轮接续[归属提示替换实验](context-management-attribution-review-2026-09-27.md)，只做断言来源表示的离线实现与复审。结论：能表达此前混在一个 attribution 中的部分职责，但目前不足以冻结为模型契约，不能进入生产，也没有证明正文质量改善。

## 范围与证据

- 复用官方长篇 `taixu-relics-part1@0.1.3` 的登记实体及原 44 条诊断原文，原文哈希与旧标签逐条校验；不改官方母本、StoryPackage 或旧实测文件。另有一条同人物双来源的设计测试，不计入 44 条或模型成绩。
- [隔离原型](../test_support/context_assertion_scope.py)、[离线回放工具](../test_support/context_assertion_scope_replay.py)、[手写控制](../test_support/fixtures/context-assertion-scope-controls-2026-09-27.json)、[定向测试](../tests_py/test_context_assertion_scope.py)。没有新增模型提示或调用入口。
- [离线报告](evidence/context-management-2026-09-22/context-assertion-scope-replay-2026-09-27.json)绑定前序证据、新模块和控制夹具哈希；[重建审计](evidence/context-management-2026-09-22/context-assertion-scope-replay-audit-2026-09-27.json)逐项复算。原 37 匹配、2 范围未决、5 失败仍是前序模型结果。
- 新模型调用、正式会话写入、正式复核记录写入、Jev 调用均为 0。手写响应不进入模型输入，也不作为模型成功案例。

## 替代表示的职责

顶层改为 `assertion-scope/0.1`，每个主体任务选择 `positions`、`absent` 或 `unresolved`；不与旧五类 attribution 同时发送。输入仍只有原有的 `source/entityRefs/tasks`，原文只保留一份，隐藏标签不进入输入。

`positions` 的每个候选保留边界、位置、核心片段、条件、其他限定及依赖划分，并增加 `origin={speaker,scope}`。来源属于每个位置候选，不属于整个人物任务，因此同一人物可同时具有直接叙述候选和转述候选。`absent/unresolved` 绑定当前主体单元内的 `basis`；这是响应选择及其引用范围，不是程序证明不存在位置。

| 字段 | 当前含义 | 程序能检查的内容 |
| --- | --- | --- |
| subject | 被描述位置的人物引用 | 属于原任务、唯一、任务完整 |
| core | 候选命题的原文片段 | 引用有效、连续且有序，含主体及边界 |
| origin.speaker=null | 模型提出的叙述来源候选 | 仅检查结构，不能证明不是转述 |
| origin.speaker=人物引用 | 模型提出的转述来源 | 当前非歧义登记人物，scope 包含其锚点 |
| origin.scope | 声称包含来源及命题的范围 | 连续、有序，不越出当前标点范围；覆盖核心、主体及完整限定证据 |
| conditions/modifiers | 条件前提和其他限定 | 条件选择完整前提单元，线索精确，依赖不重叠、不遗漏 |

不存在已知唯一登记说话人、涉及嵌套转述或无法选择来源时，契约要求使用 `unresolved`。当前实现没有推断这些情况，也不能保证模型遵守这一语义要求。`speaker=null` 不能充当“未知说话人”的安全替代值。

原型借用旧框架检查引用与依赖，未调用旧语义评分器。内部的格式投影不迁移历史回答、不补洞、不自动剥离 ID 后缀。返回结果始终为 `semanticStatus=unverified`、`acceptance=false`、`productionEnablement=false`；包括叙述候选在内都不提供 `decodedProposal` 或可提交状态补丁。

## 可表达性检查

44 条手写控制均没有结构无效响应，计数如下；这些计数不叫“模型匹配率”。

| 项目 | 数量与解释 |
| --- | --- |
| structurally_valid | 26 条，仅通过引用和结构检查 |
| scope_pending | 18 条：2 条转述，16 条有继承的 pendingUnits |
| 叙述位置候选 | 38 个 |
| 转述位置候选 | 2 个 |
| absent 决策 | 8 个，来自旧非位置子类的合并 |
| unresolved 决策 | 控制集为 0；单独测试覆盖该路径 |

16 条 `pendingUnits` 表示原覆盖索引没有为这些单元建立“登记人物＋登记边界”的完整任务，包含条件、邻接描述和两个指代控制。它们不全是语义失败，条件单元也可能已被候选引用。本原型保守保留这些待审标志，所以新的 26/18 状态不能与旧 40/4 或实测 37/2/5 直接比较。

两条转述控制完整保留原文包裹范围：

- `frame:self_report`：沈砚秋低声说：“我在议事殿内。”主体与 speaker 为同一引用，仍作为转述候选，不能因人物相同而升级。
- `frame:other_report`：沈砚秋说守门长老在议事殿内。主体是长老，speaker 是沈砚秋，核心为 `P1-U1-S2`，scope 为 `P1-U1-S1` 与 `P1-U1-S2`；漏掉前一个片段会被拒绝。说话人的任务为 absent，不向说话人转移长老的位置。

单独的设计测试“沈砚秋站在议事殿内并说‘我在殿外’”可在同一任务中保留 inside 的叙述候选和 outside 的转述候选。但目前整个句子只有一个片段，两候选的核心和范围重合。这证明列表能容纳两种来源，不能证明粒度足以区分两个子句，更不证明自动提取正确。

## 复审发现与未闭合问题

1. **引用合法不等于来源正确。** 测试故意将他人转述的 speaker 改成 null，结构仍可通过；故意将确有位置改为 absent，也可通过结构检查。结果必须留在语义未验证状态。不能依据 `narratorCandidates` 名称直接写入事实状态。
2. **旧细分类不可比较。** word_mention、other_entity、no_position 被折叠为 absent，旧 unresolved 在两个转述控制中被手工细化。原标签仍完整保存，但本轮不计算这三个子类的准确率，也不把原 6 项归属错误宣布修复。下一步需独立评分，防止合并标签掩盖问题。
3. **范围粒度尚不足。** origin.scope 使用已有片段，完整保留原文，却未必能单独绑定说话动词、引文及直述子句。不能仅凭包含人物锚点就认定它确为话语来源；嵌套来源和无登记说话人尚未解决。
4. **单元与片段 ID 混用未修复。** 旧实测 two_people:first_operator 的错误移入新响应后仍被拒绝。本轮没有调整输入候选表示，不宣称新协议能让模型更可靠地复制 ID。
5. **响应成本增加。** 同一紧凑 JSON 序列化下，44 条输入合计 21,228 字符，与旧 user 输入对象逐条相同；无输入减少。手写响应从旧控制的 12,579 增至 15,055 字符，增加 2,476（约 19.7%）。两者均为手写控制，不是实测 token、模型响应长度或时延比较；新提示及输出预算未冻结。

因此 `readyForModelEvaluation=false`。本轮只完成最小可执行原型和风险暴露，未完成协议语义验收。原五项实测失败全部保留，没有用手写正确回答覆盖失败。

## 验证

新增 11 项定向测试通过；与旧框架及归属实验相关回归合计 **33/33**（151.658 秒）。实际离线生成与独立重建审计通过，原始实测证据及新输入哈希已复核。测试覆盖完整原文输入、转述包裹范围、条件和依赖、来源与缺失误判反例、双来源表达、旧无效 ID 保持拒绝、夹具哈希漂移和审计篡改。运行命令：

```sh
python3 -B -m unittest tests_py.test_context_assertion_scope tests_py.test_context_qualifier_frame tests_py.test_context_qualifier_attribution_eval -v
python3 -B -m test_support.context_assertion_scope_replay
python3 -B -m test_support.context_assertion_scope_replay --audit
```

两条回放命令使用排他写入；产物已存在时应调用 `audit()` 只读复核，不能覆盖旧证据。

本轮仅修改隔离的 test_support、tests_py 和文档，未改生产 Python、API 或前端，不重复执行 API／前端全量回归。前轮核心 530/531 仅作为历史结果保留，唯一既有 consequences.plan 哈希失败未重设基线；不将历史通过数写成此次全量回归结果。

## 后续按序推进

1. 先完善离线评分契约：分别核对位置主体、位置内容、来源层级、遗漏／多报、错误弃答；旧非位置三子类保持独立诊断与明确的不可比项。原 44 条只作固定诊断，不充当未见留出集。
2. 比较能够定位来源子句的最小范围表示及统一 ID 选择方式，明确嵌套／未知来源的拒绝边界；不得新增一份全文或自动替模型修正语义。离线检查正确与错误控制，同时计量输入、响应和提示长度，不能为通过测试无限增加字段。
3. 只有评分映射、范围粒度、ID 契约与预算复审通过，才冻结候选并进入一次有界模型实验；记录全部失败，和原始指标分别报告。当前没有启动实测。
4. 固定集稳定后，再进入未见官方场景、来源支持、其他事实类型与完整正文／提交链路。三层上下文的真实质量收益仍以完整链路验证为准，生产接入和 Jev 后置。

后续[参考评分实现与复审](context-management-assertion-scoring-review-2026-09-27.md)已将主体、内容、来源、限定及范围差异分别记录，并检查故意错误控制。旧子类仍不可直接评分，唯一参考范围不等于语义等价判定；该后续没有改变本轮结构控制或前序模型成绩。
