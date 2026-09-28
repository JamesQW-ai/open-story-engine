## 结论

已执行事实边界、等待收尾误报及独立核对单元修复。后续诊断又定位并修复了投影证据编号与本地校验编号错配：同一份正常等待稿的原始核对响应现在能通过，坏稿中的无依据时限仍被拒绝。完整生产 Planner 新一次诊断虽然机器通过，但正文新增无来源规则及设施，且审核错配说话人，因此质量验收仍不通过；场景审查自身的多段用例漏检也未宣告解决。

本文的正文复核与用例标注由 Codex 执行，不代表用户或独立人工验收。正式会话写入 0、Jev 调用 0；本轮未变更 `.env`、公司中转或默认投影开关。

## 本轮实现

| 文件 | 改动与目的 |
| --- | --- |
| `open_story_engine/prompts/reader/narrative.md` | 约束计划不能充当事实来源，不从伤势外推时限/结局；正常停在 NPC 提问处；修复意见中引用的缺证断言不能作为新事实恢复。 |
| `open_story_engine/prompts/reader/scene_review.md` | 区分等待玩家与代玩家决定；事实核对覆盖当前内部状态、程度、时限及因果预测，不因条件语气或说话用途而免检。 |
| `open_story_engine/prompts/reader/scene_grounding.md` | 同步约束逐句核对与对白知识核对：催促、请求、劝说中的事实内容仍需依据。 |
| `open_story_engine/reader_scene_review.py` | 在既有机械切分中，将中文引号开始的新对白与前面的叙述分开；保留原文、段号和完整 draft，缺失检查单元仍拒绝。 |
| `open_story_engine/reader_scene_review.py`、`open_story_engine/api_narrative.py` | 后续增加 `grounding_input_evidence`，按该稿实际发出的核对输入绑定原始 `sourceId/content`；缓存及引用修复复用相同输入。投影存在时不并入旧编号，隐藏、未确认及文风材料不能作为公开事实依据。 |
| `test_support/fixtures/context-review-regressions-2026-09-25.json` | 固定原始失败、原审核、公开证据、旧提示及 7 条正反例；原始证据 SHA-256 校验，手工构造的变体明确标注。 |
| `test_support/context_review_regressions.py` | 有限次数的阶段审核对比；区分匹配、误报/漏检、非法审核和模型错误；独占创建输出并逐次保存原始响应。 |
| `tests_py/test_context_review_regressions.py`、`tests_api/test_reader_scene_review.py` | 防止把漏检、误报或覆盖不完整算作通过；验证叙述后的危险对白独立覆盖，调整既有切分数量断言。 |

这次调整是替换阶段契约和细化核对单元，没有加入整段历史或把完整 bundle 塞回写作输入。切分不判断语义，不使用“某句话必拦/必放”的关键词规则。

## 真实模型证据

官方来源为 `taixu-relics-part1@0.1.3`；入口盘点确认母本 102,610 汉字。固定集覆盖正常请求后等待、擅自承诺、无依据存活时限、条件语气时限、灯的内部状态、有来源伤势观察及原始混合失败稿。阶段样本与一次生产诊断不能代表全入口质量或稳定性。

| 证据 | 调用数 | 结果与解释 |
| --- | ---: | --- |
| [首次固定集对比](evidence/context-management-2026-09-22/context-review-regressions-2026-09-25.json) | 14 | 旧提示 4/7、新提示 3/7 匹配；无格式或模型错误。首次修改没有改善总结果，保留此反证。 |
| [中间版本 production 诊断](evidence/context-management-2026-09-22/context-production-boundary-fix-2026-09-25.json) | 6 | 机器返回完成，但 Codex 发现 P3“再拖，他就真没气了”无公开因果依据，正文质量不通过。该版本早于最终 grounding 与切分修复。 |
| [再次澄清事实边界](evidence/context-management-2026-09-22/context-review-regressions-2026-09-25-fact-boundary.json) | 7 | 当前场景提示 6/7 匹配；仍漏掉原始多段稿 P2 的无依据时限。正例等待、有来源观察和负例擅自承诺均匹配。单次小样本结果不是稳定率。 |
| [最终独立核对原文回放](evidence/context-management-2026-09-22/context-grounding-dialogue-replay-2026-09-25.json) | 1 | 不修改中间 production 正文；在最终单元切分和 grounding 提示下，P3 的因果预测被明确判缺证，程序拒绝。只有独立核对阶段，不包含重写、提交或幂等验收。 |

上述首次修复阶段合计 28 次调用。旧证据原样保留；回放以来源文件哈希和 `promptVersion` 绑定输入版本，`acceptance=false`。

漏检的直接机制：原输入把“文书没有递出去”和随后“再拖，他就真没气了”放在同一检查单元，模型只解释了未交文书；对白核对又把整句分类为“当前催促”。切分及事实分类修正后，同一份原文的该断言被拦截。这证明该固定漏检得到修复，尚不能推导所有组合对白、所有场景均可靠。

## 后续诊断与引用绑定修复

证据：[最终守卫与 production 诊断](evidence/context-management-2026-09-22/context-final-guard-validation-2026-09-25.json)。本次新增 9 次调用：两条固定稿各一次独立核对，production Planner 一次运行 7 次调用、1 次正文生成，未重跑整个回合。与首次阶段合计 37 次调用，不包含任何正式或临时会话提交。

1. 原始多段稿中的“撑不到天亮”被独立核对明确拒绝，正常等待没有再被判行动越权。但审核把有公开依据的“山门将闭”对应表述“落锁的时辰近了”误当成具体时刻推断；这不是正文真实违规。
2. 正常等待稿被程序拒绝的确定原因是引用契约不一致：模型引用投影实际给出的 `module:opening:knownFacts:2`，程序仍拿旧 `opening-3` 字典核对。现已用发送给该稿的实际输入建立核对字典，并同步提示中的编号说明。没有扩充模型上下文，也没有按相似文字猜测编号。
3. [引用修复后的离线重放](evidence/context-management-2026-09-22/context-grounding-reference-replay-2026-09-25.json) 不修改正文、模型响应或已有证据，仅更换本地引用绑定：正常等待稿的 grounding、knowledge、scope、boundaries 四项均通过；坏稿的无依据时限仍拒绝。这是原响应的离线回放，不是新提示或最终代码的完整真实模型复测。
4. production 机器通过稿 P5 新增“封山令在身”“我去敲一声传事钟”，公开证据中没有该命令或设施。grounding 把它们归为 `current`，理由仅是当前拒绝/表示行动，仍漏掉台词中的事实前提。P7 的“钟响不响尚未知”不能证明钟存在。Codex 据此判正文质量不通过，原文保留。
5. 同稿 D4/D5 的说话人是守门弟子，却使用陆沉舟的 `character_37531636ecf5`。本次 Planner 未登记守门弟子，审核输入 `people` 只有主要角色；模型硬选了一个现有 ID，本地只检查该 ID 存在，未证明身份对应。这里同时存在输入缺项和审核漏检，不能写成纯正文问题。

剩余工作需修正阶段契约而不是继续堆提示：为在场且有公开来源的未具名角色建立受来源约束的说话人标识；明确当前发言/意愿中的规则、设施、因果前提仍为可核对断言；检查阶段投影是否保留所需的已确认依据。后者按计划引用和当前问题选择有界证据，不回填全部历史。以上尚未实施，不能把本次引用修复视为语义漏检已解决。

## 回归与下一步

- 引用修复相关回归：125 项通过；新增 6 项覆盖原始正反例、隐藏来源、未知/文风来源、错编号、无效投影及旧路径兼容。
- `python3 -B -m test_support.run core`：155 项中 154 项通过；已有 `test_original_expression_baselines` 的 `consequences.plan` 提示哈希失败仍保留，本轮没有改该提示或基线。
- `.venv-api/bin/python -B -m test_support.run api`：引用修复后 462/462 通过。
- `git diff --check`：通过。前端未改动，未重跑前端回归。
- 新增离线测试验证的是覆盖、来源绑定与拒绝机制，不能代替真实模型的语义能力测量。

原计划中的固定稿和 production 单次诊断已完成，结果未达到扩大实验的条件。下一步先固定并修复上述语义分类和说话人对应问题，再做有上限的定向验证；单样本稳定后才扩大到冻结同一计划的 20 条配对回放，并补齐临时数据库提交与幂等验收。后续顺序和进入条件见 [开发清单](context-management-next-checklist-2026-09-24.md)。
