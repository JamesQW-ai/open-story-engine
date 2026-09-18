# P2 契约迁移记录

当前 worktree 基于 `checkpoint-2026-09-11-player-preview`，P0 的终局与收束实现仍在 `/Users/James/open-story-engine` 的未提交工作区，尚未冻结为可依赖的提交。为避免把 P0/P1 改动带入 P2，本轮只保留契约门禁测试和迁移记录。

`tests_api/test_p2_contracts.py` 使用 `expectedFailure` 固定 P2 的失败契约：

- `journey`、`route-closure`、`ending-proposals` 必须在 OpenAPI 中引用具名 `response_model`；
- 终局提交必须显式返回最终状态、`NaturalEndingReceipt` 和账本覆盖；
- 错误响应必须使用固定错误码枚举，并包含 `ending_proposal_stale` 等收束错误；
- P0 合并后，前端 `web/src/api/types.ts`、`web/src/api/client.ts` 和 `SessionPage.tsx` 应与该 OpenAPI 同步，再移除 `expectedFailure`。

当前失败属于“P0 契约尚未冻结”，不是运行包、前端状态或真实模型失败。测试只检查结构，不宣称产品验收通过，也不使用停用的 `rainy-waiting-room` 长篇桌面夹具。

## 合并后的迁移顺序

1. P0 冻结终局类型、receipt、账本证据、审计结构和错误码；
2. 将严格模型接入 FastAPI `response_model`，并补齐只读/写入 OpenAPI 错误响应；
3. 按同一 schema 更新 TypeScript 类型和 API client 返回类型；
4. 用 `test_support.longform` 导出的当前官方十万字以上长篇夹具补桌面流程：默认入口、连续回合、自由行动、永久后果、自动结局、刷新回看和分支恢复；
5. 分别运行 API 契约门禁、前端类型/测试/构建，以及真实模型专项测试。契约测试通过不等于叙事质量或真实模型验收通过。
