后续已完成[归属提示修正实测与复审](context-management-attribution-review-2026-09-27.md)：仍为 37 匹配、2 未决、5 失败，文字澄清未带来整体改善。以下保留前轮实验结论。

## 本轮结论

2026-09-27 完成 qualifier-frame/0.1 的固定预算实测：**44 次请求、37 条匹配、2 条范围未决、5 条失败（3 条不匹配、2 条响应无效）**。40 条有限语义用例中为 **37/40**；4 条范围控制中，代词／跨句 2 条正确保留未决，转述 2 条错误提取为直接位置，不能将这 4 条都记作通过或排除后不报告。

前轮三个失败本轮均匹配，条件选择出现可见改善；但人物归属和转述边界仍不可靠，**候选不采用，不扩大到未见场景或生产链路**。全部结果只证明这批固定短句的表现，不是长篇正文质量、来源支持或完整上下文工程验收。此次语义复核由 Codex 逐条阅读原文和响应完成，不替代用户最终验收。

## 执行与证据

- [实测入口](../test_support/context_qualifier_frame_eval.py)；[新增测试](../tests_py/test_context_qualifier_frame_eval.py)；[冻结契约与标签](context-management-frame-contract-review-2026-09-26.md)。原 38 条标签、新 6 条控制、提示及协议均未修改。
- [44 条原始输入、响应和评分](evidence/context-management-2026-09-22/context-qualifier-frame-eval-2026-09-27.json)；[独立离线复算审计](evidence/context-management-2026-09-22/context-qualifier-frame-eval-audit-2026-09-27.json)。审计重验绑定哈希、实际消息、原始响应与评分输入、逐项评分及汇总，复算新增模型调用为 0。
- 使用现有 direct 配置，请求模型 deepseek-v4-flash，44 次响应均标记 deepseek-flash，HTTP 200 且 finish_reason=stop。每例一次，未重试、补跑或改写响应；原始响应先落盘再评分，传输错误会阻断余下请求。
- 官方母本仍为《太虚遗录》102,610 CJK、taixu-relics-part1@0.1.3；这些短句是绑定登记实体的合成控制，不冒充小说原文。正式会话写入、复核记录写入与 Jev 调用均为 0。

## 五项失败与判定

| caseId | 原文及原始行为 | 审核判断 |
| --- | --- | --- |
| control:location_mention | “沈砚秋提到了议事殿。”被答为殿内位置，并以“提到了”为 unresolved_subject 线索 | 实质性虚构位置；已唯一登记的人物又被标为主体未决，触发 evidence_issues。结构拦截不等于模型理解正确 |
| control:mixed_people | “沈砚秋提到议事殿而守门长老在议事殿内。”的长老位置正确，沈砚秋答 word_mention，标签为 no_position | 位置内容正确，但非位置归属子类错误。当前提示“字词或名称本身被提及”存在作用对象歧义：应针对任务人物／名称本身，不能把人物提到地点也混入该类；不更改本轮金标掩盖差异 |
| frame:mixed_ownership | “议事殿内的沈砚秋提到了守门长老。”被提取出两人均在殿内；长老 core 为 S1、S3，跳过 S2 | 将修饰沈砚秋的位置错误转给长老；片段不连续又触发格式拒绝。不能自动填 S2 后把错误位置保留下来 |
| frame:self_report | “沈砚秋低声说：‘我在议事殿内。’”被提取为沈砚秋直接在殿内 | 转述层级丢失；按冻结范围应 unresolved。引用合法、结构有效，仍然语义失败 |
| frame:other_report | “沈砚秋说守门长老在议事殿内。”中长老被提取为直接位置；说话人被归 word_mention | 应为说话人 other_entity、被转述人物 unresolved；同一条有两处归属错误。不能把说话内容升级成现场事实 |

其中 control:location_mention、control:mixed_people 前轮匹配、本轮失败，属于固定集退步。原三个失败 gate:original_condition、gate:both_paraphrase、control:quoted_words 本轮匹配：完整条件前提不再包含结论、双限定未漏条件、说出字词未虚构人物位置。旧失败证据仍原样保留。

同一原 38 条：前轮 33 匹配＋2 未决＋3 失败，本轮 34 匹配＋2 未决＋2 失败；新归属子类比旧协议更细，不能把两个总分直接解释为同一难度准确率提升。原最初 30 条由 28/30 到 30/30；新 6 条为 3 匹配、1 无效、2 转述失败。仍是单次历史对照，不能证明稳定收益。

## 计分分母与逐项复核

48 个任务均被作答，未遗漏、重复或新增任务；**归属分类错误 6 项，落在 5 条失败中**。其中 4 项误答 position，另 2 项是非位置子类错误。任务完整与语义正确分开统计。

42 条结构有效响应可做位置评分，涉及 37 个预期位置：37 个内容匹配，2 个额外位置均来自转述，限定多报／漏报为 0。另 2 条响应无效被整体排除；其中 mixed_ownership 含本批第 38 个预期位置，不能因此声称全部 38 个位置都完成可靠提取。无效行里的错误归属仍计入上述归属错误。

条件依赖计分只针对主体、边界、方位、肯否内容匹配的候选：5 个预期条件单元全部选中，错误 nonPremiseUnits、条件遗漏／多选、依赖未决均为 0；其余 5 次上下文单元选择作为非前提，与逐项原文相符。没有实际 dependency_pending 样本，不以本轮零未决证明未知关系路由有效，该保护仍只有定向测试证据。

逐项复核全部 44 条响应后，37 条匹配行中未发现需要推翻有限范围标签的额外错误，但仍有两个解释边界：

- 4 个山门条件响应的 cue 引用“守门弟子放行／点头”，没有单独引用“如果／只要”；完整 premise 保留了条件连接词，reviewView 也保留完整前提，因此未把条件变成无条件事实。不能单看 cue 判断语义完整，也没有修改金标强制连接词。
- “没有人敢断定”保留为 modality，未被反转为否定位置；这只符合现有粗粒度限定契约，不表示能够区分所有认知、传闻、概率和信念类型。两对完全相同原句在不同 cohort 重复出现，44 条不是 44 个独立语言样本。

| caseId | 实测状态 | 原始回答复核 |
| --- | --- | --- |
| prefix:mention | matched | 保留长老殿内位置；说出的“可能”不作推测。 |
| prefix:operator | matched | 殿内位置保留“可能”限定。 |
| in_core:mention | matched | 动作附带位置保留；字词“可能”不作限定。 |
| in_core:operator | matched | 同片位置的“可能”保留。 |
| unquoted:mention | matched | 写在纸上的字词不限制长老位置。 |
| unquoted:operator | matched | “或许”确实修饰人物位置。 |
| denial:mention | matched | 说出“断定”不构成位置限定。 |
| denial:operator | matched | 保留“没有人敢断定”；未反转位置肯否。 |
| neighbour:mention | matched | 前句字词不转移到后句沈砚秋的位置。 |
| neighbour:operator | matched | 只保留后句位置自身的“可能”。 |
| two_people:first_operator | matched | 长老位置带推测、沈砚秋位置不带；相邻单元均非条件。 |
| two_people:second_operator | matched | 沈砚秋位置带推测、长老位置不带；相邻单元均非条件。 |
| gate:original_condition | matched | 仅 P1-U1 为完整条件前提，不再混入结论单元。 |
| gate:plain_emphasis | matched | “就”未误作推测。 |
| gate:possibility | matched | “可能”保留。 |
| gate:both | matched | 条件与“可能”分别保留。 |
| gate:condition_paraphrase | matched | 完整保留“只要”前提，没有额外推测。 |
| gate:possibility_paraphrase | matched | 封山线外位置保留“或许”。 |
| gate:both_paraphrase | matched | 完整条件与“或许”分别保留，本轮未漏条件。 |
| gate:unrelated_possibility | matched | 前句弟子的可能动作未转移到伤者位置。 |
| gate:unrelated_condition | matched | 前句铁链条件未附着到后句伤者位置。 |
| hall:source_position | matched | 沈砚秋殿外；未受邀是非前提。 |
| hall:negative_position | matched | 不在殿内表示 inside+negative，未改为殿外。 |
| hall:inferred_position | matched | 开门长老位置保留“想必”。 |
| hall:explicit_uncertainty | matched | 沈砚秋殿外保留“大概”。 |
| hall:word_mention | matched | 与 prefix:mention 重复原句；本次独立请求同样正确。 |
| hall:condition_and_inference | matched | 完整“若”前提和“也许”分别保留。 |
| hall:two_people | matched | 同单元两人物位置各自绑定，未互换。 |
| hall:unrelated_uncertainty | matched | 前句开门长老的可能动作未影响沈砚秋否定位置。 |
| hall:denied_certainty | matched | 与 denial:operator 重复原句；保留无法断定前缀。 |
| control:location_mention | invalid_response | 失败：提到地点被编为 inside，还将已登记主体标为未决。 |
| control:object_position | matched | 木盒的位置没有转给沈砚秋。 |
| control:quoted_words | matched | 仅说出字词，返回 word_mention；本轮未虚构位置。 |
| control:mixed_people | mismatched | 失败：长老位置正确；沈砚秋的 no_position 错分 word_mention。 |
| scope:pronoun | scope_pending | 空任务按边界保留未决；不代表代词语义已解决。 |
| scope:cross_sentence | scope_pending | 空任务按边界保留未决；不代表跨句指代已解决。 |
| control:adjunct_mention | matched | 抄写“或许”未抹除自身殿外位置，也未增加推测。 |
| control:negative_position | matched | inside+negative 正确。 |
| frame:name_words | matched | 名称文字在纸上，未提取为人物位置。 |
| frame:portrait | matched | 画像位置没有转给长老。 |
| frame:mixed_ownership | invalid_response | 失败：沈砚秋位置正确，但把同一殿内位置错误转给长老。 |
| frame:position_and_quote | matched | 保留叙述明确的殿内位置，没有转移引文中的殿外位置。 |
| frame:self_report | mismatched | 失败：自述位置未保持 unresolved，而被当作直接位置。 |
| frame:other_report | mismatched | 失败：转述中长老位置被直接确认，说话人也分错类别。 |

## 实际成本

| 同一 38 条比较 | 旧覆盖协议 | 本轮框架契约 | 变化 |
| --- | ---: | ---: | ---: |
| 输入字符 | 98,459 | 78,883 | −19.88% |
| 输入 token | 44,640 | 35,474 | −20.53% |
| 输出 token | 4,950 | 4,278 | −13.58% |
| 总 token | 49,590 | 39,752 | −19.84% |
| 请求耗时中位数 | 1,003.5 ms | 947 ms | −56.5 ms |

全部 44 次本轮消耗为输入 41,105、输出 4,851、合计 45,956 token，输入字符 91,381，中位耗时 930 ms；usage 与 transport 耗时记录均为 44/44。这里是请求耗时，不是正文首字或完整游玩延迟；跨时段单轮对照不足以作时延因果结论。

原最初 30 条本轮输入／输出／总 token 为 28,297／3,758／32,055；相较更早无覆盖协议的 28,915／3,469／32,384，输入略降、输出增加，总量接近。不能只选更重中间方案作为基线，宣称整个上下文工程已有同幅度成本收益。

## 验证与后续清单

新增 10 项定向测试通过（51.183 秒），覆盖标签隔离、归属／依赖独立分母、未决候选扣留、虚构与遗漏位置、畸形分类、固定预算、排他输出、原始响应先落盘、保存失败和传输失败停止、失败不重试及证据篡改／绑定漂移。

核心回归 **523/524**（421.590 秒）；唯一失败为 test_prompt_catalog.test_original_expression_baselines 中既有 consequences.plan 提示哈希不一致，本轮没有修改该提示或基线。10 项新增测试与此前 15 项框架契约测试均纳入这次核心回归。新增 Python 语法／空白、文档链接、绑定文件哈希及 `git diff --check` 检查通过。本轮未改 API／前端，未重复运行其套件。

后续按以下顺序执行，继续保留推进、审核、修复、复审循环：

1. **先修归属规则的表达，暂不扩展协议字段。** 新建版本化候选提示，替换现有 attribution 段落；明确“任务人物的位置”与“人物提到了某个地点／人名”的区别，区分直接叙述与被说出的命题，以及修饰语实际指向谁。保留条件协议、标签和本轮原始证据。归属类别定义的歧义是当前发现，不能把所有失败都归咎于模型。
2. **先离线复审候选，再给独立固定预算。** 检查规则是否互斥、转述优先级是否明确、任务仅表示共现候选而非存在位置；不得以出现“说”、引号或“提到”的正则直接决定语义。输入仍为同一原文、实体出现与有界任务，不追加旧错误、全历史或整份台账，不扩大字符预算。旧 44 条原金标完整保留，重点观察本次 5 失败及原 3 失败的双向退步。
3. **归属复测未通过就保持隔离。** 未决不计语义通过；合法引用也不代表来源支持。不使用补齐 core、删掉错误人物、替模型补条件等后处理让结果通过，不以降低金标要求消除分类错误。
4. **固定集稳定且复审无实质错误后**，才冻结来自其他官方入口的未见场景，单独验证提取忠实度与来源支持，随后回到完整正文、三层上下文投影和提交链路的目标。当前实验尚不能替代这一步。Jev 继续后置。

本轮不新增第二轮模型调用，不修改 .env、公司中转、生产代码、正式数据库或官方小说／StoryPackage。
