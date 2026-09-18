本报告记录 P0 checkpoint 接入后的只读确定性复现审计。它不包含真实模型调用，不构成 NQ-001 通过声明。

## 接入

- P1 基线：`f635919`，其评估器、夹具、测试和旧审计报告均保留。
- 接入的 P0 checkpoint：`e91345f50e207b74b0b2656b3165f1e6c469a761`。
- 当前提交：`f136421`（`checkpoint: P0 workspace handoff`）。接入无冲突，工作树在审计前干净。
- 本轮仅增加新的审计证据和本报告；没有修改母本正文、StoryPackage、P0 业务逻辑或旧版本目录。

## 复现审计

证据：`docs/evidence/p1-nq001-repro-audit-p0-2026-09-18/report.json`。

| 项目 | 结果 |
| --- | --- |
| 母本 | 《太虚遗录》第一部，50 章 |
| CJK 字数 | `102610`（门槛 `100000`） |
| 母本 SHA-256 | `61efaffb17d652ccc0fcb15b98a133fd976296d342966c0a426b7831f9810904` |
| StoryPackage | `taixu-relics-part1@0.1.2`，来源 SHA 一致 |
| reader 来源绑定 | 通过，50 章，来源 SHA 一致 |
| modules/package-index | 通过；模块索引 SHA `3c5dec0ffad1a41bd6063fd8b91005db7d630c96f8f3f394bc72413d7dc5bdc2` |
| 模块索引与构建器审计 | 通过；670 modules（50 chapter、343 beat、3 entry、7 character） |
| 固定母本重建 | **失败**；`analysis.json` 重新构建与当前 `package.json` 有字段差异，详见 JSON 的 `rebuild.differences` |
| 版本重复检查 | 未发现同一 ID 的重复版本目录 |

因此，reader/modules/package 的来源绑定和模块内部审计已通过，但“固定母本可重新构建当前包”这一复现闸门仍失败；不能把整项复现审计写成通过。

### 7 个身份、入口、开场证据

包内角色清单为 7 个：陆照临、陆沉舟、沈砚秋、顾长离、叶观澜、萧问蝉、叶青冥。结构测试 `test_longform_corpus` 与 `test_official_openings` 共 7 项通过，确认 3 个可玩入口和其独立开场账本：

- `entry_lu_gate` -> 陆照临
- `entry_gu_trial` -> 顾长离
- `entry_ye_gallery` -> 叶观澜

`official-openings-v1.json` 也只登记这 3 个 reviewed opening。陆沉舟、沈砚秋、萧问蝉、叶青冥在当前包中均为 `playable=false`、`defaultEntryPointId=null`，没有独立入口/官方开场证据；运行时只能作为 session-only 的通用角色上下文，仍是 P1 阻塞项。

### 重复候选分类

审计共报告 50 组候选，策略为只报告、不改正文、不用于补足字数：

- 5 组 exact：1 组为“记录留存，选择继续。”的重复操作性占位句；4 组为重复正文段落。状态均为 `待人工复核/未修复`。
- 45 组 near：由行 `3833`、`4017`、`4219`、`4425`、`4607`、`4815`、`5029`、`5239`、`5439`、`5651` 的同一段落块两两组合产生，属于同一重复段落族的近似配对。状态均为 `待人工复核/未修复`。

这些候选没有被静默视为通过；本轮没有修改母本，也没有按候选扣除或复制正文来满足字数。

## 当前 longform 测试盘点

- `python3 -B -m test_support.run core`：34 项当前核心测试通过；历史短篇用例 188 项保留源码但按规则不执行、不计入当前通过数。
- `python3 -B -m unittest tests_py.test_longform_corpus tests_py.test_official_openings -v`：7 项通过。
- `python3 -B -m test_support.check_scene_library`：1 部长篇、3 项素材、1 条实时规则，`status=valid`，错误数 0。
- `git diff --check`：通过。
- 本轮没有运行真实模型、API 实时模型验收或 `nq001_live_eval.py`；上述结果全部是离线/确定性检查。

## 阻塞与下一步

P1 仍被以下问题阻塞：当前包仍为 `0.1.2`，仓库规则要求升级使用新版本而不是覆盖旧包；四个身份缺少独立官方开场；固定母本到当前包的重建仍不一致；50 组重复候选尚未人工定性和修复。故当前不具备运行真实模型的条件，也不能宣称 NQ-001 通过。
