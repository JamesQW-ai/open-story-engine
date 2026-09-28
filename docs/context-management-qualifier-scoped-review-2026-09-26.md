后续进展见[严格位置与前提验收](context-management-qualifier-strict-review-2026-09-26.md)：独立内容与范围评分已实施，新一轮严格结果为 16/18，仍有实体引用与顶层字段失败。本文保留上一轮评分漏检及补充复审证据。

## 结论

本轮完成原文标点分组、必填引用示例及并行诊断；只运行一次 18 条固定集。冻结评分器给出 **16/18 匹配、2 条格式失败**，但逐项复审发现评分器漏检：4 条条件前提包含位置结论，5 条省略可解析的位置解释，其中 1 条重叠。补充离线检查将结果分为 **2 条原有失败、8 条需进一步复核、8 条未发现新增问题**。最后一类不是语义正确证明，本轮仍未验收。

上一轮缺主体引用的两条本次都补齐；`gate:unrelated_condition` 不再借用前句条件，`gate:both_paraphrase` 却省略了位置解释，不能宣布该条完整修复。没有追加重试，没有通过清理响应、补引用、删除条件或扩大待复核范围改判成功。

## 实现与输入边界

- [输入与诊断](../test_support/context_qualifier_selection_scoped.py)：同一份原文以 `sourceBlocks` 展示，按原有句末标点、分号或段落分组；逗号前后的条件与结论保持同组。逐块去掉内联标记再直接拼接须逐字等于原稿，空白、标点和段落均保留。分组不代表语义解析，也不能独自处理引号内标点或跨句条件承接。
- [实验提示](../test_support/prompts/context_qualifier_selection_scoped.md)：七个字段必填，主体/边界展示完整引用对象，明确 `position.subject` 的实体 ID 不能替代原文引用。仍用既有 `qualifier-selection/0.2`，没有新增历史、旧响应、答案标签或全量上下文。
- 格式缺失/额外字段与跨句范围分别诊断，因此缺少主体引用时也会暴露前句条件误借；不从位置解释补写引用，不吞掉无效字段。
- [有界入口](../test_support/context_qualifier_selection_scoped_eval.py)：调用前校验提示 SHA-256 与契约版本；每条一次、总上限 18、保存失败停止、不得覆盖证据。输入与依赖冻结，旧脚本和成绩保持原样。
- [补充离线复审](../test_support/context_qualifier_selection_scope_audit.py)：先复算冻结实验并核对输入、实现和原始响应，再额外诊断条件前提与 core 重叠、position 缺失。缺失解释本身只是复核触发器；是否实际可解析仍需逐项读原文。该脚本没有把异常转换为通过，不调用模型，也不授予事实或生产权限。

18 条请求的 system+user 总长由 34,120 降至 34,040 字符，减少 80 字符，基本持平。分组与提示同时变化，且只有一次已见固定集实验，不能将局部变化归因于某一个设计或宣称正文质量改善。

## 逐项复审发现

| 案例 | 原评分 | 新发现及结论 |
| --- | --- | --- |
| `gate:possibility`、`gate:unrelated_possibility` | 格式失败 | `segmentId/core/qualified` 把 `⟦ ⟧` 包进 ID，严格匹配失败；原样保留，未剥离标记后重新计分 |
| `gate:original_condition`、`gate:both` | 匹配 | premise 包含完整位置结论；前提与结论没有分离 |
| `gate:condition_paraphrase` | 匹配 | premise 含结论，qualified 还漏掉“只要”；后者是逐文审查发现，补充脚本没有声称自动检出 |
| `gate:both_paraphrase` | 匹配 | 引用和双重限定齐全，但 position=null；“是否实际在外”不确定，不妨碍解释被讨论的位置为 outside |
| `hall:inferred_position`、`hall:explicit_uncertainty`、`hall:denied_certainty` | 匹配 | 因推测或否定断定能力而省略位置解释；位置内容与事实确定性仍被混淆 |
| `hall:condition_and_inference` | 匹配 | 同时存在 premise 含结论和 position=null |
| 其余 8 条 | 匹配 | 本次复审未发现新增问题，包括前句无关条件、位置否定、同句双主体及“可能”作为被提及词语的反例；仍不等于来源支持或产品验收 |

根因不仅是模型格式稳定性。旧评分会把所有有限定的 normal 清空，再比较“待复核及限定类型”，所以无法保证被讨论的位置解释完整；条件前提只检查引用存在、包含 cue 且位于 qualified 内，没有排除结论混入。补充脚本让漏检显式可见，但尚未重构或替换运行中的冻结评分契约。

## 证据与验证

- [原始 18 次请求](evidence/context-management-2026-09-22/context-qualifier-selection-scoped-2026-09-26.json)、[冻结评分复算](evidence/context-management-2026-09-22/context-qualifier-selection-scoped-audit-2026-09-26.json)、[额外范围诊断](evidence/context-management-2026-09-22/context-qualifier-selection-scoped-scope-audit-2026-09-26.json)。原始响应、实际提示、模型输入、全部评分及历史证据哈希均可复算。山门 7/9、议事殿 9/9 只是旧评分结果。
- 同一官方长篇 `taixu-relics-part1@0.1.3` 的两个已见场景、18 份稿件、19 次位置命题；母本 102,610 CJK。没有新场景泛化测试或完整正文链路验收。
- 调用为 `direct / deepseek-v4-flash`，返回模型 `deepseek-flash`；18 次 HTTP 200，finish reason 均 `stop`。输入 14,769 token，输出 3,448 token，总计 18,217。本轮只有这 18 次，补充复审为零调用。
- 预检专项 25/25。核心回归 386 项中 385 项通过，唯一失败仍是既有 `consequences.plan` 提示哈希基线，未改基线。随后新增 4 条复审测试，连同本轮输入/网关检查共 15/15；386 是增补前快照，未把重复执行累计为新增通过数。
- `git diff --check` 及新增文件空白检查通过。没有生产/API/前端代码变动，未重复 API/前端回归；未改 `.env`、中转站、正式数据库、母本或官方包，无正式会话/复核记录写入及 Jev 调用。

## 后续顺序

1. 先修订独立验收契约：分别评价“位置内容是否解释完整”“条件前提是否精确”“能否确认为事实”。补齐前提混入结论、条件连接词漏掉、可解析位置被置空的反例；保留此轮旧分数与补充复审差异。
2. 统一展示标识与输出标识的语法，采用明确示例或可直接复制的 ID；在下一版本调用前冻结规范，不对历史响应静默归一化。
3. 上述离线边界通过后，才进行下一轮固定预算的模型实验；模型不通过则继续报告真实失败，不反复抽样凑满分。
4. 固定集经逐项复审通过后，再冻结提示验证未见官方场景、来源关系核对和完整正文链路。当前不进入生产或 Jev。
