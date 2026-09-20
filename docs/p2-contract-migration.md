# P2 契约接入记录

本 worktree 已从 P0 checkpoint `9cb10a20e0f1c35187ab4b3ec39a7a91d569533a` 接入，并保留 P2 初始门禁提交 `5d72c96`（当前分支提交为其 cherry-pick 后的 `f87ff74`）。StoryPackage 与桌面夹具固定为 `taixu-relics-part1@0.1.3`。

## 已接入

- `journey`、route closure、ending proposal、early end 和 ending commit 使用具名 FastAPI `response_model`。
- Journey 的目标、问题、账本覆盖、终局状态、路线健康和人物证据字段已显式建模。
- Ending proposal 的四项审查、审计输入、失败、调用计量、取消与迟到结果已显式建模；commit 返回 `NaturalEndingReceipt`，包括覆盖、待交代项、已清项、`ending_written`、`proposal_id` 和 `binding_digest`。
- 终局路由错误使用固定错误码枚举，OpenAPI 会暴露 `RouteErrorResponse`。
- `web/src/api/types.ts` 与 `web/src/api/client.ts` 已同步上述字段、可空性和终局状态。
- `web/tests/desktopReaderIntegration.test.mjs` 启动真实构建 reader-server，导出官方十万字长篇，覆盖七身份/七入口、预设方向、自由文本、失败重试、永久后果、收束回执、结局后续写阻断、刷新回看和分支恢复。

## 验收边界

契约与桌面集成测试只证明当前 HTTP/UI 边界和冻结运行包字段可重复验证，不宣称真实模型生成质量、长篇叙事质量或完整产品验收。桌面夹具中的审查和收束结果是结构化交互注入，用于区分前端状态处理与契约失败；真实模型未运行。

当前仍需会话 1 处理的事项：50 组重复候选的人工复核，以及真实模型专项评估。若 P0 后续修改终局字段，必须先更新 OpenAPI、Python models、TypeScript types 和契约测试，禁止以兼容性宽字段掩盖漂移。
