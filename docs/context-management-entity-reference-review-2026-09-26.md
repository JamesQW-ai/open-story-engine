后续实测及当前计划见[整单元条件前提审核记录](context-management-unit-premise-review-2026-09-26.md)：结束标记版为 15/18，整单元前提实测为 17/18，仍有词语提及误报；下一步是同场景成对诊断。本文保留实体引用阶段的实现及其 17/18 历史证据。

## 实体引用阶段结论

本阶段验证了 0.2 修正版：**18 次真实请求，严格匹配 17/18，仍未通过固定集验收**。失败是 `gate:condition_paraphrase` 的条件线索引用错位，同时漏选条件前缀；不能只把它归为格式问题。原始响应及评分全部离线复现，未重跑挑选成功。

已继续完成“明确片段结束边界”的离线实验入口，只调整原文展示，不改实体身份、切分、解码、标签或评分。**该展示调整尚未实测，不计入 17/18。** 本次续作只新增上述 18 次调用；与首版合计 36 次，不进入新场景或生产。

以下首版 7/18 和最初离线状态保留为阶段记录；最新结果及下一步见文末。

## 首版实现与实际失败

[实体出现引用](../test_support/context_qualifier_entity_refs.py)按原文片段寻找登记称呼，为每次出现生成单独引用 ID。ID 绑定原稿与称呼表的摘要；更换原文或称呼表会使旧 ID 失效。它是实验内的引用绑定机制，不是授权凭证。

同一实体的重叠别名保留更完整的称呼，重复出现保留各自 occurrence；共享称呼的多个候选仍标为歧义。主体、边界类型必须匹配，未知引用、自由引文对象、歧义引用和其他原文的 ID 都拒绝。最多 256 个实体出现引用，超过预算拒绝，不截断正文。识别只限登记称呼和既有片段，不等于通用实体消歧或语义解析。

模型返回 `{"qualifier-entity-refs/0.1":[...]}`：协议版本是必填结果容器的键，缺少、错名、多余键、非数组以及旧协议响应都拒绝。这是显式的新传输契约，不是为旧响应补写 schemaVersion。

首版仍保留 `position.subject/object` 实体 ID，与外层 `subject/boundary` 引用 ID 并存。**这是本轮设计引入的冗余**：文字解释没有稳定地区分两套身份表示，10 条响应在 position 内填写了引用 ID。另有 2 条写入非空 condition，其中 1 条同时混用 ID；并集为 11 条失败。

| 观察 | 真实结果及审核边界 |
| --- | --- |
| 版本容器 | 18 条都保留正确的必填容器；仅是单轮观察，不证明永远不会漏字段 |
| 实体出现引用 | 本轮选择的主体和边界引用均可定位，原“殿外”引文问题没有以相同形式出现；多数条目仍因重复身份字段失败，不能宣布完整修复 |
| 位置解释 | 10 条把引用 ID 写入 position.subject/object，严格关系校验拒绝 |
| 条件句 | `gate:original_condition`、`gate:both_paraphrase` 还写了非空 condition，漏选条件前缀片段，并把“如果”或“只要守门弟子点头”引用到不包含该文字的片段；不能只报告 ID 格式问题 |
| 7 条匹配 | 山门 3 条、议事殿 4 条，含 8 个位置命题；逐项读原文与响应，未发现新的身份/范围问题，仍不等于来源支持或生产验收 |

首版证据：[原始响应](evidence/context-management-2026-09-22/context-qualifier-entity-refs-2026-09-26.json)、[离线复算](evidence/context-management-2026-09-22/context-qualifier-entity-refs-audit-2026-09-26.json)。实际输入、原始接口内容、实现和依赖哈希、逐条评分及汇总均复现。格式检查可能先于范围检查退出，上述条件句问题是逐项复审补充，不冒充评分器已自动检出。

## 0.2 修正版实现（最初离线阶段）

[修正版解码](../test_support/context_qualifier_entity_refs_v2.py)、[提示](../test_support/prompts/context_qualifier_entity_refs_v2.md)和[下一轮入口](../test_support/context_qualifier_entity_refs_v2_eval.py)使用 `qualifier-entity-refs/0.2`：

1. `subject/boundary` 仅选择原文出现引用。可见引用表只有 `kind/ambiguous/segmentId/quote/occurrence`，内部 entityIds 不发送给模型。
2. `position` 必须且仅含 `value/polarity`；拒绝重复身份、relation、time、condition 等字段，不静默删除多余字段。无法解析可保留 null，但不计完整提取成功。
3. 程序检查引用唯一性和类型后，从所选引用导出实体 ID；按本实验既定关系契约生成 `boundary_side/snapshot/condition=null`。这是新协议声明的确定性解码，不是根据自然语言猜身份、补缺失引用或修补旧模型响应。
4. 解码后继续复用上轮严格评分：位置内容、完整命题、精确条件前提、限定类型、引用与 core 的关系都必须通过。有限定仍为待复核，候选仍无事实确认权限。
5. 旧 0.1 响应一律拒绝，不迁移历史输出来计算新版分数。保留完整 `proposedResponse`，另存 `decodedResponse`，方便核对输入选择与程序派生值。

最初离线阶段尚无正式实验输出文件；测试中的 18 条模拟响应只用于验证调用上限、保存和复算流程，不能记为 18/18 真实模型验收。随后已按每条一次、总上限 18 完成下文实测，未覆盖旧证据。

## 首版与最初离线阶段的验证与成本

- [首版测试](../tests_py/test_context_qualifier_entity_refs.py)和[修正版测试](../tests_py/test_context_qualifier_entity_refs_v2.py)共 21 项全部通过，覆盖引用定位、别名重叠、重复出现、共享称呼歧义、错类型/错原文、位置及前提退化、旧协议拒绝、顶层容器、标签隔离、输入字段收敛和模拟审计。最终核心回归 **429/430**，唯一失败仍是已有的 `consequences.plan` 提示哈希基线，未改基线。
- `git diff --check`、新增文件空白及文档链接检查通过。首版 7/18 与更早严格版 16/18 均重新离线复算一致。无 API/前端变动，未重复相应回归。
- 真实实验为 `direct / deepseek-v4-flash`，返回模型 `deepseek-flash`；18 次 HTTP 200，finish reason 全为 stop，无重试。输入 19,772 token、输出 3,169 token，共 22,941；中位请求耗时 1,112 ms。格式成功或 HTTP 成功不等于内容通过。
- 首版 system+user 总长 43,374 字符，比上轮 38,000 增加约 14.1%。离线修正版装配为 41,620 字符，比首版减少约 4.0%，仍比上轮增加约 9.5%。实体引用表替换称呼表，没有增加历史材料、答案、旧响应或隐藏状态；这些是装配测量，不证明正文质量或稳定延迟改善。
- 测试继续绑定官方《太虚遗录》102,610 CJK 母本与 `taixu-relics-part1@0.1.3` 的两个已见场景。没有更改官方包、母本、旧标签、生产代码、`.env`、中转站或正式数据库，没有正式会话/复核记录写入或 Jev 调用。

## 0.2 真实实验与逐项复审

证据：[原始响应](evidence/context-management-2026-09-22/context-qualifier-entity-refs-v2-2026-09-26.json)、[离线复算](evidence/context-management-2026-09-22/context-qualifier-entity-refs-v2-audit-2026-09-26.json)。18 条实际提示、原始接口内容、实现及输入哈希、逐项评分和汇总均可复现。结果为 matched=17、invalid_response=1，其余为 0。

Codex 已逐项阅读全部 18 篇固定短稿、19 个位置输出及所选实体和范围。17 篇匹配稿包含 18 个位置命题；未发现这些结构化位置的额外身份、肯否、限定或作用范围问题，但不等于来源支持、独立专家复核或长篇正文验收。

| 复核项 | 结果与边界 |
| --- | --- |
| 版本容器与身份 | 18 条均保留正确容器，全部位置只含 value/polarity；主体/边界引用均可定位，没有重复身份混用 |
| 纯条件及双重限定 | original_condition、both、both_paraphrase、condition_and_inference 的位置、完整条件前提和限定均匹配；condition_paraphrase 仍失败 |
| 推测与否定断定能力 | possibility、possibility_paraphrase、inferred_position、explicit_uncertainty、denied_certainty 保留限定，未生成确定事实；最后一条 reason 把同句前缀称为“前句”，是解释文字不精确，结构化范围正确，不隐去该措辞问题 |
| 否定、词语提及、邻句干扰 | negative_position、word_mention、unrelated_possibility、unrelated_condition、unrelated_uncertainty 均未把词语提及或邻句限定错借为本句限定 |
| 直接位置与双主体 | plain_emphasis、source_position、two_people 均正确；“殿”引用未再扩大为“殿外”，双主体分别绑定各自出现位置 |

失败原文为“只要守门弟子点头，伤者便在封山线外。”原始划分为 `P1-U1-S1=只要`、`P1-U1-S2=守门弟子点头，`、`P1-U2-S1=伤者便在封山线外。`。模型把 cue“只要”放在 S2，qualified 仅选 S2 与结论，premise 仅选 S2；遗漏 S1。程序以“所选引文出现位置不存在”拒绝。位置的 outside/positive 与实体引用正确，也不足以追认成功。

本轮 `fieldDiagnostics/scopeDiagnostics` 为空，不能解释成没有范围问题：错误引用先使解码退出，条件前缀缺失由逐项复审识别。新增独立负例验证，即使 cue 正确，只要 premise 漏掉连接词也仍判 mismatched；该负例不改写或重新计分已保存响应。

实测为 direct / deepseek-v4-flash，接口模型 deepseek-flash；18 次 HTTP 200、finish_reason 均为 stop、每条一次无重试。输入 19,252 token、输出 2,439 token，共 21,691；中位请求耗时 1,101.5 ms；system+user 总长 41,620 字符。这是两场景已见固定集的一轮测量，17/18 高于首版 7/18 不能证明泛化或稳定延迟改善。

## 片段结束边界：离线改动与复审

[新实验入口](../test_support/context_qualifier_explicit_segments_eval.py)和[提示](../test_support/prompts/context_qualifier_explicit_segments.md)将展示改为 `⟦P1-U1-S1⟧只要⟦/⟧⟦P1-U1-S2⟧守门弟子点头，⟦/⟧`。根因假设是原来的起点标记没有显式展示片段终点，可能增加相邻片段归属误读；这只是可检验假设，不能证明上述模型失败的因果原因。

- 只给既有片段加结束标记；移除标记后逐字等于原稿，包括标点、引号、空白和段落。仍只提供一份正文和原实体引用表，不追加答案、历史、来源状态、规则清单或第二份原文。
- 沿用 qualifier-entity-refs/0.2 响应和冻结评分器；不扩大片段、不合并条件前缀、不代选 cue、不补模型漏选内容。旧失败离线复算仍为 17/18。
- 单独绑定新提示与展示实现，保留冻结入口；预检在输出创建和网关调用前完成，每条一次、最多 18 次，独占创建输出并保存原始响应。提示 SHA-256：`1d400468626fbf0a5843967c4c685f515527bfda437eeda521b49e2f4e3316c9`。
- 新版 18 条装配合计 42,385 字符，较 0.2 实测输入增加 765 字符、约 1.84%，来自标记及其说明；没有减少输入的宣称，是否值得该成本仍待实测。
- [新增 7 项测试](../tests_py/test_context_qualifier_explicit_segments_eval.py)覆盖原文逐字还原、片段终点、标签隔离、提示绑定、标记冲突/预算提前拒绝、旧失败保留、条件前缀缺失拒绝、模拟调用/审计与输入篡改拒绝。最终核心回归 **436/437**，唯一失败仍为既有 `test_prompt_catalog.test_original_expression_baselines` 的 `consequences.plan` 哈希不一致，未改基线。

新展示入口尚未调用真实模型，模拟 18/18 只说明实验流程可运行和可复算。生产代码、官方小说/StoryPackage、正式数据库、.env 和中转服务均未修改，Jev 未调用；无 API/前端变更，未重复相应测试。

## 下一步

先运行片段结束边界入口的一轮固定预算验证，重点复核 cue 的实际片段、完整条件前提和邻句限定隔离，并保留全部 18 条结果。若仍失败，保留失败，不放宽评分或反复抽样凑满分。固定集严格评分和逐项复审通过后，才冻结方案验证未见官方场景、来源关系与完整正文链路；Jev 继续后置。
