状态：读取时生成历史指纹已接入快照、选项准备、入队和提交；配对测量与复审完成，完整 API 回归 514 项通过。本轮未改变数据库格式、提示词、正文或记忆预算。

后续进展：[行动审核历史引用与空窗口审查](context-management-action-history-review-2026-09-27.md)。下文保留本阶段的测量与当时待办边界。

## 改动与兼容边界

`SessionStore.lineage_with_fingerprint` 在读取完整祖先链的同时，对每行原始 `node_json` 字节计算 SHA-256，再绑定该行实际 `id`、`session_id`、`sequence`、`parent_id`。整链按根至当前父分支顺序组合固定长度节点指纹，并带 `stored-lineage/1` 域标识。

全部 JSON 仍照常解析，返回的 lineage 与原方法逐项相同。不使用数据库内预存摘要、不跨请求缓存、不省略来源或历史状态；省掉的是解析后再次规范序列化整个节点的工作。

数据库读取列中的会话和序号仍覆盖 JSON 内同名值。新方法核对 JSON 节点 ID/父关系与存储列一致，并拒绝循环、断链和跨会话父关系。旧根节点空字符串 `parentId` 兼容原有空父语义；复审发现这一边界后已补测试修正。

指纹与上一版规范 JSON 指纹的格式有意不同：仅改变空白或键顺序也会让草稿失效。这是保守失效，不是正文或状态变化。`RULES_VERSION` 升为 `prepared-player-turn/24`，旧准备结果重新生成；已保存分支不删除、不迁移。默认 `SessionStore.lineage` 保留，其他消费者不被静默切换。

选项准备每个选项的锁内检查也改为一次读取，路线状态、收束意图和历史绑定共享同事务数据。快照、入队、提交各自重新读取；不跨越这些检查点复用旧数据库结果。

## 实测

工具：[history_fingerprint_benchmark.py](../test_support/history_fingerprint_benchmark.py)。

最终证据：[history-fingerprint-final-2026-09-27.json](evidence/context-management-2026-09-22/history-fingerprint-final-2026-09-27.json)。[首轮证据](evidence/context-management-2026-09-22/history-fingerprint-paired-2026-09-27.json)保留，不覆盖。

两组均调用实际 `_turn_snapshot`、完整读取一次历史并校验来源绑定。对照组恢复原来的“解析后规范序列化”指纹方法，新组在读取时计算原始字节指纹。每档五次、交替顺序、清空应用快照缓存，操作系统缓存不控制。数据库准备、记忆/bundle 对照检查不计入快照计时。

| 夹具 | 回合 | 原方法中位耗时 ms | 新方法中位耗时 ms |
| --- | ---: | ---: | ---: |
| goal | 32 | 9.77 | 8.33 |
| goal | 128 | 36.75 | 23.91 |
| goal | 512 | 510.62 | 349.12 |
| thread | 32 | 10.05 | 8.39 |
| thread | 128 | 37.37 | 23.73 |
| thread | 512 | 407.02 | 226.42 |

512 回合快照准备在本次夹具中分别减少约 32% / 44%。这不是整轮正文生成加速比例。当前历史 JSON 解析仍有成本，未宣称消除历史状态重复存储。

当前官方母本为《太虚遗录》`0.1.3`、102610 汉字。正文取不同原始段落，账本和审核结构仍为合成成本夹具，不提供语义验收。未调用真实模型、Jev、生图接口，未写正式会话。

两组指纹格式不同，因此不要求回合绑定字节完全相同；其余绑定字段一致。完整 lineage、记忆选择、生命周期审计、bundle 哈希和 chapter 投影一致。写作投影仍约 4.7 千字符，不因测量链长增加而塞入全部账本。逐次耗时、P95、平台、输入及源文件哈希均保存；五次 P95 即最大值，不作为稳定服务指标。

## 旧摘要路径审查

本轮只审查，不静默修改提示和历史基线：

| 调用点 | 实际行为 | 判断与后续 |
| --- | --- | --- |
| `api_narrative` 主写作 prompt | 先计算旧 `reading_history`，bundle 存在时最终用 `chapter` 连续性与动态记忆覆盖 | 主路径不是直接注入旧 5000 字符；早期计算可延后，但只属于小开销 |
| 无 bundle 的写作兼容路径 | 保留按时间截取的 `reading_history` | 未完成按需选择；需区分允许的兼容模式和应该明确拒绝的上下文缺失 |
| `readerInterlude` 分支 | 依据 `chapter_history` 是否非空决定回退，而非依据 bundle 是否存在 | 空连续性窗口可能回退旧摘要；属于兼容路径风险，需要单独判断当前产品是否可达并补覆盖 |
| 当前行动审核 `review_messages.previous` | 即使存在 bundle，仍加入 `reading_history(context['lineage'])` | 明确遗留：可能带入无关旧摘要，不能以 5000 字符上限替代相关性选择 |
| 事实抽取 | bundle 可用时传 `fact_extract`；仅无 bundle 时传旧 `previous` | 现有主路径已分离，兼容边界仍需覆盖 |
| 本地 `continuity_check` 和旧路线审核 | 仍读取历史摘要作为核查依据 | 本地确定性核查不等于写作注入；迁移时不能连校验输入一并裁掉 |

行动审核也会接收当前完整权威状态和结构化连续性，二者用途与写作提示不同。后续应按审核任务提取带来源的相关历史，保留核查本回合授权、连续性及后果的必要依据，不能直接把旧摘要替换为空字符串来减少字符数。

## 下一步顺序

1. **优先回到质量主线**：设计并实现行动审核的历史投影，按本回合行动、涉及实体、已选来源和未结义务选取；对长链无关历史、旧事实失效、复合行动、指代和缺证据分别验收。正文、审核、修复共享来源 ID 和同一快照。
2. 清理写作/事实抽取/插叙的兼容回退契约；bundle 缺失和合法空窗口必须区分，不能空窗口就扩展旧历史。未确认当前产品可达的旧路线不算完成验收。
3. 补完整真实正文—审核—修复—提交的有界实验，保留失败与原始证据；离线通过不替代正文效果。
4. 历史压缩存储保留为后续性能工作。当前先不扩大到数据库迁移；再次优化须用相同夹具证明收益且不改变上述校验。
5. 上下文质量验收完成后再推进既定剧情配图及生图提示/延迟优化。

## 验证

新增读取指纹测试 11 项通过；最终完整 API 回归 514 项通过（78.855 s）；核心上下文/bundle/提示集成定向回归 79 项通过（1.669 s）。各组重叠，不相加计数。`git diff --check`、文档相对链接与最终测量源文件哈希核对通过。

完整 core 未重跑；先前记录的 `consequences.plan` 提示哈希差异未处理，未重写提示基线。真实模型和正文质量未验收。

覆盖只读不初始化、无持久写入、输入不变、投影/审计等价、兄弟分支隔离、根父兼容、内容/排版/序号变更、伪造 ID、跨会话父节点、断链、循环、事务快照及回滚。既有祖先变化导致旧快照和旧草稿失效的回归继续保留。

```sh
.venv-api/bin/python -B -m unittest tests_api.test_lineage_fingerprint tests_api.test_longform_closing_planning tests_api.test_turn_drafts tests_api.test_turn_snapshot_benchmark
.venv-api/bin/python -B -m test_support.history_fingerprint_benchmark --output /tmp/history-fingerprint-paired.json
.venv-api/bin/python -B -m test_support.run api
python3 -B -m unittest tests_py.test_context_bundle tests_py.test_context_memory_longform tests_py.test_context_prompt_integration
git diff --check
```
