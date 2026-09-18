状态：进度展示及开局关系漏线已修复；已保存剧情的只读桌面验证通过。

## 原因与改动

《太虚遗录》没有接入通用阶段完成度，旧计算仅包含一个终局检查点，导致结局前始终显示 0%。现在没有可靠分母的路线返回 `progress: null`，前端显示“本路线历程：已作出 N 次选择”与当前目标；只统计所选分支祖先链中的正式选择，不统计其他路线、字数或 Arc 切换。已明确阶段的路线继续按检查点显示百分比，已验证终局显示 100%。这不是长篇完成度算法的交付；完整结局进度仍需 P4 的阶段与收束契约。

关系图此前未消费 `openingContext.relationships`，而顾长离开篇使用对话、代词互通姓名，不能命中旧的相邻人名正则。现在读取当前存档的已审核开局关系，仅连接当时已公开且在开局正文出现的人物；关系标签可悬停查看完整说明和“开局前情”来源。不会读取全书未来关系目录或为了连通图谱而造关系。后续正文及已校验关系备注按时间顺序更新，避免旧备注覆盖较新的关系。

关系线改为清晰的弧线，双人物默认横向展开。保留拖动、缩放、头像、身份卡和状态样式。连线表达已知关系与历史交集，人物死亡不删除既有交集；复杂的新关系仍需有公开证据，不承诺现有文本解析可以完整理解任意关系变化。

变更：`api_journey.py`、`api_relationships.py`、`web/src/api/types.ts`、`SessionPage.tsx`、`CharacterGraph.tsx`、`relationshipLayout.ts`、`styles.css`，新增 `tests_api/test_journey_display.py`。

## 验证

- API 135 项测试通过，含新增的未知完成度、分支计数隔离、开局关系可见性及新旧关系覆盖测试。
- 前端 14 项测试、TypeScript/Vite 构建和差异检查通过。
- 只读 API 8001＋桌面前端 5174，验证会话 `22b9da04-7a01-5489-a7c4-682e88fab80d`：第 1/3/36/40 页分别显示 0/1/3/6 次选择。每页均有顾长离与陆照临的公开开局关系连线；第 3/36 页保留陆照临死亡状态。
- [分支结果](evidence/journal-progress-links-2026-09-15/branch-checks.json)、[图谱截图](evidence/journal-progress-links-2026-09-15/linked-graph.png)、[历程截图](evidence/journal-progress-links-2026-09-15/journey-progress.png)。未发起新模型生成。
- 存档 SHA-256 前后均为 `3ba2695159f454572bd5c93b27e225a85bb0df8ec8537d1626daab2e5e740eaa`。
- 检查时 8000/5173 未运行，已按项目配置启动主开发 API 与前端，8000 HTTP 复核当前路线返回 `progress: null`、`choices_made: 6` 和一条开局关系。临时验收服务关闭。
