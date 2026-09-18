本批补齐 P6 收束准备检查，把待交代清单接入规划数据投影，并在手记 journey 中暴露停滞信号与收束 readiness。清单通过不表示自然结局已写成。NQ-001 与 SRC-001 保持未解决。

## 已实现

### 收束准备接口

新增只读接口 `GET /api/v1/sessions/{session_id}/route-closure?branch_id={branch_id}`，契约版本 `route-closure/1`。`route_closure.preparation` 基于同一祖先链上的局部大纲重新整理，不写库、不迁移、不调用模型。

| 字段 | 含义 |
| --- | --- |
| `outstanding` | 收束前须交代的条目：`goal`／`thread`／`dependency`，含原因、是否必需披露、阻塞人物与正文证据 |
| `cleared` | 已有祖先链证据证明完成或有依据关闭的条目 |
| `coverage` | 目标账本、问题账本、分支状态是否已登记；缺失即为 `unknown`，不当作已解决 |
| `readiness` | `ended` / `blocked_unknown` / `blocked_dependency` / `needs_explanation` / `checklist_clear` |
| `ending_written` | 恒为 `false`；`checklist_clear` 也不表示结局已写成 |
| `note` | 明确声明：通过清单检查不等于自然结局已写成或叙事质量验收通过 |

判定规则：

- 开局未写入账本时，以已验证的官方开局义务为准；后续分支缺账本则记为 `unknown`。
- `active` 目标与 `open` 问题进入待交代；`unknown` 状态阻塞 readiness。
- 目标依赖已死亡／离队／失踪人物时，同时列出目标条目与 `dependency` 条目。
- 账本自称完成但缺祖先链正文证据时，列入待交代，不静默放行。
- 路线已 `end_route` 后 `readiness=ended`，仍保留未交代清单，不伪装成全部已解决。

### 规划数据投影

`reader_consequences.planning_context` 新增 `closure` 字段，由 `route_closure.closure_projection` 生成：

- 只包含 `required_disclosure` 的待交代条目（`kind` / `target_id` / `title` / `reason` / `blockers`），不含正文全文与调试字段。
- `readiness=checklist_clear` 时 `outstanding` 为空，并保留“不表示自然结局已写成”的 `note`。
- 大纲构建失败时返回 `readiness=unknown` 且 `outstanding=[]`，不中断回合，也不伪装成清单已通过。
- 只增加数据字段，不修改后果规划提示词措辞与校验规则；收束正文仍须独立生成与验收。

### 手记 journey 的路线健康诊断

`GET .../journey` 新增只读字段 `route_health`：

| 字段 | 含义 |
| --- | --- |
| `signals` / `review_recommended` | 来自 `route-monitor` 的停滞信号（状态未变、重复行动、重复正文） |
| `closure_readiness` / `closure_outstanding_count` | 收束准备清单的 readiness 与待交代条数 |
| `ending_written` | 恒为 `false` |
| `note` | 声明诊断不强制结束、不表示结局已写成 |

- 长篇路线仍不显示虚构完成百分比（`progress=null` 逻辑不变）。
- 账本损坏时 `closure_readiness=unknown`，不再把 journey 整页读失败；手记目标列表对非法账本降级为空列表，健康诊断保留 unknown。
- 前端类型已声明可选 `route_health`，本轮未改手记 UI 布局。

## 验证

自动遍历 `test_support.longform` 全部达标小说（当前为《太虚遗录》第一部 `taixu-relics-part1@0.1.2`）。

新增专项：开局义务与非结局声明、完成证据清空清单、依赖阻塞、未知账本、结束路线保留待办、兄弟分支隔离、只读 HTTP、无效历史、缺失数据库；规划投影携带待办、大纲损坏时 unknown、有证据后 clear；journey 暴露停滞与收束诊断、损坏账本 unknown。

- `.venv-api/bin/python -B -m test_support.run api`：**304 项通过**
- `python3 -B -m test_support.run core`：**25 项通过**
- `git diff --check` 通过
- 本轮未调用真实模型；没有测试自然终局正文或输出质量。

实现：`open_story_engine/route_closure.py`、`reader_consequences.planning_context`、`api_journey.route_health`、`api_read.route_closure`、`api.py` 路由、`api_models.RouteClosurePreparation`；回归 `tests_api/test_longform_route_closure.py`、`tests_api/test_journey_display.py`。

## 下一步与剩余边界

### 账本完整性补修（2026-09-16）

本轮发现并修复以下确定性问题：

- 当前账本字段存在但条目被删除时，原实现可能把空清单判为通过。现在从官方开局、本分支祖先账本与已登记更新盘点目标／问题，缺项逐条进入待核实清单，`coverage=unknown`、`readiness=blocked_unknown`。兄弟分支不参与盘点。
- 转化目标的后继记录可能遗漏。现在按正式提交的 ID 规则核对后继目标；后继缺失不能靠旧目标的 `transformed` 状态通过检查。
- 有正文证据的目标放弃／转化曾因 `completion.met=false` 被误判为证据不一致。现在允许其作为已交代事项进入 `cleared`，明确区分“放弃／转化”和“目标达成”；开放问题放弃追查也不等于已查明。
- 临时失踪曾被描述为永久下线。现在保留死亡／离队／失踪的区分，状态恢复后依赖阻塞解除。
- 历史账本或分支状态缺失时，即便当前记录齐全，也保留历史覆盖范围为 `unknown`，增加明确的历史核查条目。损坏账本在规划投影中仍降级为 `unknown`。本轮不自动补写旧历史。

上述判定由既有 `route-closure`、规划 `closure` 与故事手记 `route_health` 共用；未修改提示词、生成和审查流程，未调用真实模型。

验证：新增 8 项长篇回归，收束专项共 **22 项通过**；最终全量 **312 项 API、25 项核心通过**，`git diff --check` 通过。全部测试使用临时数据库，自动盘点所有达标母本，目前仍为《太虚遗录》第一部（102610 CJK，`taixu-relics-part1@0.1.2`）。未改前端，本轮未进行桌面试玩或自然结局验收。

历史缺失会保守阻塞收束准备，不能仅靠后续账本恢复齐全消除；后续若提供历史修复，须另有明确证据和修复流程。清单不能识别从未登记的语义伏笔，也不能证明自然结局已经写成。下一步仍是收束阶段及不同结束类型的契约，不以清单为空自动结束路线。NQ-001 与 SRC-001 保持暂缓。

规划数据已能读到待交代清单，手记也能读到停滞与收束诊断。2026-09-17 已补充收束提示词封口：`closing` 只能处理已有目标和剧情问题账本条目，证据不足保持 `unknown` 或请求澄清；详见 [收束阶段提示词封口](closing-prompt-guardrails-2026-09-17.md)。自动强制收束和完整 P4 仍不属于当前已验收范围；完整自然结局仍须真实正文与未知原著视角试玩证明。
