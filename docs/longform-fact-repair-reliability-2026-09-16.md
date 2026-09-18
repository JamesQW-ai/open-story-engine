## 本轮结果

在 NQ-001 暂缓期间，补齐可确定验证的事实审查、局部修复和失败降级契约，修复两类已复现缺陷。未调整篇幅策略、写作提示或模型，不将这些工程回归等同于叙事质量验收。

1. **局部修复标记漏检**：网页修复只识别自己的两类中文标记，核心修复只识别 `⟦REPAIR_GAP_…⟧`，相互漏检；核心路径还只检查被替换段落。现在两条路径共享三种已知标记前缀，并检查最终整篇结果，未修改段落残留标记也拒绝通过。
2. **空重试清空可读正文**：局部修复未成功后，后续正文调用返回空内容，会覆盖之前保存的完整候选，最终异常时没有可恢复正文。现在空白或包含已知修复标记的候选不覆盖之前的候选。保留正文仍标记 `unconfirmed`，不能据此更新正式剧情或状态。

草稿规则版本更新为 `prepared-player-turn/18`，新请求不能复用旧规则下的缓存结果，也不修改已经保存的历史正文。

## 长篇测试范围

新增 `tests_py/test_longform_fact_repairs.py`（9 项）与 `tests_api/test_longform_repair_fallback.py`（3 项）。两套测试每次从 `longform_cases()` 枚举全部达标母本及匹配官方包，不硬编码书名、包版本或人物 ID。

当前只有《太虚遗录》第一部达标：原始 102610 CJK、50 章、`taixu-relics-part1@0.1.2`。事实契约测试使用首章完整原文段落、官方身份以及明确注入的错误经历；API 故障测试使用实际官方开局与临时数据库，并以长篇段落构造带有已知视角错误的候选，触发真实规划器的校验与局部修复流程。候选正文用于故障注入，不是新生成剧情的文学样本，也不会写回母本。

测试不导入或执行停用短篇模块；不调用真实模型，不使用生产数据库。原文来源、全书模块与三身份路线检查由既有长篇测试继续覆盖。

## 核心契约迁移

以下是对历史 `test_story_draft_regressions.py` 中通用保障的长篇改写，不代表旧模块全部恢复，也没有照搬旧规划器“失败不展示正文”的历史预期。

| 当前用例 | 对应历史保障 |
| --- | --- |
| `test_support_references_bind_exact_source_and_owner` | 不透明证据引用绑定来源与人物归属，错编号／错归属拒绝 |
| `test_review_requires_all_paragraphs_and_registered_evidence` | 全段覆盖、真实证据编号、重复／布尔编号拒绝 |
| `test_independent_review_catches_first_review_omission` | 初审漏报由独立复核纠正，复核本身不能缺段或重复 |
| `test_format_repair_preserves_conflicts_and_only_fills_requested_records` | 格式修复只补指定条目，不撤销已发现的事实问题 |
| `test_local_replacement_preserves_source_neighbors_and_rejects_invalid_edits` | 保留未修改原文，拒绝歧义编辑、无变化与三类占位标记 |
| `test_fixing_one_issue_cannot_keep_another_or_move_it` | 已拒绝断言不能留在原段或转移到别段 |
| `test_repair_budget_rejects_whole_chapter_rewrite` | 全文重写不能伪装为局部修订 |
| `test_exact_neighbor_echo_removed_without_changing_original` | 只清除能逐字证实的相邻段回显，不改原邻段 |
| `test_quote_localization_does_not_drop_negation_or_reorder_source` | 引文定位保留原词与顺序，不接受插入、反向排列或否定改写 |

这些检查证明结构化审查结果被程序正确消费，不能证明真实模型会报告所有背景错误、识别所有代词或理解全部经历归属。

## 失败降级与审计证据

- 局部修复请求连续两次传输失败时，仅在当前失败调用重试一次，不重生成前面的规划与正文。调用顺序为规划、授权、正文、修复、修复。
- 正式数据库完整 dump 在请求前后相同；没有新分支、评估回执或状态写入。
- 私有草稿状态为 `failed`，可读正文标为 `unconfirmed`，不存在可提交 artifact；流在终止错误前恢复该正文。
- 审计保留 `beforeBody`、`issues`、`failureReason`、失败结果和调用阶段。修复服务未返回正文时 `afterBody` 为 `null`，不虚构修复后文本。
- 关闭并重建服务后，从持久化私有草稿恢复同一正文和审计记录，仍不能作为正式成功结果返回。
- 另行复现“局部修复响应无效 → 下一轮空正文 → 后一轮传输失败”，修复后仍恢复首轮可读候选；正式数据库保持不变。

## 验证结果与后续

- `.venv-api/bin/python -B -m unittest tests_py.test_longform_fact_repairs tests_api.test_longform_repair_fallback -v`：12 项通过。
- `python3 -B -m test_support.run core`：25 项通过。
- `.venv-api/bin/python -B -m test_support.run api`：214 项通过。
- `git diff --check`：通过。未修改前端，本轮未重复前端构建或桌面试玩。

本轮复现的占位标记漏检和空正文覆盖已修复。NQ-001 真实叙事质量与 SRC-001 冻结母本重复内容仍未解决；尚未恢复所有历史通用回归，也未完成告知／询问／等待的真实模型连续试玩。

下一步优先核查修复审计的完整性：已有记录能否准确区分模型返回无效修复、修复后仍不通过、传输失败，并保留真正的前后正文与问题类型；继续使用长篇和临时数据库，避免扩大生成轮数或引入短稿扩写。

后续审计批次已完成上述字段与失败分类核查，并修复同一请求重试丢失前次审计的问题；累计 25 项核心、220 项 API 通过。见 [修复审计可靠性记录](repair-audit-reliability-2026-09-16.md)，记录仍受私有草稿保留期限限制。
