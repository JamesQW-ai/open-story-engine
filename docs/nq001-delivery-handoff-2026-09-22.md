# NQ-001 明日交付记录（2026-09-22）

## 交付结论

本次停止永久损毁道具的真实模型重试。当前交付状态为：**未通过，不能交付为已验收功能**。

- 最新真实评估记录：`a334b33a4c1d4535abe5aad0d71f8b2d5b6fdb17`
- P0 修复基线：`71030651757c9f9314a0040fb820f8b90c26a1d9`
- StoryPackage：`taixu-relics-part1@0.1.3`
- 固定请求：`我永久损毁道具：引荐文书。`
- 数据库：`/private/tmp/nq001-live-20260921-abandon-goal.sqlite`
- 会话：`615a8714-854e-5cfe-a448-4ed98fd5b142`
- 父分支：`branch_21d02a70-09c3-4f85-b855-efa4e031a1af`

本次未修改运行时代码、提示、StoryPackage 或数据库。

## 真实证据归档

完整脱敏证据已归档至：

`docs/evidence/nq001-live-2026-09-21-lu-route-destroy-item-final-v3/`

目录共 24 个文件，保留：

- 请求与 SSE：`calls/0005-turn-request.json`、`calls/0005-turn.sse`
- 两次 planner 原始响应、规范化审计、失败字段和未确认草稿：`calls/0010-turn-drafts.json`
- 失败后和服务重启后的状态/分支读取：`calls/0006`、`0007`、`0012`、`0013`
- 用量：`calls/0008`、`calls/0014`
- 数据库盘点：`calls/0011-db-*.json`
- 运行配置：`run-config.json`
- 阶段摘要：`stage-summary.json`
- 本轮原始报告：`report.md`

## 首个失败与字段证据

首个失败阶段明确为：

`branch_planner contract validation`

失败码为 `generation_failed`，HTTP 状态为 503。两次 provider planner 响应都没有形成可接受的 canonical plan；不是规则拒绝，也不是模型正文失败。

失败字段：

| JSON path | 证据中的问题 |
| --- | --- |
| `$.method` | 写入抽出、对折、撕扯及 NPC 可能反应 |
| `$.steps[0].action` | 写入对折、撕扯 |
| `$.stateChanges[0].reason` | 用具体动作解释永久损毁 |
| `$.scenePlan.outcome` | 写入撕碎及守门弟子目睹 |
| `$.scenePlan.lengthReason` | 以撕扯和 NPC 即时反应规划篇幅 |
| `$.scenePlan.beats[0].purpose` | 写入具体撕扯动作 |
| `$.scenePlan.observationLimits[0]` | 将撕毁全过程和是否目睹写入 canonical 限制 |
| `$.scenePlan.observationLimits[1]` | 将碎裂、撕扯写入 canonical 限制 |

第二次响应已经保留：

- `destroyedPermanently=true`
- `item_open_letter` 当前由玩家持有的依据
- 空的 `scenePlan.knowledge`
- `pending + requiresConfirmation=true` 的 `narrativeOptions[]`

但仍未通过，因为具体动作和 NPC 目睹/反应暗示继续泄漏到 canonical 字段。

## 场景分类

### 已通过的真实场景

无。本轮固定请求只有一个真实模型场景，未形成 planner 通过、authority 通过、正文通过或提交凭证。

此前同一数据库中由 mock 规划器产生的放弃目标子分支不属于本轮真实模型结果，也不能作为永久损毁成功证据。

### 未通过的真实场景

- 场景：陆照临永久损毁引荐文书
- 结果：两次 planner 尝试均在 `branch_planner contract validation` 失败
- provider 调用：2 次
- 报告 tokens：21279
- SSE：`generation_failed`，无 `done`
- 未形成可接受正文或状态提交

### 失败但状态未污染的场景

同一永久损毁请求满足状态安全边界：

- 失败前、失败后和重启后 `state_version=1`
- 引荐文书仍由陆照临持有
- `destroyedPermanently=false`
- 无新增真实分支
- `game_events=0`
- `event_narrations=0`
- 未确认正文为空，状态为 `unconfirmed`

### 尚未执行的场景

由于 planner 未通过，以下阶段均未形成可验收结果：

- `action_authority`
- 正文生成
- `full_review`
- `local_repair`
- 修复后复核
- 原子状态提交
- 永久损毁后的后续读取和依赖路线验证

### 不能宣称的验收项

当前不能宣称：

- 永久损毁请求已支持
- planner canonical/pending 边界已通过真实模型验证
- `action_authority` 已通过
- 正文事实审查或修复闭环已通过
- 道具状态已成功提交
- NQ-001 真实模型验收通过
- StoryPackage `0.1.3` 已具备该场景的交付证据

## 道具状态

引荐文书永久损毁当前标记为：

**未验证，planner canonical/pending 边界阻塞**

该状态不能写成规则拒绝，也不能写成模型正文失败。失败发生在 planner 输出进入下游前的 canonical 字段契约校验。

## 下一阶段技术方向

重新设计后再进行一次真实复测，方向固定为：

1. 代码必须拥有独立、可校验的 canonical consequence，包含玩家明确请求的抽象永久后果、目标道具和当前持有依据。
2. 模型生成的具体叙事候选必须与 canonical plan 分离，只能进入待确认的 narrative options。
3. 不能静默删除模型输出中的具体动作；遇到 canonical 泄漏必须返回可读契约错误并保留原始失败证据。
4. 新设计完成后，使用同一数据库、同一 session/父分支、同一 StoryPackage 和固定请求重新进行一次真实复测。

本记录只用于明日交付交接，不代表永久损毁功能已经验收。
