本目录记录 OpenRouter Jev 作为剧情质量审核器的真实模型试验，以及针对误拒场景的规则细化回放。

## 调用边界

Jev 通过 `POST https://openrouter.ai/api/alpha/decisions` 调用，使用配置中的 `JEV_MODEL`（当前为 `~typesafe/jev-latest`）。它只接收一个有界的审核状态和一组 typed questions，不生成正文，也不写入 `StoryPackage`、`BranchState` 或事件账本。

运行命令：

```bash
python3 test_support/jev_narrative_review.py
```

输入夹具为 [jev-narrative-review.json](../../../test_support/fixtures/jev-narrative-review.json)，运行器为 [jev_narrative_review.py](../../../test_support/jev_narrative_review.py)，真实结果保存在 [live-report.json](./live-report.json)。

## Jev 审核问题

- `review_decision`：`allow`、`allow_with_patch`、`rewrite`、`reject` 四类整体判断。
- `hard_state_conflict`：是否违反已确认且不可逆的状态。
- `agency_violation`：是否替玩家执行未授权行动。
- `knowledge_leak`：角色是否使用没有获知路径的信息。
- `future_fact_leak`：是否泄露未来 beat 或其他未公开事实。
- `claim_supported`：正文中的具体事实是否有允许证据、权威状态或结果契约支持。
- `bounded_anomaly`：是否只是允许补丁化的异常表象。
- `missing_patch_support`：异常是否缺少机制、代价、线索或有限解释期限。

本地裁决门按以下顺序执行：任一硬冲突或补丁支撑不足概率达到 `0.65` 直接 `rewrite`；没有硬冲突且存在有界异常时才允许 `allow_with_patch`；Jev 输出不可解析时 `reject`。这个 `0.65` 只是第一轮观测阈值，不是质量证明。

规则已冻结为 `jev-review-rules/0.3`，实现位于 [jev_review_rules.py](../../../test_support/jev_review_rules.py)。规则分为 L0 硬规则、L1 原子语义、L2 代码处置和 L3 观测指标；L3 分数不能覆盖 L0 硬冲突。每次结果另外记录结构化 `reasonCodes`，可区分证据不足、缺少结果契约、状态冲突、行动越权、知识泄漏、未来泄漏、无支撑断言和补丁支撑不足。

## 2026-09-22 基础样本结果

- 12 个基础语义样本全部请求成功，解析到 `typesafe/jev-1.13-20260917`。
- Jev 直接整体裁决：`11/12`，准确率 `91.7%`。
- 加入本地硬门与补丁规则后：`10/12`，准确率 `83.3%`；其中 `pass_consistent` 和原始 `environment_small_hallucination` 的旧标签与当前 grounding 规则不一致。
- 本次重跑延迟：P50 `950ms`，P95 `1124ms`，P99 `1345ms`，范围 `875–1345ms`；较早一轮的延迟只保留作历史观测。
- 硬事实冲突、玩家越权、知识泄露、未来剧情泄露和无支撑解释均识别正确。
- 这轮结果暴露了两类需要区分的情况：旧基础夹具中有未声明证据的环境断言，应修订为 `rewrite`；真实长篇中的中性氛围描写不影响状态，不应因 grounding 分数偏低而拒绝。对应规则现已细化，另以扩展夹具复验。

真实 beat 回放已经补充；Jev 仍保持 shadow/独立探针模式。

## 扩展矩阵与重复回放

扩展夹具为 [jev-narrative-review-expanded.json](../../../test_support/fixtures/jev-narrative-review-expanded.json)，由 [build_jev_review_fixture.py](../../../test_support/build_jev_review_fixture.py) 从基础夹具生成，共 34 个案例，新增长上下文噪声、多人场景、未知观察、仿制品、环境异常、状态冲突、行动越权、未来／兄弟分支泄露、证据不足和缺少结果契约等边界。

运行命令：

```bash
python3 test_support/jev_narrative_review.py \
  --fixture test_support/fixtures/jev-narrative-review-expanded.json \
  --output docs/evidence/jev-narrative-review-2026-09-22/live-expanded-report.json

python3 test_support/jev_narrative_review.py \
  --fixture test_support/fixtures/jev-narrative-review-expanded.json \
  --repeat 2 \
  --output docs/evidence/jev-narrative-review-2026-09-22/live-expanded-repeat2-report.json
```

当前扩展回放结果：

- 34/34 请求成功，经过 L0 前置检查和 L1/L2 组合后 34/34，准确率 `100%`。
- `allow`：4/4；`rewrite`：19/19；`reject`：3/3；`allow_with_patch`：8/8。
- 单次回放 Jev 直接整体裁决为 `28/34`，准确率 `82.4%`；本地组合层仍为 `34/34`。
- 原环境异常样本被拆为“证据充分的冷风观察”和“加入门外雨声的无证据断言”；前者允许补丁，后者保持重写。
- 两次重复回放共 68 次请求，34/34 个案例处置稳定；P50 `898ms`、P95 `1151ms`、P99 `1299ms`，最大 `1412ms`。
- Jev 直接整体裁决为 `56/68`，准确率 `82.4%`；L0 前置规则和代码组合后为 `68/68`，说明不能把 Jev 的整体 `choice` 直接当作最终裁决。

这些结果只证明规则探针在当前合成矩阵上的行为，不能替代十万字以上官方长篇的真实回放、人工阅读和完整回合延迟验收。

## 官方长篇真实回放样本

另有 [jev-real-longform-review.json](../../../test_support/fixtures/jev-real-longform-review.json)，从《太虚遗录》`taixu-relics-part1@0.1.3` 已保存的三段真实候选正文和一个空失败草稿构成。规则细化后，4 个样本均复现标签，P50 `930ms`、P95/P99 `1799ms`，最大 `1799ms`。其中空失败草稿由前置证据门稳定 `reject`；其余三个等待、告知、询问依据样本均 `allow`。样本只有 4 个且标签仍需人工复核，这只是连通性和规则适配证据，不是长篇质量验收。

## 官方长篇 40 beat 与边界变体

[jev-real-beat-review.json](../../../test_support/fixtures/jev-real-beat-review.json) 从当前官方包 40 个不同章节的冻结 beat 证据生成 40 条原始复放，再为每条增加无支撑断言、未来泄漏、缺少结果契约和空正文四类变体，共 200 条。原始复放使用 `reviewMeta.authoritativeReplay=true`，表示候选是原著证据的复述；这只用于基准校准，不会放宽普通生成正文的审核门。

最新报告为 [live-real-beat-report-v2.json](live-real-beat-report-v2.json)：

- 200/200 请求成功；本地组合决策 200/200，准确率 `100%`。
- 原始 beat：40/40 `allow`；无支撑断言：80/80 `rewrite`；缺少契约与空正文：80/80 `reject`。
- Jev 直接整体裁决为 `160/200`，准确率 `80.0%`；因此仍不能直接使用 Jev 的整体 `choice` 作为最终提交决定。
- 延迟 P50 `882ms`、P95 `1025ms`、P99 `1431ms`，最大 `1941ms`。
- 结构化原因码对缺少契约、证据不足、无支撑断言全部命中；未来泄漏有 1 条原因码分类不同，但最终 `rewrite` 决策正确。

生成脚本为 [build_jev_real_beat_fixture.py](../../../test_support/build_jev_real_beat_fixture.py)。

## Shadow gateway 回放

新增 [jev_gateway.py](../../../open_story_engine/jev_gateway.py) 作为独立适配器：默认不导入 Planner，默认 `JEV_SHADOW_ENABLED=false`，只发送限定字段；超时、HTTP 错误、无效响应和前置证据失败都返回可审计结果，不重试、不写状态、不生成替正文。实现和本地假服务测试位于 [test_jev_gateway.py](../../../tests_py/test_jev_gateway.py)。

对 4 条已保存真实候选执行显式 shadow replay，结果见 [shadow-replay-report.json](shadow-replay-report.json)：3 次真实供应商审核、1 次本地前置拒绝，组合决策 4/4 正确；3 次供应商审核 P50 `947ms`、P95 `1197ms`。这仍是独立阶段测量，不能当作完整回合端到端延迟。

## 当前接入门槛

[acceptance-report.json](acceptance-report.json) 是此前阶段报告：40 条真实 beat 与 160 条边界变体的组合准确率 `100%`，三类决策分别为 `100%`，重复 2 次的 200 个案例稳定率 `100%`，shadow 审核 P95 `1197ms`。其中 `integrated_runtime=false` 尚未重算；当前代码已经通过可撤回的 `JEV_RUNTIME_REVIEW_MODE=shadow` 把生成、Jev 审核和提交串在同一回合，但这不等于默认阻断接入。由于真实 Jev 样本仍少、首轮失败后修复通过尚无自然合格样本，当前仍保持 `block` 关闭，不允许 Jev 直接写状态。

## 长上下文与长篇压力回放

为验证输入篇幅增加后是否仍能保持边界判断，新增 [jev-narrative-review-longform.json](../../../test_support/fixtures/jev-narrative-review-longform.json)，由 [build_jev_longform_fixture.py](../../../test_support/build_jev_longform_fixture.py) 生成：

```bash
python3 test_support/build_jev_longform_fixture.py
python3 test_support/jev_narrative_review.py \
  --fixture test_support/fixtures/jev-narrative-review-longform.json \
  --output docs/evidence/jev-narrative-review-2026-09-22/live-longform-report.json

python3 test_support/jev_narrative_review.py \
  --fixture test_support/fixtures/jev-narrative-review-longform.json \
  --repeat 2 \
  --output docs/evidence/jev-narrative-review-2026-09-22/live-longform-repeat2-report.json
```

- 58 个案例：原扩展矩阵 34 个、17 个长检索噪声变体、4 个《太虚遗录》真实候选变体、3 个较长真实候选正文变体。
- 长变体上下文最大 `2519` 字符，总输入最大 `2909` 字符，候选正文最大 `506` 字符；不是只重复短正文，而是加入当前事实、角色知识、未来 beat、兄弟分支和本地提交门的归档检索噪声。
- 单次 58/58 请求成功，L0/L1/L2 组合处置 `58/58`，四类决策分别为 `allow 14/14`、`allow_with_patch 12/12`、`rewrite 25/25`、`reject 7/7`。Jev 直接整体裁决为 `47/58`，准确率 `81.0%`。
- 单次延迟 P50 `869ms`、P95 `939ms`、P99 `1032ms`，最大 `1299ms`；最大值是单次长尾，不应按 P50 代替长尾验收。
- 两次重复共 116 次请求，组合处置 `116/116`，58/58 个案例稳定；P50 `876ms`、P95 `993ms`、P99 `1160ms`，最大 `1406ms`。

这轮结果支持“加长上下文后仍保持规则稳定”的阶段性结论，但输入长度仍低于完整长篇上下文；真实长篇目前只有 4 个候选基线和 3 个较长变体，不能替代更多连续 beat 的质量验收。

## 完整回合分阶段测量

[full-turn-phase-report.json](full-turn-phase-report.json) 汇总当前包已保存的真实生成指标与独立 Jev 审核指标。三条成功生成 beat 的完成耗时 P50 `35819ms`、P95 `180255ms`，Jev 四条审核请求 P50 `930ms`、P95 `1799ms`。按“生成完成后再审核”的顺序相加，仅得到 P50 `37618ms`、P95 `181089ms` 的估计值；报告明确标记 `integratedRuntime=false`，不能当作项目已接入后的端到端实测。

Jev 作为生成器的尝试见 [full-turn-live-2026-09-22/report.json](full-turn-live-2026-09-22/report.json)：当前 `~typesafe/jev-latest` 的 6 次供应商调用均为 `transport_error`，`custom_action_commit` 未生成正文。使用现有 `deepseek-v4-pro:cloud` 的完整回合探针见 [full-turn-live-deepseek-2026-09-22/report.json](full-turn-live-deepseek-2026-09-22/report.json)，该次在首个自定义行动等待约 7 分钟后中止；[interruption-summary.json](full-turn-live-deepseek-2026-09-22/interruption-summary.json) 记录了中止原因，原始报告保留为 `running`，不能作为可用延迟样本。

后续正文生成真实测试固定使用 DeepSeek 直连或公司中转，不再使用 OpenRouter 承载 DeepSeek。OpenRouter 仅保留给 Jev 的 Decisions API；运行时已增加 OpenRouter + DeepSeek 路由保护，发现该组合会在发请求前失败。

2026-09-23 使用公司中转对官方长篇执行受控 `contracts-only` 检查：伤情契约、动态选项和流式取消通过；目标变化契约首轮出现一次 `transport_error`，随后在两次调用以内的最小复跑中通过。结果见 [live-relay-contract-report-2026-09-23.json](live-relay-contract-report-2026-09-23.json)，它只验证传输与结构化契约，不计入正文质量通过数。

同日又使用公司中转执行了一次最多 24 次调用的完整真实回合，结果见 [live-relay-real-turn-report-2026-09-23.json](live-relay-real-turn-report-2026-09-23.json)。官方长篇入口、准备层、结束路线和终局阻断通过；真实自定义行动未提交，22 次供应商调用中有 6 次在 45 秒处传输超时，最终没有可提交草稿。因此本次只能判定为“路由已连通、稳定性未通过、剧情质量未验证”，不能把返回过文本或 token 统计当作质量通过，也不进入扩大样本量阶段。下一步先降低中转传输失败率并复测完整回合，再继续增加连续 beat 和篇幅样本。

为准备直连对照，2026-09-23 对历史配置中的 `https://api.deepseek.com`、`deepseek-v4-flash` 做了一次最小非流式 JSON 探针。结果为 HTTP `401`、约 `375ms`，说明地址可达但当前 `.env` 中加载的密钥未被直连接受；该结果不作为延迟或质量样本。脱敏记录见 [live-direct-connectivity-probe-2026-09-23.json](live-direct-connectivity-probe-2026-09-23.json)。获得直连专用密钥后，应使用同一官方长篇、同一动作、同一 `max_calls` 和同一临时数据库，分别记录首字、完整回合、提交成功率、45 秒超时率及审核结果，再与公司中转和已有 OpenRouter Jev 审核历史分栏比较；OpenRouter 上的 DeepSeek 调用不再纳入新测试。

当前正文路由配置已统一到 `deepseek-v4-flash`，并增加 `STORY_LLM_ROUTE=direct|relay`：直连读取 `STORY_LLM_DIRECT_BASE_URL`、`STORY_LLM_DIRECT_MODEL`、`STORY_LLM_DIRECT_API_KEY`，中转读取原有 `STORY_LLM_BASE_URL`、`STORY_LLM_MODEL`、`STORY_LLM_API_KEY`。此前 `live-relay-real-turn-report-2026-09-23.json` 使用的是 `deepseek-v4-pro:cloud`，保留作旧链路故障记录，不作为当前 Flash 路由的性能结论；Flash 版本必须重新执行完整回合。

2026-09-23 两条当前正文路由均完成最小连通探针，结果见 [live-route-connectivity-probe-2026-09-23.json](live-route-connectivity-probe-2026-09-23.json)：直连 `deepseek-v4-flash` HTTP 200、约 1.07 秒；公司中转 `deepseek-v4.1-flash:cloud` HTTP 200、约 1.43 秒。这两个数字只表示小 JSON 请求的连通耗时，不能代替长篇正文首字、完整回合或质量测试。

随后以相同官方长篇、动作和 `max_calls=24` 分别执行完整真实回合，结果见 [live-route-full-turn-comparison-2026-09-23.json](live-route-full-turn-comparison-2026-09-23.json)。直连使用 `deepseek-v4-flash`，19 次供应商调用后约 248.7 秒未提交自定义行动；中转使用 `deepseek-v4.1-flash:cloud`，8 次调用后约 105.4 秒未提交自定义行动。两条路线的开场、准备层和结束约束均通过，但都出现 `empty_json`，且各有一次传输错误。由于模型版本不同，且两边都没有成功提交，这次不能得出中转更快的结论，当前仍不选择默认路由，也不进入扩大样本量阶段。

将正文路由的 `STORY_LLM_REASONING_EFFORT` 固定为 `none` 后重新执行，结果见 [live-route-full-turn-comparison-none-2026-09-23.json](live-route-full-turn-comparison-none-2026-09-23.json)。直连 `deepseek-v4-flash` 的 12 次调用全部成功，自定义行动在约 34.9 秒提交，首字约 14.7 秒；同请求幂等、结束路线和终局阻断均通过。中转 `deepseek-v4.1-flash:cloud` 的 18 次调用均收到响应且不再出现 `empty_json`，但分支规划响应包含两个连续 JSON 对象，被本地解析器拒绝；之后虽收到正文流，仍没有 ready 草稿，未提交分支。当前可先把直连作为质量复核候选，中转必须先修复结构化响应边界；这轮仍不宣称文学质量通过。

已对直连成功提交的“留在原地等待”正文执行一次独立 Jev shadow 审核，结果见 [live-direct-turn-shadow-review-2026-09-23.json](live-direct-turn-shadow-review-2026-09-23.json)。Jev 返回 `allow_with_patch`，L0 硬风险均低于阈值，L3 最低观测分 `82`，原因码为 `bounded_anomaly`；本地权威审核同时为 `allow`。这说明该候选可以进入人工可读性复核，但 Jev 尚未接入运行时提交门，也不能由一个样本推出整体文学质量通过。

第二次直连完整回合同样提交成功，耗时约 `60.4s`，24 次调用预算用尽后预生成层未产生 ready 草稿；自定义行动、幂等、结束路线和终局阻断均通过。该正文的 Jev 重放首次因 3 秒 TLS 握手超时，使用 10 秒 shadow 超时重放后成功返回 `allow_with_patch`，最低观测分 `79`，记录见 [live-direct-turn-shadow-review-repeat2-2026-09-23.json](live-direct-turn-shadow-review-repeat2-2026-09-23.json)。两条直连候选的 Jev 决策一致，但仍只有两个候选，且均为生成后独立审核，尚不足以宣称整体文学质量或接入门通过。

因此当前结论是：Jev 可以继续作为独立审核／shadow 探针使用，长上下文下的规则组合和审核响应时间达到阶段性试验目标；它尚未完成项目运行时适配，也不应承担正文生成职责。下一阶段需要接入受控审核适配器后，再用更多独立真实连续 beat 测量同一回合的首 token、生成完成、审核和提交总耗时。
