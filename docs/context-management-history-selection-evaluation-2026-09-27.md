状态：第二轮压力评估与明确引用传递修复完成，核心定向 124 项、完整 API 520 项通过；新增压力场景仅 5/14 符合标签，上下文质量门槛尚未通过。第一轮开发控制的通过不能代表复杂语义或真实正文验收。

## 第一轮实测发现

当前《太虚遗录》`0.1.3` 母本为 102610 汉字。使用其陆照临官方开场的可见事实、物品说明及逐字核对的原文片段，构造 12 个历史引用控制；另通过现有长篇成本夹具构造 8 个目标/问题生命周期控制。

这些回合排列、父页摘要和记忆回执是合成输入，不是原著实际发生的剧情。标签由本次开发助手逐项写出并附理由，没有独立人工评审，也不是留出集。期望标签不传给选择器或 bundle。

| 组别 | 修正前 | 最终修正后 |
| --- | --- | --- |
| 12 个历史引用案例 | 7 个符合标签；漏选 3，误选 2 | 12 个符合标签；漏选 0，误选 0 |
| 历史候选精确率 / 召回率 | 66.7% / 57.1% | 100% / 100%（仅该控制集） |
| 8 个记忆生命周期案例 | 8 个符合标签 | 8 个符合标签 |

指标只计算可选历史候选，强制保留的父页不混入精确率和召回率分母。没有候选时分母为零，返回 `null`，不伪造 100%。

三次漏选分别是“看空灯”“读文书”和“记起老人交代”的指代承接：不重叠的双字切分让内容词随着前缀奇偶改变。两次误选来自“手边”和“他们”的共享词面，实际涉及不同对象或不提供本次所需依据。

上述漏选来源仍在完整 `sceneEvidence` 中；报告单独记录 `missingFromFullPublicEvidence`，这三例均为空。不能将“历史引用没选中”描述成“整个审核请求已经没有该证据”。

## 第一轮修正

只修改 `action_review_context.py` 的局部匹配，不改变硬事实/修复模块共用的 `context_fact_terms`，也不新增模型调用或扩大历史窗口：

- 中文采用重叠双字匹配，避免单字前缀切坏内容词。
- 泛指代词与相对位置词不能独立证明相关性；先以边界隔开这些词，再提取匹配项。补充反例表明，仅在提取后过滤还会把“他们手边”中的“们手”误当内容词，此问题已修复并保留测试。
- 父页承接、已校验计划明确引用和已选确认动态记忆继续优先保留。排除泛指词不会屏蔽同一句里的“空灯”等明确对象，也不取消明确引用。
- `RULES_VERSION` 升为 `prepared-player-turn/27`，旧准备结果重新生成，已提交故事不迁移。

这仍是词面选择规则，不是语义理解器。它无法据此证明复杂同义改写、跨多页指代或隐含关系均能召回；未因本组全过而宣布完整上下文工程验收。

## 可核对产物

- [标签及逐例理由](../test_support/fixtures/action-history-selection-2026-09-27.json)
- [评估脚本](../test_support/action_history_selection_eval.py)
- [初始结果](evidence/context-management-2026-09-22/action-history-selection-before-2026-09-27.json)
- [最终复审结果](evidence/context-management-2026-09-22/action-history-selection-reviewed-2026-09-27.json)
- [修正前选择器快照](evidence/context-management-2026-09-22/action-history-selector-before-2026-09-27.py)、[初始评估器快照](evidence/context-management-2026-09-22/action-history-evaluator-initial-2026-09-27.py)、[初始标签快照](evidence/context-management-2026-09-22/action-history-labels-initial-2026-09-27.json)

`after` 与 `final` 文件保留中间结果；第一轮以 `reviewed` 为准，不覆盖旧证据。已核对初始三份快照哈希与初始报告一致，并用初始选择器/评估器重现全部初始案例结果。第二轮修改了选择器和评估器，当前代码不能再直接审计第一轮报告；已保存[第一轮复审选择器](evidence/context-management-2026-09-22/action-history-selector-reviewed-2026-09-27.py)与[第一轮复审评估器](evidence/context-management-2026-09-22/action-history-evaluator-reviewed-2026-09-27.py)，核对其哈希与 `reviewed` 报告一致，并使用这些快照复现其全部案例结果。

## 第一轮验证边界

核心定向回归 120 项通过（3.988 s）。新增评估器测试检查：漏选与误选分开计数、零分母、结果可复现、反转标签不改变选择及上下文、空组/重复 ID/非布尔标签拒绝，以及原文引用变化明确报错。额外选择测试覆盖泛指词旁仍有明确对象、泛指词跨边界碎片和显式引用优先级。

最终版本完整 API 回归 520 项通过（85.582 s）。最终报告来源哈希和全部结果复现一致。

```sh
python3 -B -m test_support.action_history_selection_eval --output /tmp/action-history-evaluation.json
python3 -B -m test_support.action_history_selection_eval --audit docs/evidence/context-management-2026-09-22/action-history-selection-v28-2026-09-27.json
python3 -B -m unittest tests_py.test_action_history_selection_eval tests_py.test_action_review_context tests_py.test_context_required tests_py.test_context_bundle tests_py.test_context_memory_longform tests_py.test_context_prompt_integration tests_py.test_state_visibility
.venv-api/bin/python -B -m test_support.run api
git diff --check
```

输出文件必须不存在，评估器拒绝覆盖证据。未调用真实模型、Jev 或生图接口，未写正式会话。未改 `.env`、公司中转、冻结故事包、提示词或历史提示基线。未运行前端测试或完整 core；此前 `consequences.plan` 提示哈希差异未在本轮处理。

## 第二轮：复杂表达与跨页压力评估

新增 [14 个历史场景标签](../test_support/fixtures/action-history-stress-2026-09-27.json)，继续使用同一达标官方长篇的原文来源。中间页和计划引用是合成控制，标签由开发助手自审，不称为独立人工评审或未见测试集。8 个记忆生命周期案例沿用第一轮，不计作新增案例。预期标签与理由不进入运行时；反转标签后实际选源及上下文不变。

| 指标 | 本轮修正前 | 本轮修正后 |
| --- | --- | --- |
| 历史场景完全符合标签 | 3/14 | 5/14 |
| 所需引用选中 / 漏选 / 额外引用 | 2 / 9 / 3 | 4 / 7 / 3 |
| 历史候选精确率 / 召回率 | 40.0% / 18.2% | 57.1% / 36.4% |
| 沿用的记忆生命周期控制 | 8/8 | 8/8 |

本轮发现并修复：明确计划引用仅在两条 `continuityWindow` 中查找，即使旧模块摘要仍在同一冻结包的公开证据内，也没有进入行动审核引用。现已沿用这些明确绑定的历史来源，不读取更远祖先、不扩大窗口、不再次复制来源正文。所有明确引用重新校验当前冻结包的公开来源及逐字引文；调用方额外塞入的来源、隐藏来源、来源内容变化或无效引文均拒绝。相同引用去重。此检查证明来源存在且可公开，不证明引用在语义上支持计划。

`RULES_VERSION` 从 `prepared-player-turn/27` 升至 `/28`。核心业务和正式会话不迁移；父页承接、动态记忆和权威状态的职责保持原有边界。

仍有 9 个失败场景，不隐藏或改写标签：

| 问题 | 场景数 | 缺口所在 |
| --- | --- | --- |
| 推荐信/引荐文书、家父/父亲等改写漏选 | 3 | 来源仍在公开证据中，但词面引用选择不能建立语义关联 |
| 点名对象的旧模块摘要未选中 | 1 | 来源在冻结包内，但不在连续性窗口，也没有计划明确绑定 |
| 隔页物品事实与老人交代丢失 | 3 | 目标摘要已不在完整公开证据中；其中一例还误选了无关停步摘要 |
| 文书职务与纸件同词、不同事件共享时间 | 2 | 词面相交不足以区分对象和事件 |

跨页摘要缺失不等于权威状态也缺失；此处评估的是具体承接来源，不将缺少来源解释成“事件未发生”。`gap-three-document` 同时有漏选和误选，所以引用错误总数与失败场景数不同。

本轮证据：[修正前](evidence/context-management-2026-09-22/action-history-stress-before-2026-09-27.json)、[修正后](evidence/context-management-2026-09-22/action-history-stress-after-2026-09-27.json)、[原 20 项控制在 /28 下的结果](evidence/context-management-2026-09-22/action-history-selection-v28-2026-09-27.json)。修正前结果已用第一轮选择器快照复现；修正后全部结果及来源哈希审计一致。所有旧证据保留。

本轮核心定向 124 项通过（4.866 s），完整 API 520 项通过（79.634 s），`git diff --check` 通过。新增覆盖窗口外明确引用、引用去重、冻结包不变、隐藏/伪造/篡改/空引文拒绝，以及压力评估的标签隔离与建包缺失识别。测试通过只证明这些检查执行正确，不能将 9 个失败场景计为质量通过。未运行完整 core 或前端测试，未调用真实模型、Jev 或图片接口。

```sh
python3 -B -m test_support.action_history_selection_eval --fixture test_support/fixtures/action-history-stress-2026-09-27.json --audit docs/evidence/context-management-2026-09-22/action-history-stress-after-2026-09-27.json
```

## 后续顺序与验收门槛

1. 先定位跨页来源在确认回执、动态记忆选择、建包三个环节中的丢失点。只召回当前行动需要且来源仍有效的事实，以当前物品归属、目标生命周期及分支可见性为约束。验收需包含来源变化、物品转移、同父分叉和无关历史；不能单纯扩大最近回合数来通过。
2. 再处理来源绑定：评估现有规划调用能否将同义行动和指代绑定到有界公开候选的 `sourceId` 与原文引文，不先增加一轮串行模型调用。已有明确绑定必须从规划持续传到写作、审核及修复；无法消歧时保留未知，不根据同词或时间重合决定事实成立。此方案仍待实现和实测，不能靠新增同义词表宣布解决。
3. 上述路径经过固定正反例与复审后，开展限定次数的真实正文—审核—修复—提交实验，检查首稿事实一致性、连续三回合承接、拒绝不入账、幂等和耗时。保留所有失败，不能反复重跑挑成功。若仍有语义误判，应报告剩余风险而不是放宽守卫。
4. 上下文质量验收后，进入既定配图 I1：已提交正文的稳定段落锚点；后续才推进高潮/长文插图与现有生图接口提速。

上一阶段：[上下文包必需性审查](context-management-required-bundle-review-2026-09-27.md)。
