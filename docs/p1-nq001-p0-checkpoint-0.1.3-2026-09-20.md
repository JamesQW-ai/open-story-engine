本报告记录 P0 checkpoint `0.1.3` 接入 P1 worktree 后的只读确定性复现审计。它不包含真实模型调用，不构成 NQ-001 通过声明。

## 接入

- P1 worktree：`/Users/apple/.codex/worktrees/p1-nq001-evaluation-audit/open-story-engine`
- 分支：`p1-nq001-audit-0.1.3`
- 基底：`origin/main` / `9cb10a20e0f1c35187ab4b3ec39a7a91d569533a`（`checkpoint: P0 official openings package 0.1.3`）
- 保留的 P1 提交内容（工具/测试/报告未丢）：
  - `da95d04` ← 原 `f635919` `test: add NQ-001 live evaluation and reproducibility audit`
  - `3d2e552` ← 原 `2fbee8d` `docs: record P0 reproducibility checkpoint audit`
- 接入方式：直接 merge 会在并行 workspace handoff 上产生冲突；改为以 `9cb10a2` 为基底 cherry-pick 上述两个 P1 提交。旧版本包目录未覆盖，`taixu-relics-part1@0.1.2` 仍在库且未改动。
- 本轮不修改母本正文，不修改 P0 业务逻辑，不运行真实模型。

## 复现审计（0.1.3）

证据：

- `docs/evidence/p1-nq001-repro-audit-0.1.3-2026-09-20/report.json`
- `docs/evidence/p1-nq001-repro-audit-0.1.3-2026-09-20/rebuild-full-pipeline.json`

| 项目 | 结果 |
| --- | --- |
| 母本 | 《太虚遗录》第一部，50 章 |
| CJK 字数 | `102610`（门槛 `100000`） |
| 母本 SHA-256 | `61efaffb17d652ccc0fcb15b98a133fd976296d342966c0a426b7831f9810904` |
| StoryPackage | `taixu-relics-part1@0.1.3`，来源 SHA 一致 |
| reader 来源绑定 | 通过，50 章，来源 SHA 一致 |
| modules/package-index | 通过；`moduleIndexSha256=cf63d8713beccc108a7c29655e3334e6ee858d6ce5e202ea6223dd9b01cb01c4` |
| 模块审计 | 通过；686 modules（50 chapter、343 beat、1 node、50 arc、7 entry、7 character、29 location、19 item、5 relationship） |
| 仅 analysis 重建 | 预期差异：不覆盖 opening review 字段 |
| 母本 + analysis + opening review + modules 重建 | **通过**；`package.json` 与 `reader.json` 与盘上 `0.1.3` 完全一致，模块索引 SHA 一致 |
| 版本重复检查 | 未发现同一 ID 的重复版本目录；`0.1.2` 与 `0.1.3` 并存 |
| 旧包 `0.1.2` | 仍在库，工作树干净，未被覆盖 |

因此：固定母本可按同一 analysis 与 opening-review 配置重建当前 `0.1.3` 运行包；reader/modules/package 来源绑定与模块内部审计通过。

### 7 个身份、入口、开场证据

包内 7 个角色均为 `playable=true`，且各自绑定独立 `defaultEntryPointId` 与正式开场账本；`official-openings-v1.json` 与 `0.1.3/opening-review.json` 同步登记 7 条 reviewed opening，均绑定同一母本 SHA。

| 身份 | 入口 | 开场 | 正式独立入口 | session-only |
| --- | --- | --- | --- | --- |
| 陆照临 | `entry_lu_gate` | 山门将闭 | 是 | 否 |
| 陆沉舟 | `entry_lu_ledger` | 石碑下的无名令 | 是 | 否 |
| 沈砚秋 | `entry_shen_register` | 名册上的空白 | 是 | 否 |
| 顾长离 | `entry_gu_trial` | 木牌中的金屑 | 是 | 否 |
| 叶观澜 | `entry_ye_gallery` | 对不上的旧记录 | 是 | 否 |
| 萧问蝉 | `entry_xiao_edict` | 没有签名的诏书 | 是 | 否 |
| 叶青冥 | `entry_ye_qingming_hall` | 掌门的回应 | 是 | 否 |

结构测试 `tests_py.test_official_openings` 与 `tests_py.test_longform_corpus` 通过，确认 7 个身份均有正式入口与开场，不再存在 session-only 开场。

### 重复候选分类（50 组）

策略仍为只报告、不改正文、不用于补足字数：

- 5 组 exact：
  1. 操作性占位句「记录留存，选择继续。」重复出现 8 处。
  2. 城墙灯火询问段落族，10 处。
  3. 灯册抄写留存段落族，10 处。
  4. 河滩点灯收束段落族，10 处。
  5. 守灯规约意见段落族，2 处。
- 45 组 near：由行 `3833/4017/4219/4425/4607/4815/5029/5239/5439/5651` 的同一段落族两两组合产生，相似度约 `0.9928–0.9976`。

状态：全部仍为 `待人工复核/未修复`。本轮未修改母本，也未按候选扣除或复制正文。

## 测试盘点

| 检查 | 结果 |
| --- | --- |
| `python3 -B -m test_support.run core` | 34 项通过；历史短篇 188 项按规则不执行、不计入 |
| `python3 -B -m unittest tests_py.test_nq001_tools tests_py.test_official_openings tests_py.test_longform_corpus` | 11 项通过 |
| `python3 -B -m test_support.run api`（主仓 `.venv-api` + 本 worktree 源码） | 449 项通过 |
| `python3 -B -m test_support.check_scene_library --package-id taixu-relics-part1` | `status=valid`，1 部长篇、3 素材、1 条实时规则，错误 0；绑定 `0.1.3` |
| `test_support.longform.longform_cases()` | 自动盘点指向 `taixu-relics-part1@0.1.3` |
| `git diff --check` | 通过 |

全部为离线/确定性检查。未运行 `nq001_live_eval.py` 真实模型路径，未宣称 NQ-001 通过。

## 阻塞与边界

1. **NQ-001 仍未验收**：本轮不运行真实模型，不把离线复现或工程回归当作连续试玩/正文质量证据。
2. **50 组重复候选仍未人工定性修复**：母本冻结策略下仅报告，不自动改写。
3. **`repro_audit` 的 analysis-only 路径仍会失败**：这是预期行为；完整重建闸门应走「母本 + analysis + opening-review + modules」。本轮已补上该路径，不再把仅 analysis 的差异误报为整包不可重建。
4. **真实模型与人工闸门未恢复**：模型请求服务此前暂停；恢复后仍须按人工真实游玩闸门与 NQ-001 专项单独验收。

结论：`0.1.3` checkpoint 已接入 P1 分支，确定性复现审计在完整重建路径下通过；P1 工具与历史报告保留；仍不具备宣称 NQ-001 通过的条件。
