## 修正范围与实验前复审

本轮针对[上一轮 44 条实测](context-management-frame-evaluation-review-2026-09-27.md)的五项失败，替换 attribution 定义，保持 qualifier-frame/0.1 协议、原文输入、44 条标签、条件处理和评分器不变。新候选仍是实验，不构成生产接入。

[新版提示](../test_support/prompts/context_qualifier_attribution_v2.md)只替换第三段，其余段落逐字一致。归属段落从 285 减至 270 字符，完整提示从 1,552 减至 1,537；每次少 15 字符，44 次输入字符合计应从 91,381 减至 90,721。程序验证提示哈希、替换范围和字符上限，不把旧失败、标签或历史台账加入模型输入。

| 判定顺序 | 含义与检查重点 |
| --- | --- |
| position | 直接叙述人物位置，包括条件、推测、否定和动作附带位置；仍保留原限定证据 |
| unresolved | 人物位置仅存在于转述内容时保持未决，不能转成直接现场事实 |
| word_mention | 只谈名称自身或念写字词；位置附带在说写动作上时由前面的直接位置规则处理 |
| other_entity | 该人物出现关联的位置实际属于另一人或物件，不能借同句共现转移位置 |
| no_position | 只提地点却没有此人的位置命题；各独立分句分别判定 |

其余无法确定归属的情况仍为 unresolved。以上为模型需要执行的语义判定顺序，不是程序以引号、“说”“提到”等字符串触发分类。程序仍只验证引用、范围、完整性和预算。源文包含直接叙述与多个转述层级的复杂混合情况仍超出本批覆盖，不能因五类定义明确就宣称通用归属解析完成。

实验前逐项对照：地点提及应归 no_position；同句另一个人物的位置不改变独立提及分句的类别；前置位置修饰语只归被修饰人物；自述位置与转述他人位置保持 unresolved；物件、名称文字和实际人物位置仍分别处理。条件、推测、否定及无关邻句继续按原金标验证。手写控制只证明契约可表达这些标签，不作为模型效果证据。

## 执行边界

[新入口](../test_support/context_qualifier_attribution_eval.py)直接复用前轮 assess_content、metrics 与汇总函数；独立保存本轮输入、输出和审计，绑定前轮证据，不修改冻结文件。每例一次，共 44 次；原始响应先保存再评分，遇到传输或保存失败立即停止，不补跑失败案例。

[7 项新增测试](../tests_py/test_context_qualifier_attribution_eval.py)已通过（60.811 秒），包括输入与评分隔离、旧真实失败复现、固定预算与排他输出、篡改拒绝、畸形响应不重试、传输／保存失败停止、原始响应先落盘及提示漂移预检。前轮五项失败和六项归属错误均按原评分复现。

## 实测结果与复审结论

**修正未通过效果复审。** 新一轮 44 次独立请求仍为 **37 匹配、2 范围未决、5 失败**（4 不匹配、1 无效响应）；40 条有限语义用例为 37/40，4 条范围控制仅 2 条保留预期未决，2 条转述失败。归属错误仍为 6 项。不能用未决增加、格式错误减少或 token 略降声称问题已解决。

本轮只执行这 44 次，不补跑失败、不挑选较好回答，也没有改写前轮证据。请求路线为 direct，请求模型 deepseek-v4-flash；44 次响应均为 deepseek-flash、HTTP 200、finish_reason=stop。模型／执行器错误为 0，正式会话写入、复核记录写入、Jev 调用均为 0。

[原始请求、响应及评分](evidence/context-management-2026-09-22/context-qualifier-attribution-eval-2026-09-27.json)与[复算审计](evidence/context-management-2026-09-22/context-qualifier-attribution-eval-audit-2026-09-27.json)保留全部案例及绑定哈希。离线审计已通过：实际输入、原始响应、逐行评分、汇总和绑定文件均可复现，审计新增模型调用为 0。

### 本轮五项失败

| caseId | 原始回答 | 复审判断 |
| --- | --- | --- |
| two_people:first_operator | nonPremiseUnits 返回 P1-U2-S1、P1-U2-S2 或 P1-U1-S1 | 应复制 contextUnits 中的单元 ID，却返回了片段 ID；前轮匹配、本轮无效。位置与推测看似正确也不能绕过协议，不能截掉后缀自动修复 |
| control:location_mention | “沈砚秋提到了议事殿。”→ unresolved、空 items | 不再编造位置，但没有正确识别 no_position；保守弃答是错误形态变化，不是匹配 |
| frame:mixed_ownership | “议事殿内的沈砚秋提到了守门长老。”→ 两人均 unresolved | 前轮将位置错误转给长老，本轮则漏掉沈砚秋的明确位置；同时错分长老的 other_entity。不是语义修复 |
| frame:self_report | “沈砚秋低声说：‘我在议事殿内。’”→ position、inside | 转述仍被当作现场位置；原失败保留 |
| frame:other_report | 说话人 unresolved，被转述长老 position、inside | 说话人应 other_entity，被转述人物应 unresolved；仍然丢失断言来源与层级 |

上一轮五项失败中只有 control:mixed_people 本次完全匹配；其余四项仍失败，并新增 two_people:first_operator。更早覆盖协议暴露的 gate:original_condition、gate:both_paraphrase、control:quoted_words 本次继续匹配。原最初 30 条从前轮 30/30 回退到 29/30，不能只报告新修正的案例。

### 分母与语义边界

48 个任务全部作答，归属六项错误包括 2 次不该答 position 却答了 position，以及 4 次不该答 unresolved 却答了 unresolved；后者含 1 个明确人物位置被漏掉。因此 incorrectNonPosition=0 不能单独解读为没有漏报，该指标不包含 unresolved，须同时看 missingPositions 和 unexpectedUnresolved。

43 条结构有效响应涉及 36 个预期位置：35 个内容匹配，1 个遗漏，2 个额外位置（均来自转述）。余下两个预期位置位于整条无效的 two_people:first_operator 中，未进入位置评分；本批预期位置总数仍为 38。限定多报／漏报为 0 只针对 35 个内容匹配的位置，不覆盖未评分或遗漏的位置。

5 个预期条件前提均正确选择，错误非前提、漏条件、多条件、依赖未决均为 0。无效行使用错误 ID 的依赖分区未被算作成功。两条代词／跨句空任务仍只验证范围未决，不证明指代消解能力；两条转述失败仍纳入失败报告，不因其属于范围控制而隐藏。

已由 Codex 逐项阅读全部 44 条原文与模型响应。37 条匹配行未发现应推翻本批有限范围标签的额外错误；这不是用户人工验收，也不是来源真实性审核。条件 cue 有的选连接词、有的选前提内容，但完整 premise 都保留条件；“没有人敢断定”保留 modality，未把位置极性反转。两对重复原句仍存在，不把 44 次请求说成 44 个独立语言样本。

| caseId | 本轮状态 | 原始回答复核 |
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
| two_people:first_operator | invalid_response | 失败：位置和推测归属看似正确，但 nonPremiseUnits 使用片段 ID；整条拒绝，不自动转成单元。 |
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
| control:location_mention | mismatched | 失败：未再虚构殿内位置，但把明确无位置的提及答成 unresolved。 |
| control:object_position | matched | 木盒的位置没有转给沈砚秋。 |
| control:quoted_words | matched | 仅说出字词，返回 word_mention；本轮未虚构位置。 |
| control:mixed_people | matched | 沈砚秋 no_position、长老 inside 正确；本次修正了归属子类。 |
| scope:pronoun | scope_pending | 空任务按边界保留未决；不代表代词语义已解决。 |
| scope:cross_sentence | scope_pending | 空任务按边界保留未决；不代表跨句指代已解决。 |
| control:adjunct_mention | matched | 抄写“或许”未抹除自身殿外位置，也未增加推测。 |
| control:negative_position | matched | inside+negative 正确。 |
| frame:name_words | matched | 名称文字在纸上，未提取为人物位置。 |
| frame:portrait | matched | 画像位置没有转给长老。 |
| frame:mixed_ownership | mismatched | 失败：两人均答 unresolved，漏掉叙述明确的沈砚秋殿内位置。 |
| frame:position_and_quote | matched | 保留叙述明确的殿内位置，没有转移引文中的殿外位置。 |
| frame:self_report | mismatched | 失败：自述内容仍被提取为沈砚秋直接在殿内。 |
| frame:other_report | mismatched | 失败：说话人错误答 unresolved，被转述人物仍答直接在殿内。 |

## 实际成本与验证

| 同一 44 条 | 前轮框架提示 | 本轮归属修正 |
| --- | ---: | ---: |
| 输入字符 | 91,381 | 90,721 |
| 输入 token | 41,105 | 41,105 |
| 输出 token | 4,851 | 4,635 |
| 总 token | 45,956 | 45,740 |
| 请求耗时中位数 | 930 ms | 835 ms |

usage 与请求耗时记录均为 44/44。输入少 660 字符，但服务端记录的输入 token 逐条与前轮相同；字符下降没有转化为本轮输入 token 收益。输出少 216 token 不构成质量改进证据，部分短回答恰好是错误弃答。耗时属于跨时段单轮观测，不是稳定时延收益或正文首字耗时。

核心回归 **530/531**（489.113 秒），唯一失败仍为 test_prompt_catalog.test_original_expression_baselines 的既有 consequences.plan 提示哈希不一致。未修改该提示或更新基线。新增 Python 语法与空白、文档链接、全部历史／本轮证据绑定及 `git diff --check` 检查通过。本轮未改 API／前端，未重复执行其套件。

## 接下来调整推进方式

本次实验不支持继续靠追加或反复改写分类文字解决归属问题。保留此候选及失败证据用于对照，不把它设为默认方案；下一步先做命题表示的离线设计复审，再决定是否值得进行下一轮有限实测：

1. **拆清问题，先不增加调用。** 当前一个 attribution 同时承载“有没有位置”“位置属于谁”“是否来自转述”“是否仅为字词”“是否无法判断”；本轮对这些类别仍混淆。先用已有原文和失败回答设计最小表示，明确哪一段是位置命题、命题主体是谁、哪一段标明转述来源。引用合法只证明片段存在，不能替代归属正确。
2. **以替代契约表达断言层级。** 评审候选应能区分直接叙述位置、带限定的位置、转述中的位置与非位置提及，并对每个候选保留原文范围和主体引用。转述不得生成可提交的位置断言。用命题范围取代含混的五类归属职责，不把一套新标签、自由文本理由和旧分类重复堆入同一上下文。具体字段先通过离线正反例证明可表达，再冻结；本轮尚未实现该替代契约。
3. **单独处理标识符可靠性。** core/cue 使用片段、依赖使用单元，设计上应让模型复制字段对应的允许 ID。若调整输入，必须等量替换原表示并测量体积；不添加第二份全文。对本轮错误保留拒绝，不自动删后缀、补范围或替模型选择语义。
4. **原 44 条保留为不可改写的诊断集。** 新协议若更改输出结构，须给出显式映射与不能比较项；无位置、错误归属、转述升级、遗漏和弃答均独立报告，不能合并类别后宣称旧错误已修好。离线复审通过才安排独立预算实测，任何实质问题未解决则维持隔离。
5. 固定集稳定后才进入未见官方场景、来源支持、其他事实类型及完整正文／提交链路；随后评估三层上下文的实际质量收益。生产接入与 Jev 继续后置。

本轮生产代码、.env、公司中转、正式数据库、官方小说与 StoryPackage 均未改动。


### 下一步离线设计的验收样例

以下仅冻结设计应回答的问题，不是新协议实现或新模型通过记录；均复用本批原文，不增加实测次数。实际字段、预算和映射方案待下一步复审。

| 原文类型 | 应能分别表达的语义 | 不可接受的省略 |
| --- | --- | --- |
| 人物仅提到地点 | 有提及动作，无此人的位置断言 | 编造位置，或把可明确判断的无位置一律弃答 |
| 地点内的人物提到另一人 | 位置命题主体是被修饰人物；另一人只是提及对象 | 将位置转给另一人，或为避免转移而连正确位置一起删掉 |
| 人物说“我在某处” | 话语来源与被声称的位置主体可能相同；仍是转述中的命题 | 因主体相同就删除转述层级 |
| 人物说另一人在某处 | 话语来源、位置主体是两个角色；保留完整转述范围 | 仅引用位置子句，就将被转述内容升级为直接事实 |
| 人物念出表示位置的字词 | 字词出现与命题被断言是两件事 | 看到位置用语就提取现场位置，或仅按“说／念”动词猜层级 |
| 人物确实在某处并念出字词 | 直接叙述位置与字词内容共存 | 因存在引号就放弃叙述明确的位置 |
| 条件／推测下的位置 | 主体和断言来源之外，还须保留完整条件与推测证据 | 换了归属表示却把限定压成普通肯定位置 |

设计复审必须检查主体、位置内容、断言来源和限定是否分别保留，并列出旧标签到新表示的可比与不可比项。即使所有引用都合法，也只能声称结构可表达；语义正确仍需独立实测，来源支持仍需对受权证据核验。

后续离线实现见[断言来源表示复审](context-management-assertion-scope-review-2026-09-27.md)。它新增手写结构控制和隔离原型，模型调用为 0；本文件记录的原始实测成绩和五项失败不因此改变。
